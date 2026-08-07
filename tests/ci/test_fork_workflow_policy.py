from pathlib import Path

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
DEFAULT_BRANCH_EXPR = "${{ github.event.repository.default_branch }}"


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _workflow(name: str) -> dict:
    yaml = YAML(typ="safe")
    return yaml.load(_text(name))


def test_push_ci_targets_fork_default_branch() -> None:
    triggers = _workflow("ci.yml")["on"]
    assert triggers["push"]["branches"] == ["develop"]


def test_runtime_branch_consumers_follow_repository_default() -> None:
    ci = _text("ci.yml")
    assert (
        "if: github.event_name == 'push' && github.ref_name == "
        "github.event.repository.default_branch"
    ) in ci
    assert (
        "if: github.event_name == 'push' && github.ref_name == "
        "github.event.repository.default_branch && "
        "hashFiles('ci-timings-baseline.json') != ''"
    ) in ci

    tests = _text("tests.yml")
    assert (
        "if: needs.test.result == 'success' && github.ref_name == "
        "github.event.repository.default_branch"
    ) in tests

    desktop = _text("e2e-desktop.yml")
    assert (
        "key: visual-baselines-${{ github.ref_name }}\n"
        "          restore-keys: |\n"
        "            visual-baselines-${{ github.event.repository.default_branch }}"
    ) in desktop
    assert (
        'if [ "${{ github.ref_name }}" = "${{ '
        'github.event.repository.default_branch }}" ]; then'
    ) in desktop
    assert (
        "- name: Save updated baselines to cache\n"
        "        if: github.ref_name == github.event.repository.default_branch && always()"
    ) in desktop
    assert "key: visual-baselines-${{ github.event.repository.default_branch }}" in desktop
    assert (
        "- name: Upload visual diffs\n"
        "        id: upload-diffs\n"
        "        if: always() && github.ref_name != github.event.repository.default_branch"
    ) in desktop
    assert (
        "- name: Upload inline E2E evidence\n"
        "        if: always() && github.ref_name != github.event.repository.default_branch"
    ) in desktop

    history = _text("history-check.yml")
    contributor = _text("contributor-check.yml")
    assert 'DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}' in history
    assert 'git merge-base "origin/$DEFAULT_BRANCH" HEAD' in history
    assert 'DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}' in contributor
    assert 'git merge-base "origin/$DEFAULT_BRANCH" HEAD' in contributor
    assert "origin/main" not in history
    assert "origin/main" not in contributor


def test_default_branch_guidance_and_reusable_install_ref_are_aligned() -> None:
    uv_lock = _text("uv-lockfile-check.yml")
    install_run = _text("install-e2e-run.yml")
    assert DEFAULT_BRANCH_EXPR in uv_lock
    assert "origin/main" not in uv_lock
    assert "refs/heads/main" not in install_run
    assert _workflow("install-e2e-run.yml")["on"]["workflow_call"]["inputs"]["install-ref"]["default"] == "refs/heads/develop"


def test_install_e2e_is_manual_or_tag_driven_but_not_scheduled() -> None:
    triggers = _workflow("install-e2e.yml")["on"]
    assert "schedule" not in triggers
    assert "workflow_dispatch" in triggers
    assert triggers["push"]["tags"]


def test_autofix_targets_develop_and_requires_manual_merge() -> None:
    workflow = _workflow("js-autofix.yml")
    text = _text("js-autofix.yml")
    assert workflow["on"]["push"]["branches"] == ["develop"]
    assert '--base "${{ github.event.repository.default_branch }}"' in text
    assert "gh pr create" in text
    assert "gh pr merge" not in text
    assert "gh pr review --approve" not in text
    assert "--approve" not in text
    assert "Wait for merge" not in text
    assert "gh pr close" not in text
    assert "--delete-branch" not in text

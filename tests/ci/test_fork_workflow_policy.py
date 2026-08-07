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
    for name in ("ci.yml", "tests.yml", "e2e-desktop.yml"):
        assert DEFAULT_BRANCH_EXPR in _text(name), name

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

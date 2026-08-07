# Fork `develop` CI Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fork's GitHub Actions use `develop` as the canonical branch, stop scheduled Install E2E runs, and retain auto-fix as a manual-review pull request workflow.

**Architecture:** Keep inherited workflow structure intact and change only fork-relevant branch semantics. Use an explicit `develop` filter where GitHub trigger syntax requires a literal branch, and use `github.event.repository.default_branch` for runtime comparisons, caches, merge bases, and pull request targets. Protect the behavior with focused workflow-policy tests before changing YAML.

**Tech Stack:** GitHub Actions YAML, Bash workflow steps, Python 3.13, pytest, ruamel.yaml, GitHub CLI.

## Global Constraints

- The fork default branch is exactly `develop`.
- Scheduled Install & Update E2E is disabled; manual dispatch and release-tag triggers remain.
- Auto-fix creates or updates `bot/js-autofix` pull requests for manual review and must not approve, merge, auto-close, or delete them.
- The existing read-only patch generation and privileged validated-patch application boundary remains intact.
- Workflows explicitly gated to `NousResearch/hermes-agent` retain their upstream `main` publishing semantics.
- Application code and runtime behavior remain unchanged.

---

## File map

- Create `tests/ci/test_fork_workflow_policy.py`: behavioral contracts for fork workflow triggers, default-branch consumers, scheduled E2E, and manual-review auto-fix.
- Modify `.github/workflows/ci.yml`: run push CI on `develop` and store timing baselines for the repository default branch.
- Modify `.github/workflows/tests.yml`: store duration caches for the repository default branch.
- Modify `.github/workflows/e2e-desktop.yml`: generate, restore, and publish visual-baseline artifacts according to the repository default branch.
- Modify `.github/workflows/history-check.yml`: compare PR history with `origin/<default branch>` and emit accurate remediation text.
- Modify `.github/workflows/contributor-check.yml`: derive the attribution range from `origin/<default branch>`.
- Modify `.github/workflows/uv-lockfile-check.yml`: make stale-lockfile guidance refer to the repository default branch.
- Modify `.github/workflows/install-e2e-run.yml`: make the reusable workflow's default source ref `refs/heads/develop`.
- Modify `.github/workflows/install-e2e.yml`: remove the scheduled trigger while retaining manual and release-tag triggers.
- Modify `.github/workflows/js-autofix.yml`: target `develop`, create/update a PR, and remove automatic merge/cleanup behavior.

---

### Task 1: Align canonical branch behavior

**Files:**
- Create: `tests/ci/test_fork_workflow_policy.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/tests.yml`
- Modify: `.github/workflows/e2e-desktop.yml`
- Modify: `.github/workflows/history-check.yml`
- Modify: `.github/workflows/contributor-check.yml`
- Modify: `.github/workflows/uv-lockfile-check.yml`
- Modify: `.github/workflows/install-e2e-run.yml`

**Interfaces:**
- Consumes: GitHub context value `github.event.repository.default_branch` and the fork's literal push branch `develop`.
- Produces: workflow policies in which push CI targets `develop` and runtime branch-sensitive behavior follows the repository default branch.

- [ ] **Step 1: Write failing branch-policy tests**

Create `tests/ci/test_fork_workflow_policy.py` with the following initial content:

```python
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
```

- [ ] **Step 2: Run the tests and confirm the current workflows violate the contracts**

Run:

```bash
./.venv/bin/python -m pytest -q tests/ci/test_fork_workflow_policy.py
```

Expected: three failures showing `branches == ["main"]`, missing default-branch expressions, and `refs/heads/main` in the reusable Install E2E workflow.

- [ ] **Step 3: Change the explicit CI push branch and dynamic ref conditions**

In `.github/workflows/ci.yml`, use:

```yaml
  push:
    branches: [develop]
```

Replace both timing-baseline conditions with the default-branch relation:

```yaml
if: github.event_name == 'push' && github.ref_name == github.event.repository.default_branch
```

The cache-upload condition keeps its hash guard:

```yaml
if: github.event_name == 'push' && github.ref_name == github.event.repository.default_branch && hashFiles('ci-timings-baseline.json') != ''
```

In `.github/workflows/tests.yml`, use:

```yaml
if: needs.test.result == 'success' && github.ref_name == github.event.repository.default_branch
```

- [ ] **Step 4: Make Desktop visual baselines follow the default branch**

In `.github/workflows/e2e-desktop.yml`, replace branch comparisons and fixed cache keys with:

```yaml
restore-keys: |
  visual-baselines-${{ github.event.repository.default_branch }}
```

```bash
if [ "${{ github.ref_name }}" = "${{ github.event.repository.default_branch }}" ]; then
```

```yaml
if: github.ref_name == github.event.repository.default_branch && always()
key: visual-baselines-${{ github.event.repository.default_branch }}
```

Use the inverse relation for visual-diff and inline-evidence uploads:

```yaml
if: always() && github.ref_name != github.event.repository.default_branch
```

Update adjacent comments from “main” to “default branch” so the operational explanation matches the condition.

- [ ] **Step 5: Make merge-base checks consume the default branch**

In the shell steps of `.github/workflows/history-check.yml` and `.github/workflows/contributor-check.yml`, add:

```yaml
env:
  DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}
```

Use the environment value in both merge-base commands:

```bash
git merge-base "origin/$DEFAULT_BRANCH" HEAD
```

In `history-check.yml`, use `$DEFAULT_BRANCH` in double-quoted shell diagnostics. Inside the single-quoted `STATUS` JSON, use `${{ github.event.repository.default_branch }}` because GitHub evaluates expressions before Bash and Bash must not expand the JSON. Describe “the default branch” instead of `main` and preserve the JSON structure consumed by the review-status aggregator.

- [ ] **Step 6: Align lockfile guidance and reusable Install E2E defaults**

In `.github/workflows/uv-lockfile-check.yml`, replace branch-specific prose and command examples with GitHub's evaluated default branch expression, including:

```text
git fetch origin ${{ github.event.repository.default_branch }}
git rebase origin/${{ github.event.repository.default_branch }}
```

Keep the quoted heredoc so shell command substitution remains disabled; GitHub evaluates `${{ ... }}` before the shell receives the script.

In `.github/workflows/install-e2e-run.yml`, change the call example, input description, input default, and full-ref artifact comment to `refs/heads/develop`:

```yaml
default: refs/heads/develop
```

- [ ] **Step 7: Run the branch-policy tests and relevant CI unit tests**

Run:

```bash
./.venv/bin/python -m pytest -q tests/ci/test_fork_workflow_policy.py tests/ci/test_timings_report.py tests/ci/test_e2e_screenshot_status.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Review and commit the canonical-branch batch**

Run:

```bash
git diff --check
git diff -- .github/workflows tests/ci/test_fork_workflow_policy.py
```

Confirm upstream-only `NousResearch/hermes-agent` publishing guards still contain their intended `main` conditions, then commit:

```bash
git add .github/workflows/ci.yml .github/workflows/tests.yml .github/workflows/e2e-desktop.yml .github/workflows/history-check.yml .github/workflows/contributor-check.yml .github/workflows/uv-lockfile-check.yml .github/workflows/install-e2e-run.yml tests/ci/test_fork_workflow_policy.py
git commit -m "ci: align fork workflows with develop"
```

---

### Task 2: Disable scheduled Install E2E and make auto-fix PR-only

**Files:**
- Modify: `tests/ci/test_fork_workflow_policy.py`
- Modify: `.github/workflows/install-e2e.yml`
- Modify: `.github/workflows/js-autofix.yml`

**Interfaces:**
- Consumes: `develop`, `github.event.repository.default_branch`, the existing `bot/js-autofix` branch, and the existing validated patch artifact.
- Produces: no fork cron trigger for Install E2E and at most one manual-review auto-fix pull request targeting the default branch.

- [ ] **Step 1: Add failing trigger and auto-fix safety contracts**

Append to `tests/ci/test_fork_workflow_policy.py`:

```python
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
    assert "Wait for merge" not in text
    assert "gh pr close" not in text
    assert "--delete-branch" not in text
```

- [ ] **Step 2: Run the new contracts and confirm they fail**

Run:

```bash
./.venv/bin/python -m pytest -q tests/ci/test_fork_workflow_policy.py::test_install_e2e_is_manual_or_tag_driven_but_not_scheduled tests/ci/test_fork_workflow_policy.py::test_autofix_targets_develop_and_requires_manual_merge
```

Expected: both tests fail because the cron trigger, `main` target, auto-merge command, and polling cleanup step still exist.

- [ ] **Step 3: Remove only the Install E2E schedule trigger**

Delete this block from `.github/workflows/install-e2e.yml`:

```yaml
  schedule:
    # Every 12 hours, off the hour to avoid the top-of-hour runner crunch.
    - cron: '20 7,19 * * *'
```

Update the header trigger documentation to list only release-tag and manual execution. Do not change the tag patterns or matrices.

- [ ] **Step 4: Retarget auto-fix and remove autonomous merge behavior**

In `.github/workflows/js-autofix.yml`, set:

```yaml
  push:
    branches: [develop]
```

Rename the PR step to `Create/update PR for manual review`, and create the PR with:

```bash
gh pr create \
  --head "$BOT_BRANCH" --base "${{ github.event.repository.default_branch }}" \
  --title 'fmt(js): `npm run fix` auto-fix' \
  --body 'Auto-generated by the `auto-fix lint issues & formatting` workflow. Review the patch and CI results, then merge manually if appropriate.'
```

Remove the `gh pr merge "$PR_NUM" --auto --squash` command and delete the entire `Wait for merge, auto-close on failure or stale` step. Remove the now-unused PR URL parsing and update the security-model comments to state that the privileged job creates or updates a manual-review PR.

- [ ] **Step 5: Run all workflow-policy tests**

Run:

```bash
./.venv/bin/python -m pytest -q tests/ci/test_fork_workflow_policy.py
```

Expected: all five policy tests pass.

- [ ] **Step 6: Review and commit the automation-policy batch**

Run:

```bash
git diff --check
git diff -- .github/workflows/install-e2e.yml .github/workflows/js-autofix.yml tests/ci/test_fork_workflow_policy.py
```

Confirm `generate-patch`, the disallowed-file guard, `git apply --check`, and the dedicated bot branch remain unchanged, then commit:

```bash
git add .github/workflows/install-e2e.yml .github/workflows/js-autofix.yml tests/ci/test_fork_workflow_policy.py
git commit -m "ci: make fork automation review-only"
```

---

### Task 3: Enable safe PR creation and verify origin

**Files:**
- No repository files modified.

**Interfaces:**
- Consumes: committed workflow changes, GitHub repository admin access, and authenticated `gh` with `repo` and `workflow` scopes.
- Produces: an origin repository that allows the PR-only auto-fix workflow to create pull requests and runs CI on `develop`.

- [ ] **Step 1: Run fresh local verification before publishing**

Run:

```bash
./.venv/bin/python -m pytest -q tests/ci/test_fork_workflow_policy.py tests/ci/test_timings_report.py tests/ci/test_e2e_screenshot_status.py
git diff --check HEAD~2..HEAD
git status --short --branch
```

Expected: all selected tests pass, diff check exits zero, and the worktree is clean with `develop` ahead of `origin/develop` only by the intentional commits.

- [ ] **Step 2: Inspect and enable the narrow repository Actions setting**

Inspect current state:

```bash
gh api repos/WangYan-Good/hermes-agent/actions/permissions/workflow
```

Enable Actions pull-request creation without changing the repository's default token permission:

```bash
gh api --method PUT repos/WangYan-Good/hermes-agent/actions/permissions/workflow -F can_approve_pull_request_reviews=true
```

Re-read the endpoint and require `"can_approve_pull_request_reviews": true`. Do not set `default_workflow_permissions` to `write`.

- [ ] **Step 3: Push the reviewed commits to `origin/develop`**

Run:

```bash
git push origin develop
```

Expected: the remote `develop` ref advances to local `HEAD` and the `CI` workflow starts because workflow files are within the change set.

- [ ] **Step 4: Verify regular CI was triggered by the `develop` push**

Run:

```bash
gh run list -R WangYan-Good/hermes-agent --branch develop --commit "$(git rev-parse HEAD)" --limit 10 --json databaseId,workflowName,status,conclusion,url
```

Expected: a `CI` run exists for the pushed HEAD. Follow it with:

```bash
gh run watch "$(gh run list -R WangYan-Good/hermes-agent --branch develop --commit "$(git rev-parse HEAD)" --workflow ci.yml --limit 1 --json databaseId --jq '.[0].databaseId')" -R WangYan-Good/hermes-agent --exit-status
```

If it fails, inspect the same run with:

```bash
gh run view "$(gh run list -R WangYan-Good/hermes-agent --branch develop --commit "$(git rev-parse HEAD)" --workflow ci.yml --limit 1 --json databaseId --jq '.[0].databaseId')" -R WangYan-Good/hermes-agent --log-failed
```

Report the observed root cause and do not broaden the fix without approval.

- [ ] **Step 5: Manually verify auto-fix remains review-only**

Run:

```bash
gh workflow run js-autofix.yml -R WangYan-Good/hermes-agent --ref develop
```

Then wait for the exact latest manual run:

```bash
gh run watch "$(gh run list -R WangYan-Good/hermes-agent --workflow js-autofix.yml --branch develop --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId')" -R WangYan-Good/hermes-agent --exit-status
```

Expected: the run succeeds. If no patch is needed, `apply-patch` is skipped. If a patch is produced, one open pull request targets `develop` and remains unmerged.

- [ ] **Step 6: Verify scheduled Install E2E is absent remotely**

Run:

```bash
gh workflow view install-e2e.yml -R WangYan-Good/hermes-agent --yaml
```

Expected: the remote workflow contains `workflow_dispatch` and release-tag `push` entries but no `schedule` entry.

- [ ] **Step 7: Report residual verification limits**

If auto-fix finds no patch, explicitly report that the no-op path and repository permission were verified but actual PR creation remains unexercised. Do not introduce a formatting defect solely to force the privileged path.

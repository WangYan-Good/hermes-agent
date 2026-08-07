# Fork `develop` CI Alignment Design

## Context

The `WangYan-Good/hermes-agent` fork now uses `develop` as its default branch,
while several inherited GitHub Actions workflows still trigger on or compare
against `main`. This creates two distinct failure modes:

- regular push CI does not run for `develop`, so the absence of failures can be
  mistaken for a green build;
- reusable checks and automation still use `main` for caches, merge bases, pull
  request targets, and stale-branch detection.

The fork also inherits the scheduled Install & Update E2E workflow. The fork
does not carry upstream release tags, so scheduled runs fail before reaching
the install or update matrices. Separately, the JS/TS auto-fix workflow has
demonstrated value by generating a non-empty formatting patch, but its attempt
to create a pull request failed because repository Actions may not create pull
requests.

## Goals

1. Make `develop` the canonical push branch for fork CI.
2. Use the repository default branch dynamically wherever GitHub Actions
   expressions allow it, so checks do not silently drift after future renames.
3. Stop scheduled Install & Update E2E runs in the fork while retaining manual
   and release-tag entry points.
4. Retain JS/TS auto-fix as a pull-request-producing workflow, with manual
   review and merge.
5. Preserve upstream-only release and publishing behavior.

## Non-goals

- Do not sync upstream release tags into the fork.
- Do not enable auto-merge or automated pull request approval.
- Do not change Docker publishing, website publishing, Skills Index publishing,
  or other jobs explicitly gated to `NousResearch/hermes-agent`.
- Do not change application code or runtime behavior.

## Design

### Canonical branch handling

Workflow `push.branches` filters cannot use expressions, so fork CI and
auto-fix will explicitly listen to `develop`. Other branch-sensitive behavior
will use `github.event.repository.default_branch` where supported instead of a
literal branch name.

The following inherited paths must be aligned:

- `.github/workflows/ci.yml`
  - trigger push CI on `develop`;
  - save timing baselines when the current ref is the repository default branch.
- `.github/workflows/tests.yml`
  - save merged test-duration data on the repository default branch.
- `.github/workflows/e2e-desktop.yml`
  - generate and cache visual baselines on the repository default branch;
  - treat other refs as comparison runs and upload visual evidence for them;
  - derive the fallback cache key from the default branch.
- `.github/workflows/history-check.yml`
  - compute the common ancestor against `origin/<default branch>`;
  - update diagnostics to describe the default branch rather than `main`.
- `.github/workflows/contributor-check.yml`
  - compute the attribution range from `origin/<default branch>`.
- `.github/workflows/uv-lockfile-check.yml`
  - update remediation guidance to reference the default branch used by the
    fork. This is diagnostic text, not lockfile behavior.
- `.github/workflows/install-e2e-run.yml`
  - align the reusable workflow's default target ref with `develop`.

Literal `main` references that are guarded by
`github.repository == 'NousResearch/hermes-agent'` remain unchanged because
they describe upstream publishing policy, not fork CI policy.

### Scheduled Install & Update E2E

Remove the `schedule` trigger from `.github/workflows/install-e2e.yml` in the
fork. Keep both remaining entry points:

- `workflow_dispatch`, for intentional manual validation;
- release-tag pushes, if the fork later creates its own release tags.

Removing the trigger is preferred over a job-level repository condition because
it avoids creating a skipped workflow run every twelve hours and matches the
requested behavior: scheduled E2E is off in the fork.

### JS/TS auto-fix

Keep the existing two-job trust boundary:

1. `generate-patch` runs repository-controlled tooling with read-only contents
   permission and uploads a bounded patch artifact.
2. `apply-patch` downloads and validates the patch, commits it on
   `bot/js-autofix`, and creates or updates a pull request targeting `develop`.

Change the workflow as follows:

- trigger on relevant pushes to `develop` and retain manual dispatch;
- target the repository default branch when creating the pull request;
- keep force-updating only the dedicated `bot/js-autofix` branch;
- stop after creating or updating the pull request;
- remove the auto-merge command and the polling job step that waits for merge,
  closes failed/stale pull requests, or deletes the bot branch;
- update comments and pull request text so they promise manual review rather
  than automatic merge.

The repository setting that permits GitHub Actions to create pull requests must
be enabled. Although GitHub labels the setting as allowing Actions to “create
and approve pull requests,” this workflow will request only the permissions it
needs and will contain no approval or merge command. If a GitHub App token is
configured later, the existing token action may use it without changing the
workflow design.

### Error and stale-state behavior

- If patch generation finds no changes, `apply-patch` remains skipped.
- If the generated patch touches a disallowed file type or no longer applies
  cleanly, the run fails without modifying `develop`.
- If an auto-fix pull request already exists, the workflow updates its bot
  branch instead of creating duplicates.
- Since merge is manual, conflicts or failed checks remain visible on the pull
  request for a human decision; automation does not close them.
- The existing historical `bot/js-autofix` branch can be reused by the first
  aligned run and does not need destructive cleanup.

## Verification

Local verification will cover:

1. YAML parsing or the repository's available workflow lint path.
2. Targeted tests that assert workflow triggers and branch-sensitive behavior.
3. Static checks confirming that fork-critical paths no longer use a literal
   `main`, while upstream-only publishing guards remain intact.
4. A clean working tree review showing only the intended workflow, test, and
   design/plan changes.

Remote verification after commit and push will cover:

1. enabling the repository Actions pull-request creation setting;
2. causing a controlled `develop` push and confirming regular CI starts;
3. manually dispatching auto-fix and confirming it either reports no fixes or
   opens/updates a pull request without merging it;
4. confirming Install & Update E2E no longer has a scheduled trigger in the
   fork.

## Risks and mitigations

- **Fork-only drift during future upstream rebases:** keep changes focused and
  prefer default-branch expressions where possible, reducing repeated edits.
- **Broader Actions pull-request capability:** retain the two-job trust boundary,
  limit the privileged job to a validated patch artifact, and remove all
  approval and merge operations.
- **Visual baseline cache reset:** changing the canonical cache key may cause one
  fresh baseline run; subsequent PRs reuse the `develop` baseline.
- **Manual auto-fix PR accumulation:** the fixed bot branch and existing-PR lookup
  ensure at most one open auto-fix PR.

## Success criteria

- A push to `develop` starts the regular CI workflow.
- Default-branch caches and PR checks use `develop`, not stale `main` state.
- The fork produces no scheduled Install & Update E2E runs.
- Auto-fix can create or update one pull request targeting `develop` and cannot
  merge or approve it.
- Upstream-only publishing behavior remains unchanged.

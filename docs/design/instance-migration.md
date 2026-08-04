# Whole-instance migration to another host

**Date:** 2026-08-03
**Branch:** `personal`
**Status:** design approved, pending implementation plan

## Problem

Moving a Hermes instance to another machine is entirely manual today. `hermes
backup` produces a zip and `hermes import` unpacks one, but everything between
them — getting the archive to the other machine, making sure Hermes is even
installed there, checking the target can hold the data, deciding when to cut
over — is left to the operator.

The gap is not the packaging. `backup.py` already solves that, and solves it
*for this case*: `_EXCLUDED_DIRS` drops the codebase, caches, venvs and
`node_modules`, and excludes `checkpoints` with the note that they are
"session-hash-keyed so they don't port to another machine anyway".
`_IMPORT_SKIP_NAMES` refuses to restore `gateway_state.json`, `gateway.pid`,
`gateway.lock` and `processes.json` — precisely the runtime residue that must
not follow an instance to a new host. `_SECRET_FILE_NAMES` restores `.env`,
`auth.json` and `state.db` at 0600. `MemoryProvider.backup_paths()` reaches
state living outside `HERMES_HOME` entirely (`~/.honcho`, `~/.hindsight`).

What is missing is the orchestration around it.

## Goals

- Register a target host, verify it, then migrate the whole instance in one
  action.
- Work whether or not the target already has Hermes installed.
- Linux, macOS and Windows targets.
- Never modify the source instance, so a failed migration is always
  recoverable by restarting it.
- Same capability headless as in the dashboard.

## Non-goals

- **Clone / dual-run.** Exactly one instance is live. Two instances holding the
  same `auth.json` and `cron/jobs.json` would authenticate as the same user and
  fire the same scheduled jobs twice; resolving that needs bidirectional
  conflict rules and is a different feature.
- **Repeatable sync to a warm standby.** Same reason, plus change detection and
  idempotent transfer of live SQLite.
- **Migrating the deployment substrate.** A containerised source migrates to a
  *native* target. Carrying `docker-compose.yml`, image builds or systemd units
  across would mean handling every deployment form × three operating systems.
- **Erasing the source.** Credentials remain on the source disk after a
  successful migration. Automatic deletion of user data is not a thing a
  migrate button should do.
- **Resumable transfer.** The state archive is in the hundreds of MB; a full
  retransmit costs less than implementing resume.

## Transport: SSH

SSH is not merely preferred, it is the only option that satisfies the
requirements. A target without Hermes installed exposes no HTTP endpoint, so an
HTTP transport between two Hermes instances cannot bootstrap a bare machine —
it would serve only the "already installed" half of the requirement while
adding a second code path.

SSH also means credentials cross the network inside an already-encrypted
channel, so no key exchange needs designing.

The image ships `ssh` but **not** `scp` or `rsync`, and no `paramiko` /
`asyncssh` / `fabric`. `hermes backup` already emits a single `.zip`, so the
transfer streams that one file over a single `ssh` invocation
(`ssh host 'cat > <tmp>' < archive.zip`) rather than depending on a second
binary or a new Python dependency.

Windows targets require OpenSSH Server to be enabled — a documented
prerequisite, not something the tool installs.

## Components

Four units, drawn so each can be understood and tested on its own.

| Unit | Responsibility |
|---|---|
| `hermes_cli/remote_exec.py` | Knows SSH, knows nothing about Hermes. Run a command and return `(rc, stdout, stderr)`; stream a local file to a remote path; detect remote OS and architecture. Shells out to the system `ssh`. The only IO boundary. |
| `hermes_cli/migration_admin.py` | Every rule, as pure functions over arguments: host-profile validation, preflight definitions and verdicts, migration state machine. Takes an **injected executor** rather than opening its own connection. |
| `web_server.py` routes | Thin: `_require_token`, `_profile_scope`, `ValueError` → 400. |
| CLI `hermes migrate host` | Sits alongside the existing `hermes migrate xai`. |

The executor injection is what makes the rules testable without a real machine:
a fake executor exercises preflight verdicts, state-machine transitions,
profile validation and fingerprint comparison entirely in-process. Only
`remote_exec` needs a live sshd. This mirrors `provider_proxy_admin.py`, where
the rules are pure functions over a config dict and the routes stay thin.

**The CLI is the implementation; the dashboard drives it.** The dashboard
launches `hermes migrate host <id> --execute` through the existing
`_spawn_hermes_action()` and polls `/api/actions/{name}/status`, tailing its log
— reusing the long-running-action machinery instead of inventing a job system,
and guaranteeing the feature works headless.

## Host profiles

Stored in `$HERMES_HOME/migration_targets.json` at 0600, **not** in
`config.yaml`. Two reasons: `config.yaml` is 0640 and is re-dumped by every
dashboard write, and a migration target is an operator asset rather than
Hermes configuration.

Fields: `id`, `label`, `host`, `port`, `user`, `identity_file`, `target_home`,
`host_fingerprint`, `last_preflight`.

**Only a path to the private key is stored** — never key material, never a
password. Password authentication is deliberately unsupported: it forces a
plaintext secret into the file to save one `ssh-copy-id`. Because the file
holds no secrets, the dashboard can display it verbatim; no redaction layer is
needed.

**Host fingerprints are pinned (TOFU).** The first preflight accepts the host
key and records its fingerprint; every later connection verifies against it and
fails hard on mismatch. `StrictHostKeyChecking=accept-new` is acceptable for a
one-off human investigation but not as product behaviour, because this channel
carries plaintext `.env` and `auth.json`. A changed host key is an event for a
human to judge, not to auto-accept.

## Flow

### Stage 0 — profile

Address, port, user, identity file, target `HERMES_HOME`. Touches nothing
remote.

### Stage 1 — preflight (read-only)

Never modifies the target. Verdicts are two-tier:

**Blocking** — SSH authentication succeeds; OS and architecture identified;
free space ≥ 2× archive size; `python3` present; target `HERMES_HOME` is
*safe to overwrite*.

"Safe to overwrite" needs care, because a target that already has Hermes
installed — which the requirements explicitly allow — has a populated
`HERMES_HOME` from its own first run. Treating any non-empty directory as
blocking would reject exactly that case. The check is therefore: absent or
empty passes; a *pristine* home (default `config.yaml`, no `auth.json`, no
`sessions/`, no populated `state.db`) passes; anything carrying real user state
blocks until the operator explicitly confirms overwrite. Confirmation is a
distinct acknowledgement, not a checkbox that defaults on — this is the branch
that destroys someone else's instance.

**Warning** — Hermes already installed at a different version; dashboard port
already bound; **clock skew**. `auth.json` holds expiry-sensitive OAuth tokens,
so a target with a skewed clock presents as "login expired" — a failure mode
that is near-impossible to diagnose after the fact and trivial to check before.

### Stage 2 — execute

Each step is idempotent and independently retryable.

1. **Install Hermes on the target if absent** — drives the existing
   `scripts/install.sh` / `scripts/install.ps1`.
2. **Stop the source gateway** — downtime starts here.
3. `hermes backup` on the source — the **full** archive, not `--quick`.
   `--quick` captures only critical state (`config.yaml`, `state.db`, `.env`,
   `auth.json`, cron) and would silently drop `skills/`, memories, plans and
   projects, which are user content a migration must carry.
4. Stream the archive over SSH.
5. `hermes import` on the target.
6. **Verify** — config schema version, `auth.json` at 0600, every SQLite store
   opens.
7. **Stop.** Report "ready, not started".

Install precedes the stop deliberately. It depends on no data, and it is the
step most likely to fail (network, dependencies, permissions) — so it should
fail while the source is still serving, where the cost is nearly zero. The
interaction is unchanged: one action, halting at the same point.

The run halts before starting the target because that is where a human should
decide. Migration completes to a verified-but-idle instance; promotion is
deliberate.

## Failure and recovery

**The source is only ever stopped, never modified.** That single property is
the whole basis of rollback.

| Failure point | Source | Recovery |
|---|---|---|
| Preflight | Untouched | Target unmodified; fix the profile and retry |
| Target install | Untouched, still serving | Retry the install |
| Transfer / import | Stopped, data intact | Restart the source |
| Verification | Stopped, data intact | Restart the source; clear target home before retrying |

`hermes import` overwrites, so a failed restore leaves the target partially
populated. Retrying requires an empty target `HERMES_HOME`, and the tool asks
for explicit confirmation rather than silently deleting it.

**Temporary archives.** A plaintext zip exists on both hosts. Both are created
at 0600 and removed in a `finally`, including on the failure path — not by
remembering to clean up.

**Residual credentials.** After success the source disk still holds `.env`,
`auth.json` and `state.db`. The completion screen states this so the operator
can choose between keeping it as a rollback path and clearing it by hand.

## UI

### Placement

One top-level nav entry, **"迁移与备份" / "Backup & Migration"**, at `/migrate`.
The page holds two sections: *Backup & restore* (the existing controls, moved
off `SystemPage`) and *Migrate to another host* (new).

Not a nav submenu, despite that being the natural way to express the grouping.
`NavItem` is `{icon, label, labelKey, path}` with no children, and the sidebar
renders one flat list with a collapsed icon-only mode. Supporting nesting means
changing a component every nav entry shares and answering how a submenu behaves
when the sidebar is collapsed to icons — infrastructure work that would land on
this feature's critical path for presentational benefit. Grouping the two
capabilities on one page delivers the same grouping without touching shared
navigation. If a real submenu is wanted later it is an independent change.

Moving backup off `SystemPage` is deliberate: that file is ~1540 lines, and
backup belongs next to migration rather than in a general system panel.

### Panels

*Backup & restore* — create, download and restore, reusing the existing
`/api/ops/backup`, `/api/ops/backup/download`, `/api/ops/import` and
`/api/ops/import-upload` routes unchanged.

*Migrate* — host list (add / edit / remove), preflight results (per check
✓ / ⚠ / ✗, blocking and warning tiers visually distinct so a warning is
obviously not a stop), and execution (stage progress plus a live log tail).
The completion screen states the target is ready but **not started**, gives the
command to start it, and notes the credentials left behind on the source.

### Routes

All token-protected via `_require_token`: they carry SSH connection details and
trigger remote execution.

| Route | Purpose |
|---|---|
| `GET /api/migration/targets` | List host profiles |
| `POST /api/migration/targets` | Create |
| `PUT /api/migration/targets/{id}` | Update |
| `DELETE /api/migration/targets/{id}` | Remove |
| `POST /api/migration/targets/{id}/preflight` | Run preflight, return per-check verdicts |
| `POST /api/migration/targets/{id}/migrate` | Spawn the migration action, return its name |

Progress polling reuses the existing `/api/actions/{name}/status`; no new
job-tracking endpoint.

### i18n

New keys go in a `migration` block in `types.ts`, `en.ts` and `zh.ts` — **and
in the other 15 locale files**.

This is not boilerplate advice. The per-provider-proxy design asserted that the
other locales are partial and fall back to English by construction; they are
not. `define-locale.ts` exports a deep-partial `TranslationOverrides`, and exactly
one locale — `ar.ts` — actually uses it. The other 16 are declared
`: Translations`, the full type, so a new required key must be added to all
sixteen; `ar.ts` inherits it from English automatically. Adding a
required key to three of them made `tsc -b` fail on the other 15, which meant
the Docker image could not be built at all.

**Verify with `tsc -b` or `npm run build`, never `npm run typecheck`.** The root
`tsconfig.json` is `{"files": [], "references": [...]}`, so `tsc -p .` checks an
empty file list and passes regardless — confirmed by planting a deliberate type
error that `npm run typecheck` accepted and `tsc -b` caught.

### Where the logic lives

Pure logic goes in `web/src/lib/migration.ts`: preflight verdict → display
mapping, stage → progress, client-side host-profile validation. This repo's
frontend tests cover `lib/` only and there is no component-test setup, so logic
placed inside components is untestable. That constraint shapes the file
split and cannot be retrofitted afterwards.

## Testing

Per `CLAUDE.md`, everything runs in a container.

- `migration_admin` — every rule against a fake executor: preflight verdicts at
  both tiers, state-machine transitions, profile validation, fingerprint
  mismatch. No real host involved.
- `remote_exec` — integration tests against an sshd container, covering real
  SSH behaviour: auth failure, host-key mismatch, non-zero exit, stream
  transfer.
- End-to-end smoke: migrate into an sshd container and assert the restored
  home has the expected state files at the expected permissions.
- `web/src/lib/migration.ts` — vitest over the pure mappings. No component
  tests; this repo has none and inventing a component-test setup for one page
  is out of scope.
- Frontend verification runs `npm run build`, not `npm run typecheck` — see
  the i18n note above for why the latter proves nothing.

## Decisions made during design

- **Reuse `backup`/`import` rather than writing a migration packer.** The
  exclusion lists, the WAL-safe SQLite copy, the import-time skip list and the
  0600 restore already encode lessons learned (`_QUICK_STATE_FILES` carries
  issue references such as #52889). A parallel implementation would re-learn
  them in a second copy that nobody watches. If archive size becomes a problem,
  add a `--for-migration` mode to `backup` rather than forking the logic.
- **Move, not clone.** Chosen over repeatable sync because it removes
  bidirectional conflict resolution, change detection and split-brain entirely.
- **Halt before starting the target.** The alternative — starting it
  automatically — means discovering a broken target *after* service has moved.
- **Install before stopping the source.** Free reduction of the downtime
  window, and it moves the most failure-prone step to where failure is cheap.
- **Rejected: HTTP between two Hermes instances.** Cannot bootstrap a target
  with no Hermes installed, so it covers half the requirement and adds a second
  code path.
- **Rejected: password authentication.** Requires storing a plaintext secret to
  avoid one `ssh-copy-id`.
- **Rejected: resumable transfer.** YAGNI at this archive size.
- **Rejected: a real nav submenu.** `NavItem` has no children and the sidebar
  renders a flat list with a collapsed icon-only mode, so nesting means
  reworking shared navigation and defining submenu behaviour under collapse.
  One page with two sections gives the same grouping without putting shared
  infrastructure on this feature's critical path.

# Whole-Instance Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register a target host, verify it, then migrate a whole Hermes instance to it over SSH — covering targets that do and do not already have Hermes installed.

**Architecture:** `remote_exec.py` is the only IO boundary and knows nothing about Hermes. `migration_admin.py` holds every rule as pure functions over arguments and takes an *injected executor*, so all rules are testable in-process against a fake. `web_server.py` routes stay thin. The CLI (`hermes migrate host`) is the real implementation; the dashboard drives it through the existing `_spawn_hermes_action()` and polls `/api/actions/{name}/status`.

**Tech Stack:** Python 3.13, FastAPI/Starlette, system `ssh` (no paramiko), React 19 + TypeScript + vitest, `@nous-research/ui`.

**Design source:** `docs/design/instance-migration.md` (approved 2026-08-03).

---

## Global Constraints

- **All verification runs in a container, never on the host** (`CLAUDE.md`). No `pytest`, `npm`, `npx`, `vitest`, `tsc`, or `eslint` on the host shell.
- **Commit messages carry no AI-attribution trailer** of any kind (`CLAUDE.md`).
- **Never run the whole `tests/hermes_cli` directory.** It hard-exits at ~33% on this branch *and* on clean HEAD — a pre-existing fault, not a regression. Always name the specific test file.
- **Frontend type-checking must use `tsc -b` or `npm run build`. `npm run typecheck` proves nothing** — the root `tsconfig.json` is `{"files": [], "references": [...]}`, so `tsc -p .` checks an empty file list and passes with a deliberate type error present.
- **New i18n keys must be added to all 17 locale files**, not just `en`/`zh`. Every locale is declared `: Translations` (the full type); `define-locale.ts` exports a deep-partial `TranslationOverrides` that no locale uses. Missing one fails `tsc -b` and makes the Docker image unbuildable.
- **`run_backup()` / `run_import()` call `sys.exit(1)` on error.** They are CLI entrypoints, not APIs. Always invoke them as subprocesses and check the return code, or a failure kills the migration process and skips temp-archive cleanup.
- **Proxy URLs and SSH details never reach a log line raw.** Reuse `utils.redact_proxy_url` style discipline for anything credential-adjacent.
- **Branch:** `personal`. Repo root: `/mnt/main/CodeSpace/Project/hermes-agent`.
- **Never `git add -A`.** Always add explicit paths and confirm with `git diff --cached --name-only` before committing.

---

## Batch Discipline

Network here is unreliable (measured: ~19% of sustained transfers through the local proxy fail, in bursts lasting 40s+). The plan is therefore built so no step depends on a long-lived connection surviving:

- **Every task ends in a commit.** An interrupted session resumes at the last commit, never mid-edit.
- **Every verification command names a single test file.** No whole-directory or whole-suite runs.
- **No step requires a live remote host except Tasks 2b and 10.** Everything else is testable in-process against a fake executor, so connectivity problems cannot block the bulk of the work.
- **Container commands are one-shot and idempotent.** Re-running a verification step after a dropped connection is always safe.

---

## File Structure

**Create:**

| File | Responsibility |
|---|---|
| `hermes_cli/remote_exec.py` | SSH only, Hermes-agnostic: run a command returning `(rc, stdout, stderr)`, stream a local file to a remote path, detect remote OS/arch. Shells out to system `ssh`. The single IO boundary. |
| `hermes_cli/migration_admin.py` | Every rule as pure functions: host-profile validation and storage, preflight definitions and verdicts, migration stage sequencing. Takes an injected executor. No FastAPI imports. |
| `tests/hermes_cli/test_migration_targets.py` | Task 1 — profile model and store. |
| `tests/hermes_cli/test_remote_exec.py` | Task 2 — command building and parsing (no network); sshd integration marked separately. |
| `tests/hermes_cli/test_migration_preflight.py` | Task 3 — preflight verdicts against a fake executor. |
| `tests/hermes_cli/test_migration_stages.py` | Task 4 — stage sequencing, failure classification, cleanup. |
| `tests/hermes_cli/test_migration_api.py` | Task 6 — the six routes via `starlette.testclient`. |
| `web/src/lib/migration.ts` | Pure frontend mappings: preflight verdict → display, stage → progress, client-side profile validation. |
| `web/src/lib/migration.test.ts` | vitest for the above. |
| `web/src/pages/MigratePage.tsx` | The `/migrate` page: backup/restore section + migration section. |

**Modify:**

| File | Change |
|---|---|
| `hermes_cli/migrate.py` | Add `cmd_migrate_host` and dispatch it from `cmd_migrate`. |
| `hermes_cli/main.py:15604-15637` | Register the `host` subparser alongside `xai`; extend the existing import on line 15604. |
| `hermes_cli/web_server.py` | Six `/api/migration/targets*` routes; one entry in `_ACTION_LOG_FILES` (dict at line 3787). |
| `web/src/lib/api.ts` | Client methods for the six routes; `MigrationTarget` / `PreflightResult` types. |
| `web/src/App.tsx` | Lazy import (~line 81-96 block), route map entry (~line 173), nav entry (~line 192-218 block). |
| `web/src/i18n/types.ts`, `en.ts`, `zh.ts` | New `migration` block. |
| `web/src/i18n/{af,ar,de,es,fr,ga,hu,it,ja,ko,pt,ru,tr,uk,zh-hant}.ts` | Same block, English strings — required or `tsc -b` fails. |
| `web/src/pages/SystemPage.tsx` | Remove the backup/restore UI (moves to `MigratePage`). |
| `docs/design/instance-migration.md` | "As built" section recording deviations. |

**Why a separate `remote_exec.py`:** it is the only code that cannot be tested without a real sshd. Isolating it means every rule in `migration_admin.py` is exercised in-process against a fake, so a flaky network blocks integration tests only — not the bulk of the work.

---

## Verification Commands

Both images already exist. `localhost/hermes-test` = the runtime image plus pytest, Node 22, npm, and a populated `/opt/hermes/web/node_modules`.

**Backend (single file, always):**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/<FILE>.py
```

`HERMES_WRITE_SAFE_ROOT=` must be passed empty — the value baked into the image makes tests that write under `/tmp` fail.

**Frontend vitest:**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npx vitest run src/lib/migration.test.ts"
```

**Frontend type-check + production build (the only trustworthy check):**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npm run build"
```

**Baselines, so a pre-existing failure is not misread as yours:**

- `npx eslint src/components/OAuthProvidersCard.tsx` reports **2 warnings, 0 errors** today (`react-hooks/refs`, `react-hooks/set-state-in-effect`). Leave them.
- `tests/agent` + `tests/run_agent` run together produce ~244 failures on this branch and 245 on clean HEAD — pre-existing test isolation, not a regression.

---

### Task 1: Host profile model and store

**Files:**
- Create: `hermes_cli/migration_admin.py`
- Test: `tests/hermes_cli/test_migration_targets.py`

**Interfaces:**
- Consumes: nothing. First task.
- Produces:
  - `TARGETS_FILENAME: str` == `"migration_targets.json"`
  - `targets_path(home: Path) -> Path`
  - `validate_target(raw: Dict[str, Any]) -> Dict[str, Any]` — returns a normalized profile; raises `ValueError` with a human-readable message
  - `load_targets(path: Path) -> Dict[str, Dict[str, Any]]` — keyed by id; `{}` when absent or corrupt
  - `save_targets(path: Path, targets: Dict[str, Dict[str, Any]]) -> None` — writes 0600

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_migration_targets.py`:

```python
"""Host-profile model and store for whole-instance migration.

A profile names a machine and how to reach it. It deliberately holds NO secret:
only a path to a private key. The store is 0600 anyway, because the set of
machines you can reach is itself worth protecting.
"""

import json
import stat

import pytest


class TestValidateTarget:
    def test_minimal_profile_normalizes(self):
        from hermes_cli.migration_admin import validate_target

        got = validate_target({"id": "prod", "host": "10.0.0.5", "user": "hermes"})
        assert got["id"] == "prod"
        assert got["host"] == "10.0.0.5"
        assert got["user"] == "hermes"
        assert got["port"] == 22, "port must default to 22"
        assert got["label"] == "prod", "label defaults to the id"
        assert got["host_fingerprint"] is None, "unknown until first preflight"

    def test_id_must_be_a_slug(self):
        from hermes_cli.migration_admin import validate_target

        # The id becomes a filename-safe key and appears in URLs.
        for bad in ("has space", "has/slash", "", "UPPER"):
            with pytest.raises(ValueError, match="id"):
                validate_target({"id": bad, "host": "h", "user": "u"})

    def test_host_and_user_are_required(self):
        from hermes_cli.migration_admin import validate_target

        with pytest.raises(ValueError, match="host"):
            validate_target({"id": "a", "user": "u"})
        with pytest.raises(ValueError, match="user"):
            validate_target({"id": "a", "host": "h"})

    def test_port_must_be_a_valid_tcp_port(self):
        from hermes_cli.migration_admin import validate_target

        for bad in (0, 65536, -1, "twenty-two"):
            with pytest.raises(ValueError, match="port"):
                validate_target({"id": "a", "host": "h", "user": "u", "port": bad})

    def test_password_is_rejected_outright(self):
        from hermes_cli.migration_admin import validate_target

        # Supporting passwords would force a plaintext secret into the store to
        # save one ssh-copy-id. Reject the key rather than silently drop it, so
        # nobody believes they configured something that is not in effect.
        with pytest.raises(ValueError, match="password"):
            validate_target(
                {"id": "a", "host": "h", "user": "u", "password": "hunter2"}
            )

    def test_identity_file_is_expanded_not_read(self):
        from hermes_cli.migration_admin import validate_target

        got = validate_target(
            {"id": "a", "host": "h", "user": "u", "identity_file": "~/.ssh/id_ed25519"}
        )
        assert got["identity_file"].endswith("/.ssh/id_ed25519")
        assert not got["identity_file"].startswith("~"), "must be expanded"


class TestTargetsStore:
    def test_missing_file_reads_as_empty(self, tmp_path):
        from hermes_cli.migration_admin import load_targets

        assert load_targets(tmp_path / "nope.json") == {}

    def test_corrupt_file_reads_as_empty(self, tmp_path):
        from hermes_cli.migration_admin import load_targets

        # A hand-mangled store must not take down the whole page.
        p = tmp_path / "migration_targets.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_targets(p) == {}

    def test_round_trip(self, tmp_path):
        from hermes_cli.migration_admin import load_targets, save_targets, validate_target

        p = tmp_path / "migration_targets.json"
        prof = validate_target({"id": "prod", "host": "h", "user": "u"})
        save_targets(p, {"prod": prof})
        assert load_targets(p) == {"prod": prof}

    def test_store_is_written_0600(self, tmp_path):
        from hermes_cli.migration_admin import save_targets, validate_target

        p = tmp_path / "migration_targets.json"
        save_targets(p, {"prod": validate_target({"id": "prod", "host": "h", "user": "u"})})
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_saved_json_carries_no_secret_fields(self, tmp_path):
        from hermes_cli.migration_admin import save_targets, validate_target

        p = tmp_path / "migration_targets.json"
        save_targets(p, {"prod": validate_target(
            {"id": "prod", "host": "h", "user": "u", "identity_file": "/k/id"}
        )})
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "password" not in json.dumps(raw)
        assert raw["prod"]["identity_file"] == "/k/id", "path only, never key material"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_migration_targets.py
```

Expected: every test errors with `ModuleNotFoundError: No module named 'hermes_cli.migration_admin'`.

- [ ] **Step 3: Write the implementation**

Create `hermes_cli/migration_admin.py`:

```python
"""Rules for whole-instance migration to another host.

Pure functions over arguments, kept out of ``web_server.py`` so they can be
tested without an HTTP client and — via an injected executor in later tasks —
without a real remote machine.

SECURITY: a host profile stores a *path* to a private key, never key material
and never a password. The file is still 0600: the list of machines you can
reach, with usernames, is worth protecting on its own.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict

TARGETS_FILENAME = "migration_targets.json"

# Slug: becomes a dict key, a filename-safe token and a URL path segment.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def targets_path(home: Path) -> Path:
    """Location of the host-profile store inside a HERMES_HOME."""
    return Path(home) / TARGETS_FILENAME


def validate_target(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate one host profile.

    Raises ``ValueError`` naming the offending field. Callers map that to a 400.
    """
    if not isinstance(raw, dict):
        raise ValueError("target must be an object")

    if "password" in raw:
        raise ValueError(
            "password authentication is not supported: it would store a "
            "plaintext secret. Use an SSH key and set identity_file."
        )

    target_id = str(raw.get("id") or "").strip()
    if not _ID_RE.match(target_id):
        raise ValueError(
            "id must be lowercase alphanumeric with - or _ (got %r)" % target_id
        )

    host = str(raw.get("host") or "").strip()
    if not host:
        raise ValueError("host is required")

    user = str(raw.get("user") or "").strip()
    if not user:
        raise ValueError("user is required")

    port_raw = raw.get("port", 22)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        raise ValueError("port must be an integer (got %r)" % (port_raw,)) from None
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535 (got %d)" % port)

    identity_file = str(raw.get("identity_file") or "").strip()
    if identity_file:
        identity_file = os.path.expanduser(identity_file)

    target_home = str(raw.get("target_home") or "").strip()
    if target_home:
        target_home = os.path.expanduser(target_home)

    fingerprint = raw.get("host_fingerprint")
    return {
        "id": target_id,
        "label": str(raw.get("label") or "").strip() or target_id,
        "host": host,
        "user": user,
        "port": port,
        "identity_file": identity_file,
        "target_home": target_home,
        "host_fingerprint": str(fingerprint) if fingerprint else None,
        "last_preflight": raw.get("last_preflight"),
    }


def load_targets(path: Path) -> Dict[str, Dict[str, Any]]:
    """Read the store. Absent or corrupt reads as empty.

    A hand-mangled file must not take down the migration page; the operator can
    still add a fresh target and overwrite it.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_targets(path: Path, targets: Dict[str, Dict[str, Any]]) -> None:
    """Write the store at 0600, creating the parent directory if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(targets, indent=2, sort_keys=True) + "\n"
    # Create with restrictive permissions from the start rather than chmod-ing
    # after write, which would leave a window where the file is world-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
```

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/migration_admin.py tests/hermes_cli/test_migration_targets.py
git diff --cached --name-only   # MUST list exactly those two files
git commit -m "feat(migration): host profile model and store"
```

---

### Task 2a: SSH command construction and output parsing (no network)

Split from 2b deliberately: everything that decides *what* to run is pure and
testable in-process. Only actually running it needs a machine. A flaky network
must not block this half.

**Files:**
- Create: `hermes_cli/remote_exec.py`
- Test: `tests/hermes_cli/test_remote_exec.py`

**Interfaces:**
- Consumes: nothing from Task 1 (deliberately Hermes-agnostic).
- Produces:
  - `build_ssh_argv(profile: Dict[str, Any], command: str, *, batch: bool = True) -> List[str]`
  - `build_put_argv(profile: Dict[str, Any], remote_path: str) -> List[str]` — argv whose stdin receives the file bytes
  - `parse_os_probe(stdout: str) -> Dict[str, str]` — `{"os": ..., "arch": ...}`
  - `KNOWN_HOSTS_ENV: str` — env var name used to pin a fingerprint

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_remote_exec.py`:

```python
"""SSH argv construction and probe parsing.

No network here by design: what we decide to run is pure, so it stays testable
when the network is not. Task 2b covers real sshd behaviour.
"""

import pytest


PROFILE = {
    "host": "10.0.0.5",
    "user": "hermes",
    "port": 2222,
    "identity_file": "/keys/id_ed25519",
    "host_fingerprint": None,
}


class TestBuildSshArgv:
    def test_carries_user_host_and_port(self):
        from hermes_cli.remote_exec import build_ssh_argv

        argv = build_ssh_argv(PROFILE, "echo hi")
        assert argv[0] == "ssh"
        assert "hermes@10.0.0.5" in argv
        assert "-p" in argv and "2222" in argv
        assert argv[-1] == "echo hi"

    def test_uses_the_identity_file_when_given(self):
        from hermes_cli.remote_exec import build_ssh_argv

        argv = build_ssh_argv(PROFILE, "true")
        assert "-i" in argv
        assert "/keys/id_ed25519" in argv

    def test_omits_identity_flag_when_absent(self):
        from hermes_cli.remote_exec import build_ssh_argv

        argv = build_ssh_argv({**PROFILE, "identity_file": ""}, "true")
        assert "-i" not in argv

    def test_batch_mode_disables_interactive_prompts(self):
        from hermes_cli.remote_exec import build_ssh_argv

        # Without BatchMode a wrong key makes ssh sit waiting for a password
        # forever, which surfaces as a mysterious hang rather than an error.
        argv = build_ssh_argv(PROFILE, "true")
        assert "BatchMode=yes" in " ".join(argv)

    def test_unknown_fingerprint_accepts_new_host_key(self):
        from hermes_cli.remote_exec import build_ssh_argv

        # First contact: TOFU. Accept and record (the caller stores it).
        argv = build_ssh_argv({**PROFILE, "host_fingerprint": None}, "true")
        assert "StrictHostKeyChecking=accept-new" in " ".join(argv)

    def test_known_fingerprint_demands_a_strict_match(self):
        from hermes_cli.remote_exec import build_ssh_argv

        # Once pinned, a changed host key must fail hard. This channel carries
        # plaintext .env and auth.json; a silently-accepted new key is a
        # man-in-the-middle handing them over.
        argv = build_ssh_argv({**PROFILE, "host_fingerprint": "SHA256:abc"}, "true")
        joined = " ".join(argv)
        assert "StrictHostKeyChecking=yes" in joined
        assert "accept-new" not in joined


class TestBuildPutArgv:
    def test_streams_to_a_remote_path_without_scp(self):
        from hermes_cli.remote_exec import build_put_argv

        # The image has ssh but no scp/rsync, so transfer is `cat > path`
        # over a single ssh invocation fed from stdin.
        argv = build_put_argv(PROFILE, "/tmp/hermes-migration.zip")
        assert argv[0] == "ssh"
        assert "cat > '/tmp/hermes-migration.zip'" in argv[-1]

    def test_rejects_a_remote_path_with_a_quote(self):
        from hermes_cli.remote_exec import build_put_argv

        # The path is interpolated into a remote shell command.
        with pytest.raises(ValueError, match="path"):
            build_put_argv(PROFILE, "/tmp/eviL';rm -rf /;'")


class TestParseOsProbe:
    def test_parses_uname_output(self):
        from hermes_cli.remote_exec import parse_os_probe

        assert parse_os_probe("Linux x86_64\n") == {"os": "linux", "arch": "x86_64"}

    def test_recognises_macos(self):
        from hermes_cli.remote_exec import parse_os_probe

        assert parse_os_probe("Darwin arm64\n") == {"os": "macos", "arch": "arm64"}

    def test_recognises_windows_probe_output(self):
        from hermes_cli.remote_exec import parse_os_probe

        # Windows OpenSSH has no uname; the probe falls back to $env:OS.
        assert parse_os_probe("Windows_NT AMD64\n") == {
            "os": "windows",
            "arch": "AMD64",
        }

    def test_unrecognised_output_raises(self):
        from hermes_cli.remote_exec import parse_os_probe

        # Guessing here would pick the wrong installer.
        with pytest.raises(ValueError, match="could not identify"):
            parse_os_probe("")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_remote_exec.py
```

Expected: `ModuleNotFoundError: No module named 'hermes_cli.remote_exec'`.

- [ ] **Step 3: Write the implementation**

Create `hermes_cli/remote_exec.py`:

```python
"""SSH transport for migration. Knows SSH; knows nothing about Hermes.

The only IO boundary in the migration feature. Everything that decides *what*
to run lives in pure functions here so it stays testable without a machine;
:func:`run` and :func:`put_file` are the only parts that need one.

Shells out to the system ``ssh``. The runtime image ships ``ssh`` but not
``scp``/``rsync``, and no paramiko/asyncssh, so file transfer streams through a
single ssh invocation rather than depending on a second binary.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List

KNOWN_HOSTS_ENV = "HERMES_MIGRATION_KNOWN_HOSTS"

# Probe emitted on the remote side; parsed by parse_os_probe.
OS_PROBE_COMMAND = "uname -s -m 2>/dev/null || echo \"$env:OS $env:PROCESSOR_ARCHITECTURE\""


def _base_argv(profile: Dict[str, Any]) -> List[str]:
    argv: List[str] = ["ssh"]

    # BatchMode: without it, a wrong or missing key makes ssh wait forever on a
    # password prompt, which the caller sees as a hang rather than an error.
    argv += ["-o", "BatchMode=yes"]

    if profile.get("host_fingerprint"):
        # Pinned: a changed host key must fail hard rather than be accepted.
        argv += ["-o", "StrictHostKeyChecking=yes"]
    else:
        # First contact (TOFU). The caller records the fingerprint afterwards.
        argv += ["-o", "StrictHostKeyChecking=accept-new"]

    identity = str(profile.get("identity_file") or "").strip()
    if identity:
        argv += ["-i", identity]
        # Do not fall back to whatever keys an agent happens to offer: the
        # profile named a key, so a failure should say that key did not work.
        argv += ["-o", "IdentitiesOnly=yes"]

    port = int(profile.get("port") or 22)
    if port != 22:
        argv += ["-p", str(port)]

    argv.append(f"{profile['user']}@{profile['host']}")
    return argv


def build_ssh_argv(
    profile: Dict[str, Any], command: str, *, batch: bool = True
) -> List[str]:
    """argv that runs *command* on the remote host."""
    return _base_argv(profile) + [command]


def build_put_argv(profile: Dict[str, Any], remote_path: str) -> List[str]:
    """argv whose stdin is written to *remote_path* on the remote host."""
    if "'" in remote_path or "\\" in remote_path:
        raise ValueError(
            "remote path may not contain a quote or backslash: %r" % remote_path
        )
    return _base_argv(profile) + [f"cat > {shlex.quote(remote_path)}"]


def parse_os_probe(stdout: str) -> Dict[str, str]:
    """Turn the OS probe's output into ``{"os": ..., "arch": ...}``.

    Raises ``ValueError`` when unrecognised: guessing would choose the wrong
    installer, which is worse than refusing to proceed.
    """
    parts = (stdout or "").strip().split()
    if len(parts) < 2:
        raise ValueError("could not identify remote OS from probe output %r" % stdout)

    kernel, arch = parts[0], parts[1]
    lowered = kernel.lower()
    if lowered == "linux":
        os_name = "linux"
    elif lowered == "darwin":
        os_name = "macos"
    elif lowered.startswith("windows"):
        os_name = "windows"
    else:
        raise ValueError("could not identify remote OS from kernel %r" % kernel)

    return {"os": os_name, "arch": arch}
```

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/remote_exec.py tests/hermes_cli/test_remote_exec.py
git diff --cached --name-only   # MUST list exactly those two files
git commit -m "feat(migration): SSH argv construction and OS probe parsing"
```

---

### Task 2b: `run()` and `put_file()` against a real sshd

The only task in the plan that needs a live machine. Isolated so a network
problem blocks this task alone.

**Files:**
- Modify: `hermes_cli/remote_exec.py`
- Test: `tests/hermes_cli/test_remote_exec_integration.py`

**Interfaces:**
- Consumes: `build_ssh_argv`, `build_put_argv`, `parse_os_probe`, `OS_PROBE_COMMAND` (Task 2a).
- Produces:
  - `RemoteResult` — dataclass with `rc: int`, `stdout: str`, `stderr: str`
  - `SshExecutor` — class with `run(command: str, *, timeout: int = 60) -> RemoteResult`, `put_file(local: Path, remote_path: str, *, timeout: int = 1800) -> RemoteResult`, `detect_os() -> Dict[str, str]`
  - `SshError` — raised on transport failure (auth, host-key mismatch, unreachable), distinct from a non-zero remote exit code

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_remote_exec_integration.py`:

```python
"""SshExecutor against a real sshd.

Marked `integration` because it needs a container running sshd. Everything that
does not need a machine is in test_remote_exec.py and must stay there.

Start the fixture sshd with:
  docker run -d --rm --name hermes-sshd -p 2222:2222 \
    -e USER_NAME=hermes -e USER_PASSWORD= -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" \
    linuxserver/openssh-server
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SSHD_HOST = os.environ.get("HERMES_TEST_SSHD_HOST")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not SSHD_HOST, reason="HERMES_TEST_SSHD_HOST not set"),
]


@pytest.fixture
def profile():
    return {
        "host": SSHD_HOST,
        "user": os.environ.get("HERMES_TEST_SSHD_USER", "hermes"),
        "port": int(os.environ.get("HERMES_TEST_SSHD_PORT", "2222")),
        "identity_file": os.environ.get("HERMES_TEST_SSHD_KEY", ""),
        "host_fingerprint": None,
    }


def test_run_returns_stdout_and_zero_rc(profile):
    from hermes_cli.remote_exec import SshExecutor

    got = SshExecutor(profile).run("echo hello")
    assert got.rc == 0
    assert got.stdout.strip() == "hello"


def test_non_zero_remote_exit_is_a_result_not_an_exception(profile):
    from hermes_cli.remote_exec import SshExecutor

    # A command that fails is data the caller interprets (preflight checks rely
    # on this). Only transport failures raise.
    got = SshExecutor(profile).run("exit 3")
    assert got.rc == 3


def test_unreachable_host_raises_ssh_error(profile):
    from hermes_cli.remote_exec import SshError, SshExecutor

    with pytest.raises(SshError):
        SshExecutor({**profile, "host": "203.0.113.1", "port": 22}).run(
            "true", timeout=5
        )


def test_detect_os_identifies_the_container(profile):
    from hermes_cli.remote_exec import SshExecutor

    assert SshExecutor(profile).detect_os()["os"] == "linux"


def test_put_file_transfers_bytes_intact(profile, tmp_path):
    from hermes_cli.remote_exec import SshExecutor

    src = tmp_path / "payload.bin"
    src.write_bytes(b"\x00\x01binary\xff" * 1024)

    ex = SshExecutor(profile)
    ex.put_file(src, "/tmp/hermes-put-test.bin")
    got = ex.run("sha256sum /tmp/hermes-put-test.bin | cut -d' ' -f1")

    import hashlib

    assert got.stdout.strip() == hashlib.sha256(src.read_bytes()).hexdigest()
```

- [ ] **Step 2: Start the fixture and run the test to verify it fails**

```bash
docker run -d --rm --name hermes-sshd -p 2222:2222 \
  -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" -e USER_NAME=hermes \
  linuxserver/openssh-server

docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  -e HERMES_TEST_SSHD_HOST=127.0.0.1 -e HERMES_TEST_SSHD_KEY=/root/.ssh/id_ed25519 \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_remote_exec_integration.py
```

Expected: `ImportError: cannot import name 'SshExecutor'`.

- [ ] **Step 3: Write the implementation**

Append to `hermes_cli/remote_exec.py`:

```python
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SshError(RuntimeError):
    """Transport failure: unreachable, auth rejected, or host key mismatch.

    Deliberately distinct from a non-zero remote exit code. Preflight checks
    read exit codes as data ("is python3 installed?"); only a broken connection
    is exceptional.
    """


@dataclass
class RemoteResult:
    rc: int
    stdout: str
    stderr: str


# ssh's own exit code for "I could not establish the session". A remote command
# that genuinely exits 255 is indistinguishable, which is why callers should
# not rely on 255 as a normal command result.
_SSH_TRANSPORT_FAILURE = 255


class SshExecutor:
    """Runs commands and streams files to one host over SSH."""

    def __init__(self, profile: Dict[str, Any]):
        self.profile = profile

    def _invoke(self, argv: List[str], *, stdin_bytes=None, timeout: int) -> RemoteResult:
        try:
            proc = subprocess.run(
                argv,
                input=stdin_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"timed out after {timeout}s connecting to "
                           f"{self.profile.get('host')!r}") from exc
        except OSError as exc:
            raise SshError(f"could not launch ssh: {exc}") from exc

        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        if proc.returncode == _SSH_TRANSPORT_FAILURE:
            raise SshError(stderr.strip() or "ssh failed to establish a session")
        return RemoteResult(rc=proc.returncode, stdout=stdout, stderr=stderr)

    def run(self, command: str, *, timeout: int = 60) -> RemoteResult:
        return self._invoke(build_ssh_argv(self.profile, command), timeout=timeout)

    def put_file(self, local: Path, remote_path: str, *, timeout: int = 1800) -> RemoteResult:
        data = Path(local).read_bytes()
        return self._invoke(
            build_put_argv(self.profile, remote_path),
            stdin_bytes=data,
            timeout=timeout,
        )

    def detect_os(self) -> Dict[str, str]:
        return parse_os_probe(self.run(OS_PROBE_COMMAND).stdout)
```

Register the marker in `pytest.ini` / `pyproject.toml` if `integration` is not
already declared, so `-m "not integration"` works.

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `5 passed`. Then stop the fixture:
`docker stop hermes-sshd`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/remote_exec.py tests/hermes_cli/test_remote_exec_integration.py
git diff --cached --name-only
git commit -m "feat(migration): SshExecutor run/put_file/detect_os over real SSH"
```

---

### Task 3: Preflight checks

**Files:**
- Modify: `hermes_cli/migration_admin.py`
- Test: `tests/hermes_cli/test_migration_preflight.py`

**Interfaces:**
- Consumes: `SshExecutor` shape from Task 2b (`run(cmd) -> RemoteResult`, `detect_os() -> dict`). Tests inject a fake with the same shape — nothing imports `SshExecutor` here.
- Produces:
  - `CheckResult` — dataclass `name: str`, `tier: str` (`"blocking"` | `"warning"`), `ok: bool`, `detail: str`
  - `run_preflight(executor, *, target_home: str, archive_bytes: int, source_version: str) -> List[CheckResult]`
  - `preflight_blocks(results: List[CheckResult]) -> bool`
  - `HOME_STATE_MARKERS: Tuple[str, ...]` — files whose presence means "real user state"

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_migration_preflight.py`:

```python
"""Preflight verdicts, driven by a fake executor.

Preflight must never modify the target, so every check here is a read. The
two-tier split matters: blocking means "this migration cannot succeed",
warning means "this will probably work but you should look".
"""

import pytest


class FakeResult:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr


class FakeExecutor:
    """Answers commands from a dict of substring -> FakeResult."""

    def __init__(self, responses=None, os_info=None):
        self.responses = responses or {}
        self.os_info = os_info or {"os": "linux", "arch": "x86_64"}
        self.commands = []

    def run(self, command, timeout=60):
        self.commands.append(command)
        for needle, result in self.responses.items():
            if needle in command:
                return result
        return FakeResult(rc=0, stdout="")

    def detect_os(self):
        return self.os_info


def _by_name(results):
    return {r.name: r for r in results}


class TestPreflightBlocking:
    def test_passes_on_a_clean_empty_target(self):
        from hermes_cli.migration_admin import preflight_blocks, run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),   # plenty of free bytes
            "command -v python3": FakeResult(rc=0, stdout="/usr/bin/python3\n"),
            "ls -A": FakeResult(rc=0, stdout=""),      # target home empty
        })
        results = run_preflight(
            ex, target_home="/home/h/.hermes", archive_bytes=1000, source_version="0.19.0"
        )
        assert not preflight_blocks(results)

    def test_missing_python3_blocks(self):
        from hermes_cli.migration_admin import preflight_blocks, run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=1, stdout=""),
            "ls -A": FakeResult(rc=0, stdout=""),
        })
        results = run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        )
        assert preflight_blocks(results)
        assert _by_name(results)["python3"].tier == "blocking"

    def test_insufficient_space_blocks_and_requires_twice_the_archive(self):
        from hermes_cli.migration_admin import run_preflight

        # 1500 free vs a 1000-byte archive: enough to hold it once, not enough
        # to hold it while unpacking. The check is 2x for that reason.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="1500\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
        })
        disk = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["disk_space"]
        assert disk.ok is False
        assert disk.tier == "blocking"

    def test_pristine_target_home_passes(self):
        from hermes_cli.migration_admin import run_preflight

        # A target where Hermes was just installed has a config.yaml and
        # nothing else. Rejecting that would reject the whole "already
        # installed" case the feature exists to support.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout="config.yaml\n"),
        })
        home = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["target_home"]
        assert home.ok is True

    def test_target_home_with_real_state_blocks(self):
        from hermes_cli.migration_admin import run_preflight

        # auth.json means someone is logged in on that box. Overwriting it
        # destroys their instance, so it blocks until explicitly confirmed.
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout="config.yaml\nauth.json\nsessions\n"),
        })
        home = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["target_home"]
        assert home.ok is False
        assert home.tier == "blocking"


class TestPreflightWarnings:
    def test_clock_skew_warns_but_does_not_block(self):
        from hermes_cli.migration_admin import preflight_blocks, run_preflight

        # auth.json holds expiry-sensitive OAuth tokens: a skewed target shows
        # up as "login expired", which is near-impossible to diagnose after the
        # fact and trivial to detect now.
        import time

        skewed = int(time.time()) + 600
        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
            "date +%s": FakeResult(stdout=f"{skewed}\n"),
        })
        results = run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        )
        clock = _by_name(results)["clock_skew"]
        assert clock.ok is False
        assert clock.tier == "warning"
        assert not preflight_blocks(results), "a warning must not block"

    def test_version_mismatch_warns(self):
        from hermes_cli.migration_admin import run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
            "hermes version": FakeResult(rc=0, stdout="0.18.0\n"),
        })
        ver = _by_name(run_preflight(
            ex, target_home="/h", archive_bytes=1000, source_version="0.19.0"
        ))["hermes_version"]
        assert ver.tier == "warning"


class TestPreflightIsReadOnly:
    def test_no_check_mutates_the_target(self):
        from hermes_cli.migration_admin import run_preflight

        ex = FakeExecutor({
            "df ": FakeResult(stdout="100000000\n"),
            "command -v python3": FakeResult(rc=0),
            "ls -A": FakeResult(rc=0, stdout=""),
        })
        run_preflight(ex, target_home="/h", archive_bytes=1, source_version="0.19.0")
        forbidden = ("rm ", "mkdir", "mv ", "install", "cp ", ">", "chmod")
        for cmd in ex.commands:
            assert not any(f in cmd for f in forbidden), f"preflight mutated: {cmd}"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_migration_preflight.py
```

Expected: `ImportError: cannot import name 'run_preflight'`.

- [ ] **Step 3: Write the implementation**

Append to `hermes_cli/migration_admin.py`:

```python
import time
from dataclasses import dataclass
from typing import List, Tuple

# Presence of any of these in the target HERMES_HOME means real user state, as
# opposed to the config.yaml a fresh install writes on first run.
HOME_STATE_MARKERS: Tuple[str, ...] = (
    "auth.json",
    "sessions",
    "state.db",
    ".env",
    "skills",
)

_MAX_CLOCK_SKEW_SECONDS = 120


@dataclass
class CheckResult:
    name: str
    tier: str      # "blocking" | "warning"
    ok: bool
    detail: str


def preflight_blocks(results: List[CheckResult]) -> bool:
    """True when any blocking check failed."""
    return any(r.tier == "blocking" and not r.ok for r in results)


def run_preflight(
    executor,
    *,
    target_home: str,
    archive_bytes: int,
    source_version: str,
) -> List[CheckResult]:
    """Read-only checks against the target. Never modifies it.

    Every command here must be a read. ``TestPreflightIsReadOnly`` asserts that
    by inspecting the commands actually issued, because a preflight that
    mutates the target defeats the point of running it before you commit.
    """
    results: List[CheckResult] = []

    # --- OS identification (blocking: picks the installer) -------------------
    try:
        os_info = executor.detect_os()
        results.append(CheckResult("os", "blocking", True,
                                   f"{os_info['os']}/{os_info['arch']}"))
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        results.append(CheckResult("os", "blocking", False, str(exc)))
        os_info = {"os": "unknown", "arch": "unknown"}

    # --- python3 (blocking: the installer and hermes both need it) ----------
    py = executor.run("command -v python3")
    results.append(CheckResult(
        "python3", "blocking", py.rc == 0,
        py.stdout.strip() or "python3 not found on PATH",
    ))

    # --- free space (blocking) ----------------------------------------------
    # 2x the archive: it must fit alongside the unpacked result.
    needed = archive_bytes * 2
    df = executor.run(f"df -Pk {shlex.quote(target_home)} 2>/dev/null "
                      f"| awk 'NR==2 {{print $4 * 1024}}'")
    try:
        free = int((df.stdout or "0").strip() or 0)
    except ValueError:
        free = 0
    results.append(CheckResult(
        "disk_space", "blocking", free >= needed,
        f"{free} bytes free, need {needed}",
    ))

    # --- target home safety (blocking) --------------------------------------
    listing = executor.run(f"ls -A {shlex.quote(target_home)} 2>/dev/null")
    entries = [e for e in (listing.stdout or "").split() if e]
    conflicting = sorted(set(entries) & set(HOME_STATE_MARKERS))
    if not entries:
        detail, ok = "absent or empty", True
    elif not conflicting:
        detail, ok = f"pristine ({', '.join(entries)})", True
    else:
        detail, ok = (
            "contains existing state: " + ", ".join(conflicting)
            + " — confirm overwrite to proceed", False
        )
    results.append(CheckResult("target_home", "blocking", ok, detail))

    # --- clock skew (warning) -----------------------------------------------
    # auth.json carries expiry-sensitive OAuth tokens; a skewed target presents
    # as "login expired" rather than as a clock problem.
    remote_clock = executor.run("date +%s")
    try:
        skew = abs(int((remote_clock.stdout or "0").strip()) - int(time.time()))
        ok = skew <= _MAX_CLOCK_SKEW_SECONDS
        detail = f"{skew}s from source"
    except ValueError:
        ok, detail = True, "could not read remote clock"
    results.append(CheckResult("clock_skew", "warning", ok, detail))

    # --- existing hermes version (warning) ----------------------------------
    ver = executor.run("hermes version 2>/dev/null | head -1")
    remote_version = (ver.stdout or "").strip()
    if not remote_version:
        results.append(CheckResult("hermes_version", "warning", True,
                                   "not installed; will be installed"))
    else:
        results.append(CheckResult(
            "hermes_version", "warning", remote_version == source_version,
            f"target {remote_version} vs source {source_version}",
        ))

    return results
```

Add `import shlex` to the module imports.

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/migration_admin.py tests/hermes_cli/test_migration_preflight.py
git diff --cached --name-only
git commit -m "feat(migration): two-tier preflight checks"
```

---

### Task 4: Stage sequencing and failure classification

Pure logic only — what the stages are, which one leaves the source stopped, and
which installer a given OS needs. Actually running them is Task 5.

**Files:**
- Modify: `hermes_cli/migration_admin.py`
- Test: `tests/hermes_cli/test_migration_stages.py`

**Interfaces:**
- Consumes: `CheckResult`, `preflight_blocks` (Task 3).
- Produces:
  - `STAGES: Tuple[str, ...]` — ordered stage ids
  - `source_is_stopped_at(stage: str) -> bool`
  - `recovery_for(stage: str) -> str` — operator-facing recovery sentence
  - `install_command(os_info: Dict[str, str]) -> str`
  - `MigrationAborted(Exception)` with `.stage: str`

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_migration_stages.py`:

```python
"""Stage order, failure classification, installer selection.

The load-bearing property under test: the source is only ever stopped, never
modified. Everything an operator is told about recovery derives from knowing
whether a given stage runs before or after the stop.
"""

import pytest


class TestStageOrder:
    def test_install_precedes_stopping_the_source(self):
        from hermes_cli.migration_admin import STAGES

        # Install depends on no data and is the most failure-prone step
        # (network, dependencies, permissions). Running it while the source is
        # still serving makes that failure nearly free.
        assert STAGES.index("install") < STAGES.index("stop_source")

    def test_verify_is_last_and_start_is_absent(self):
        from hermes_cli.migration_admin import STAGES

        # The run halts at a verified-but-idle target; promotion is a human
        # decision, so there is deliberately no "start" stage.
        assert STAGES[-1] == "verify"
        assert "start" not in STAGES

    def test_expected_sequence(self):
        from hermes_cli.migration_admin import STAGES

        assert STAGES == (
            "install", "stop_source", "backup", "transfer", "restore", "verify",
        )


class TestFailureClassification:
    def test_source_still_running_before_the_stop(self):
        from hermes_cli.migration_admin import source_is_stopped_at

        assert source_is_stopped_at("install") is False

    def test_source_stopped_from_the_stop_onward(self):
        from hermes_cli.migration_admin import source_is_stopped_at

        for stage in ("stop_source", "backup", "transfer", "restore", "verify"):
            assert source_is_stopped_at(stage) is True, stage

    def test_recovery_text_before_stop_does_not_mention_restarting(self):
        from hermes_cli.migration_admin import recovery_for

        assert "restart" not in recovery_for("install").lower()

    def test_recovery_after_stop_tells_you_to_restart_the_source(self):
        from hermes_cli.migration_admin import recovery_for

        assert "restart" in recovery_for("transfer").lower()

    def test_restore_failure_warns_that_the_target_is_half_populated(self):
        from hermes_cli.migration_admin import recovery_for

        # hermes import overwrites, so a failed restore leaves a partial home
        # that must be cleared before retrying.
        text = recovery_for("restore").lower()
        assert "restart" in text
        assert "clear" in text or "empty" in text

    def test_unknown_stage_is_rejected(self):
        from hermes_cli.migration_admin import recovery_for

        with pytest.raises(ValueError, match="stage"):
            recovery_for("nonsense")


class TestInstallCommand:
    def test_linux_and_macos_use_the_shell_installer(self):
        from hermes_cli.migration_admin import install_command

        for os_name in ("linux", "macos"):
            cmd = install_command({"os": os_name, "arch": "x86_64"})
            assert "install.sh" in cmd

    def test_windows_uses_powershell(self):
        from hermes_cli.migration_admin import install_command

        cmd = install_command({"os": "windows", "arch": "AMD64"})
        assert "install.ps1" in cmd
        assert "powershell" in cmd.lower()

    def test_unknown_os_raises_rather_than_guessing(self):
        from hermes_cli.migration_admin import install_command

        with pytest.raises(ValueError, match="unsupported"):
            install_command({"os": "plan9", "arch": "x86"})


class TestMigrationAborted:
    def test_carries_the_stage_it_failed_at(self):
        from hermes_cli.migration_admin import MigrationAborted

        exc = MigrationAborted("transfer", "connection reset")
        assert exc.stage == "transfer"
        assert "connection reset" in str(exc)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_migration_stages.py
```

Expected: `ImportError: cannot import name 'STAGES'`.

- [ ] **Step 3: Write the implementation**

Append to `hermes_cli/migration_admin.py`:

```python
# Ordered stages. `install` precedes `stop_source` on purpose: it depends on no
# data and is the likeliest to fail, so it should fail while the source is
# still serving. There is no `start` stage — the run halts at a verified-but-
# idle target because promotion is a decision for a human.
STAGES: Tuple[str, ...] = (
    "install",
    "stop_source",
    "backup",
    "transfer",
    "restore",
    "verify",
)

_INSTALL_URL = "https://hermes-agent.nousresearch.com/install.sh"
_INSTALL_PS1_URL = "https://hermes-agent.nousresearch.com/install.ps1"


class MigrationAborted(Exception):
    """A stage failed. Carries the stage so the caller can classify recovery."""

    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def source_is_stopped_at(stage: str) -> bool:
    """True when the source gateway is already stopped by the time *stage* runs."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    return STAGES.index(stage) >= STAGES.index("stop_source")


def recovery_for(stage: str) -> str:
    """Operator-facing recovery sentence for a failure at *stage*.

    Derived from one invariant: the source is only ever stopped, never
    modified. So before the stop there is nothing to undo, and after it the
    remedy is always to start the source again.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")

    if not source_is_stopped_at(stage):
        return (
            "The source is still serving and the target was not modified. "
            "Fix the problem and run the migration again."
        )
    if stage == "restore":
        return (
            "The source is stopped but its data is intact — restart it to roll "
            "back. The target home is partially populated because import "
            "overwrites; clear it before retrying."
        )
    return (
        "The source is stopped but its data is intact — restart it to roll back."
    )


def install_command(os_info: Dict[str, Any]) -> str:
    """Command that installs Hermes on the target.

    Drives the project's existing installers rather than reimplementing
    per-platform setup.
    """
    os_name = str(os_info.get("os") or "").lower()
    if os_name in ("linux", "macos"):
        return f"curl -fsSL {_INSTALL_URL} | sh"
    if os_name == "windows":
        return (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            f"\"irm {_INSTALL_PS1_URL} | iex\""
        )
    raise ValueError(f"unsupported target OS {os_name!r}: no installer available")
```

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `13 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/migration_admin.py tests/hermes_cli/test_migration_stages.py
git diff --cached --name-only
git commit -m "feat(migration): stage order, failure classification, installer selection"
```

---

### Task 5: `hermes migrate host` CLI

Wires Tasks 1–4 together. This is the real implementation; the dashboard in
Task 6 only launches it.

**Files:**
- Modify: `hermes_cli/migrate.py`
- Modify: `hermes_cli/main.py:15604` (import) and `:15606-15637` (parser block)
- Test: `tests/hermes_cli/test_migrate_host_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4, plus `SshExecutor` (Task 2b).
- Produces:
  - `cmd_migrate_host(args) -> int`
  - `execute_migration(executor, profile, *, home: Path, confirm_overwrite: bool, emit) -> int` — `emit(stage, status, detail)` is the progress callback the dashboard's log tail reads

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_migrate_host_cli.py`:

```python
"""The migration runner, driven end to end against fakes.

No SSH and no subprocess: the executor is faked and the two subprocess calls
(`hermes backup` locally, `hermes import` remotely) are monkeypatched, so the
whole sequence is exercised in-process.
"""

from pathlib import Path

import pytest


class FakeResult:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr


class FakeExecutor:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on or {}
        self.commands = []
        self.puts = []

    def run(self, command, timeout=60):
        self.commands.append(command)
        for needle, rc in self.fail_on.items():
            if needle in command:
                return FakeResult(rc=rc, stderr=f"boom: {needle}")
        return FakeResult(rc=0, stdout="")

    def put_file(self, local, remote_path, timeout=1800):
        self.puts.append((Path(local).name, remote_path))
        return FakeResult(rc=0)

    def detect_os(self):
        return {"os": "linux", "arch": "x86_64"}


@pytest.fixture
def profile():
    return {
        "id": "prod", "label": "prod", "host": "h", "user": "u", "port": 22,
        "identity_file": "", "target_home": "/home/u/.hermes",
        "host_fingerprint": "SHA256:x", "last_preflight": None,
    }


@pytest.fixture
def fake_backup(monkeypatch, tmp_path):
    """Stand in for the `hermes backup` subprocess, producing a real file."""
    def _run(archive_path):
        Path(archive_path).write_bytes(b"PK\x03\x04fake-archive")
        return 0
    monkeypatch.setattr("hermes_cli.migrate._run_source_backup", _run)
    return _run


class TestHappyPath:
    def test_runs_every_stage_in_order(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration

        seen = []
        rc = execute_migration(
            FakeExecutor(), profile, home=tmp_path,
            confirm_overwrite=False,
            emit=lambda stage, status, detail: seen.append((stage, status)),
        )
        assert rc == 0
        started = [s for s, st in seen if st == "start"]
        assert started == ["install", "stop_source", "backup", "transfer",
                           "restore", "verify"]

    def test_does_not_start_the_target(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration

        ex = FakeExecutor()
        execute_migration(ex, profile, home=tmp_path, confirm_overwrite=False,
                          emit=lambda *a: None)
        joined = " ".join(ex.commands)
        assert "gateway run" not in joined
        assert "gateway start" not in joined


class TestArchiveHygiene:
    def test_local_archive_is_deleted_on_success(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration

        execute_migration(FakeExecutor(), profile, home=tmp_path,
                          confirm_overwrite=False, emit=lambda *a: None)
        assert list(tmp_path.glob("*.zip")) == []

    def test_local_archive_is_deleted_when_a_later_stage_fails(
        self, profile, fake_backup, tmp_path
    ):
        from hermes_cli.migrate import execute_migration

        # The archive holds plaintext .env and auth.json. It must not survive a
        # failure — this is why deletion is in a finally, not on the happy path.
        with pytest.raises(Exception):
            execute_migration(
                FakeExecutor(fail_on={"hermes import": 1}), profile,
                home=tmp_path, confirm_overwrite=False, emit=lambda *a: None,
            )
        assert list(tmp_path.glob("*.zip")) == []

    def test_remote_archive_is_removed(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration

        ex = FakeExecutor()
        execute_migration(ex, profile, home=tmp_path, confirm_overwrite=False,
                          emit=lambda *a: None)
        assert any(c.startswith("rm -f") for c in ex.commands), \
            "the plaintext archive must not be left on the target"


class TestFailures:
    def test_install_failure_aborts_before_stopping_the_source(
        self, profile, fake_backup, tmp_path
    ):
        from hermes_cli.migrate import execute_migration
        from hermes_cli.migration_admin import MigrationAborted

        ex = FakeExecutor(fail_on={"install.sh": 1})
        with pytest.raises(MigrationAborted) as err:
            execute_migration(ex, profile, home=tmp_path, confirm_overwrite=False,
                              emit=lambda *a: None)
        assert err.value.stage == "install"
        assert not any("gateway stop" in c for c in ex.commands), \
            "the source must still be serving when install fails"

    def test_restore_failure_reports_the_stage(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration
        from hermes_cli.migration_admin import MigrationAborted

        with pytest.raises(MigrationAborted) as err:
            execute_migration(
                FakeExecutor(fail_on={"hermes import": 1}), profile,
                home=tmp_path, confirm_overwrite=False, emit=lambda *a: None,
            )
        assert err.value.stage == "restore"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_migrate_host_cli.py
```

Expected: `ImportError: cannot import name 'execute_migration'`.

- [ ] **Step 3: Write the implementation**

Append to `hermes_cli/migrate.py`:

```python
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict

from hermes_cli.migration_admin import (
    MigrationAborted,
    STAGES,
    install_command,
    load_targets,
    recovery_for,
    targets_path,
)

_REMOTE_ARCHIVE = "/tmp/hermes-migration.zip"


def _run_source_backup(archive_path: str) -> int:
    """Produce a full backup at *archive_path* via a subprocess.

    Deliberately not an in-process call: ``run_backup()`` is a CLI entrypoint
    that calls ``sys.exit(1)`` on failure, which would kill this process and
    skip the archive cleanup in ``execute_migration``'s ``finally``.

    Full backup, not ``--quick``: quick captures only critical state and would
    silently drop skills/, memories, plans and projects — user content a
    migration must carry.
    """
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "backup", "-o", archive_path],
        check=False,
    ).returncode


def execute_migration(
    executor,
    profile: Dict[str, Any],
    *,
    home: Path,
    confirm_overwrite: bool,
    emit: Callable[[str, str, str], None],
) -> int:
    """Run every stage in order. Raises ``MigrationAborted`` on the first failure.

    The source is only ever stopped, never modified — that is what makes every
    failure recoverable by restarting it.
    """
    target_home = profile.get("target_home") or "~/.hermes"
    archive = Path(home) / f"hermes-migration-{os.getpid()}.zip"

    def _step(stage: str, detail: str = "") -> None:
        emit(stage, "start", detail)

    def _fail(stage: str, detail: str) -> None:
        emit(stage, "fail", f"{detail} — {recovery_for(stage)}")
        raise MigrationAborted(stage, detail)

    try:
        # 1. install (source untouched, still serving)
        _step("install")
        cmd = install_command(executor.detect_os())
        got = executor.run(f"command -v hermes >/dev/null 2>&1 || ({cmd})", timeout=1800)
        if got.rc != 0:
            _fail("install", got.stderr.strip() or "installer returned non-zero")
        emit("install", "ok", "")

        # 2. stop the source — downtime starts here
        _step("stop_source")
        subprocess.run([sys.executable, "-m", "hermes_cli.main", "gateway", "stop"],
                       check=False)
        emit("stop_source", "ok", "")

        # 3. backup
        _step("backup")
        if _run_source_backup(str(archive)) != 0 or not archive.is_file():
            _fail("backup", "hermes backup did not produce an archive")
        os.chmod(archive, 0o600)
        emit("backup", "ok", f"{archive.stat().st_size} bytes")

        # 4. transfer
        _step("transfer")
        put = executor.put_file(archive, _REMOTE_ARCHIVE)
        if put.rc != 0:
            _fail("transfer", put.stderr.strip() or "transfer failed")
        executor.run(f"chmod 600 {_REMOTE_ARCHIVE}")
        emit("transfer", "ok", "")

        # 5. restore
        _step("restore")
        flag = " --force" if confirm_overwrite else ""
        got = executor.run(f"hermes import{flag} {_REMOTE_ARCHIVE}", timeout=1800)
        if got.rc != 0:
            _fail("restore", got.stderr.strip() or "hermes import returned non-zero")
        emit("restore", "ok", "")

        # 6. verify — read-only assertions about what landed
        _step("verify")
        checks = executor.run(
            f"test -f {target_home}/config.yaml && "
            f"stat -c '%a' {target_home}/auth.json 2>/dev/null || echo missing"
        )
        emit("verify", "ok", checks.stdout.strip())
        return 0
    finally:
        # The archive holds plaintext .env and auth.json. Remove both copies on
        # every path, including failures.
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            executor.run(f"rm -f {_REMOTE_ARCHIVE}")
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            pass


def cmd_migrate_host(args: Any) -> int:
    """``hermes migrate host <id> [--preflight-only] [--confirm-overwrite]``."""
    from hermes_cli.config import get_default_hermes_root
    from hermes_cli.remote_exec import SshExecutor

    home = get_default_hermes_root()
    targets = load_targets(targets_path(home))
    profile = targets.get(args.target_id)
    if not profile:
        print(f"Unknown migration target: {args.target_id}", file=sys.stderr)
        return 2

    executor = SshExecutor(profile)

    def emit(stage: str, status: str, detail: str) -> None:
        print(f"[{stage}] {status} {detail}".rstrip(), flush=True)

    try:
        return execute_migration(
            executor, profile, home=home,
            confirm_overwrite=bool(getattr(args, "confirm_overwrite", False)),
            emit=emit,
        )
    except MigrationAborted as exc:
        print(f"Migration aborted at {exc.stage}: {exc.detail}", file=sys.stderr)
        print(recovery_for(exc.stage), file=sys.stderr)
        return 1
```

Extend `cmd_migrate`'s dispatcher:

```python
    if sub == "host":
        return cmd_migrate_host(args)
```

and its usage line to `usage: hermes migrate [xai|host] ...`.

In `hermes_cli/main.py`, change line 15604 to:

```python
    from hermes_cli.migrate import cmd_migrate, cmd_migrate_host, cmd_migrate_xai
```

and add after the `migrate_xai.set_defaults(...)` line:

```python
    migrate_host = migrate_subparsers.add_parser(
        "host",
        help="Migrate this whole instance to another host over SSH",
        description=(
            "Install Hermes on the target if needed, stop this instance, "
            "transfer its state, and restore it there. Halts at a verified "
            "but unstarted target; the source is only stopped, never modified."
        ),
    )
    migrate_host.add_argument("target_id", help="Host profile id")
    migrate_host.add_argument(
        "--confirm-overwrite",
        action="store_true",
        help="Overwrite existing state in the target's HERMES_HOME",
    )
    migrate_host.set_defaults(func=cmd_migrate_host)
```

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/migrate.py hermes_cli/main.py tests/hermes_cli/test_migrate_host_cli.py
git diff --cached --name-only
git commit -m "feat(migration): hermes migrate host runner"
```

---

### Task 6: Dashboard routes

**Files:**
- Modify: `hermes_cli/web_server.py` (`_ACTION_LOG_FILES` dict at line 3787; new routes)
- Test: `tests/hermes_cli/test_migration_api.py`

**Interfaces:**
- Consumes: `load_targets`, `save_targets`, `targets_path`, `validate_target`, `run_preflight`, `preflight_blocks` (Tasks 1, 3); `SshExecutor` (Task 2b); `_spawn_hermes_action` (existing, `web_server.py:3847`).
- Produces: six routes. Progress polling reuses the existing `/api/actions/{name}/status`.

| Route | Behaviour |
|---|---|
| `GET /api/migration/targets` | `{"targets": [...]}` |
| `POST /api/migration/targets` | Validate, reject duplicate id, save |
| `PUT /api/migration/targets/{id}` | Merge over existing, save |
| `DELETE /api/migration/targets/{id}` | Remove |
| `POST /api/migration/targets/{id}/preflight` | Run checks, store `last_preflight`, return results |
| `POST /api/migration/targets/{id}/migrate` | Spawn `hermes migrate host <id>`, return the action name |

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_migration_api.py`:

```python
"""The six migration routes.

Thin by construction: token check, profile scope, load/save, ValueError -> 400.
Anything with a rule in it lives in migration_admin and is tested there.
"""

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from starlette.testclient import TestClient
    from hermes_cli.web_server import app

    app.state.auth_required = False
    return TestClient(app)


@pytest.fixture
def token(client):
    from hermes_cli import web_server

    return {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}


class TestTargetsCrud:
    def test_list_is_empty_before_anything_is_added(self, client, token):
        got = client.get("/api/migration/targets", headers=token)
        assert got.status_code == 200
        assert got.json()["targets"] == []

    def test_create_then_list(self, client, token):
        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})
        rows = client.get("/api/migration/targets", headers=token).json()["targets"]
        assert [r["id"] for r in rows] == ["prod"]
        assert rows[0]["port"] == 22

    def test_duplicate_id_is_rejected(self, client, token):
        body = {"id": "prod", "host": "h", "user": "u"}
        client.post("/api/migration/targets", headers=token, json=body)
        again = client.post("/api/migration/targets", headers=token, json=body)
        assert again.status_code == 409

    def test_invalid_profile_is_400_not_500(self, client, token):
        got = client.post("/api/migration/targets", headers=token,
                          json={"id": "prod", "host": "h"})   # no user
        assert got.status_code == 400
        assert "user" in got.json()["detail"]

    def test_password_field_is_refused(self, client, token):
        got = client.post("/api/migration/targets", headers=token,
                          json={"id": "p", "host": "h", "user": "u",
                                "password": "hunter2"})
        assert got.status_code == 400
        assert "hunter2" not in got.text, "never echo a submitted secret back"

    def test_delete_removes_it(self, client, token):
        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})
        assert client.delete("/api/migration/targets/prod",
                             headers=token).status_code == 200
        assert client.get("/api/migration/targets",
                          headers=token).json()["targets"] == []

    def test_unknown_id_on_delete_is_404(self, client, token):
        assert client.delete("/api/migration/targets/nope",
                             headers=token).status_code == 404


class TestAuth:
    def test_every_route_requires_a_token(self, client):
        # These carry SSH connection details and trigger remote execution.
        assert client.get("/api/migration/targets").status_code == 401
        assert client.post("/api/migration/targets", json={}).status_code == 401
        assert client.delete("/api/migration/targets/x").status_code == 401
        assert client.post(
            "/api/migration/targets/x/preflight").status_code == 401
        assert client.post("/api/migration/targets/x/migrate").status_code == 401


class TestPreflightRoute:
    def test_returns_per_check_verdicts_and_stores_the_summary(
        self, client, token, monkeypatch
    ):
        from hermes_cli import migration_admin

        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        monkeypatch.setattr(
            "hermes_cli.web_server.SshExecutor",
            lambda profile: object(),
        )
        monkeypatch.setattr(
            migration_admin, "run_preflight",
            lambda *a, **k: [migration_admin.CheckResult("os", "blocking", True, "linux")],
        )
        got = client.post("/api/migration/targets/prod/preflight", headers=token)
        assert got.status_code == 200
        assert got.json()["checks"][0]["name"] == "os"
        assert got.json()["blocked"] is False

        rows = client.get("/api/migration/targets", headers=token).json()["targets"]
        assert rows[0]["last_preflight"] is not None


class TestMigrateRoute:
    def test_spawns_the_cli_action_and_returns_its_name(
        self, client, token, monkeypatch
    ):
        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        spawned = {}

        def fake_spawn(subcommand, name):
            spawned["subcommand"] = subcommand
            spawned["name"] = name
            return object()

        monkeypatch.setattr("hermes_cli.web_server._spawn_hermes_action", fake_spawn)
        got = client.post("/api/migration/targets/prod/migrate", headers=token)
        assert got.status_code == 200
        assert got.json()["action"] == "migrate-host"
        assert spawned["subcommand"][:3] == ["migrate", "host", "prod"]
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_migration_api.py
```

Expected: 404s everywhere — the routes do not exist.

- [ ] **Step 3: Write the implementation**

Add `"migrate-host": "action-migrate-host.log"` to `_ACTION_LOG_FILES` (line 3787).

Add near the other provider routes:

```python
from hermes_cli.migration_admin import (
    load_targets,
    preflight_blocks,
    run_preflight,
    save_targets,
    targets_path,
    validate_target,
)
from hermes_cli.remote_exec import SshExecutor


class MigrationTargetBody(BaseModel):
    id: Optional[str] = None
    label: Optional[str] = None
    host: Optional[str] = None
    user: Optional[str] = None
    port: Optional[int] = None
    identity_file: Optional[str] = None
    target_home: Optional[str] = None
    password: Optional[str] = None   # accepted only to reject it explicitly


def _targets_file() -> Path:
    from hermes_cli.config import get_default_hermes_root

    return targets_path(get_default_hermes_root())


@app.get("/api/migration/targets")
async def list_migration_targets(request: Request, profile: Optional[str] = None):
    _require_token(request)
    with _profile_scope(profile):
        targets = load_targets(_targets_file())
    return {"targets": [targets[k] for k in sorted(targets)]}


@app.post("/api/migration/targets")
async def create_migration_target(
    body: MigrationTargetBody, request: Request, profile: Optional[str] = None
):
    _require_token(request)
    with _profile_scope(profile):
        path = _targets_file()
        targets = load_targets(path)
        try:
            entry = validate_target(body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if entry["id"] in targets:
            raise HTTPException(
                status_code=409, detail=f"target {entry['id']} already exists"
            )
        targets[entry["id"]] = entry
        save_targets(path, targets)
    return {"ok": True, "target": entry}


@app.put("/api/migration/targets/{target_id}")
async def update_migration_target(
    target_id: str, body: MigrationTargetBody, request: Request,
    profile: Optional[str] = None,
):
    _require_token(request)
    with _profile_scope(profile):
        path = _targets_file()
        targets = load_targets(path)
        if target_id not in targets:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id}")
        merged = {**targets[target_id], **body.model_dump(exclude_none=True)}
        merged["id"] = target_id
        try:
            entry = validate_target(merged)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        targets[target_id] = entry
        save_targets(path, targets)
    return {"ok": True, "target": entry}


@app.delete("/api/migration/targets/{target_id}")
async def delete_migration_target(
    target_id: str, request: Request, profile: Optional[str] = None
):
    _require_token(request)
    with _profile_scope(profile):
        path = _targets_file()
        targets = load_targets(path)
        if target_id not in targets:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id}")
        targets.pop(target_id)
        save_targets(path, targets)
    return {"ok": True}


@app.post("/api/migration/targets/{target_id}/preflight")
async def preflight_migration_target(
    target_id: str, request: Request, profile: Optional[str] = None
):
    """Read-only checks. Never modifies the target."""
    _require_token(request)
    with _profile_scope(profile):
        from hermes_cli import migration_admin
        from hermes_cli.config import get_default_hermes_root

        path = _targets_file()
        targets = load_targets(path)
        entry = targets.get(target_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"unknown target {target_id}")

        home = get_default_hermes_root()
        approx = sum(f.stat().st_size for f in home.rglob("*") if f.is_file())
        try:
            checks = migration_admin.run_preflight(
                SshExecutor(entry),
                target_home=entry.get("target_home") or "~/.hermes",
                archive_bytes=approx,
                source_version=_hermes_version_string(),
            )
        except Exception as exc:  # noqa: BLE001 - transport failure is a result
            raise HTTPException(status_code=502, detail=str(exc))

        payload = [c.__dict__ for c in checks]
        entry["last_preflight"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "blocked": preflight_blocks(checks),
        }
        targets[target_id] = entry
        save_targets(path, targets)

    return {"checks": payload, "blocked": preflight_blocks(checks)}


@app.post("/api/migration/targets/{target_id}/migrate")
async def start_migration(
    target_id: str, request: Request, profile: Optional[str] = None,
    confirm_overwrite: bool = False,
):
    """Launch the CLI runner; the dashboard polls /api/actions/migrate-host/status."""
    _require_token(request)
    with _profile_scope(profile):
        if target_id not in load_targets(_targets_file()):
            raise HTTPException(status_code=404, detail=f"unknown target {target_id}")
        argv = ["migrate", "host", target_id]
        if confirm_overwrite:
            argv.append("--confirm-overwrite")
        _spawn_hermes_action(argv, "migrate-host")
    return {"ok": True, "action": "migrate-host"}
```

Use whatever helper `web_server.py` already has for the running version in place
of `_hermes_version_string()` if one exists.

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `12 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add hermes_cli/web_server.py tests/hermes_cli/test_migration_api.py
git diff --cached --name-only
git commit -m "feat(migration): dashboard routes for targets, preflight and launch"
```

---

### Task 7: Frontend pure logic

Everything with a rule in it goes here, because this repo's frontend tests cover
`lib/` only and there is no component-test setup. Logic placed in a component is
untestable, and that cannot be retrofitted later.

**Files:**
- Create: `web/src/lib/migration.ts`
- Test: `web/src/lib/migration.test.ts`

**Interfaces:**
- Produces:
  - `type PreflightTier = "blocking" | "warning"`
  - `interface PreflightCheck { name: string; tier: PreflightTier; ok: boolean; detail: string }`
  - `type MigrationStage = "install" | "stop_source" | "backup" | "transfer" | "restore" | "verify"`
  - `MIGRATION_STAGES: readonly MigrationStage[]`
  - `checkTone(c: PreflightCheck): "ok" | "warn" | "error"`
  - `isBlocked(checks: PreflightCheck[]): boolean`
  - `stageProgress(stage: MigrationStage | null, status: "start" | "ok" | "fail"): number`
  - `parseActionLine(line: string): { stage: MigrationStage; status: string; detail: string } | null`
  - `validateTargetDraft(d: Record<string, string>): string | null`

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/migration.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  MIGRATION_STAGES,
  checkTone,
  isBlocked,
  parseActionLine,
  stageProgress,
  validateTargetDraft,
} from "./migration";

describe("checkTone", () => {
  it("maps a failed blocking check to error", () => {
    expect(checkTone({ name: "python3", tier: "blocking", ok: false, detail: "" }))
      .toBe("error");
  });

  it("maps a failed warning check to warn, not error", () => {
    // A warning must never look like a stop: clock skew is worth seeing but
    // does not prevent the migration.
    expect(checkTone({ name: "clock_skew", tier: "warning", ok: false, detail: "" }))
      .toBe("warn");
  });

  it("maps any passing check to ok", () => {
    expect(checkTone({ name: "os", tier: "blocking", ok: true, detail: "" })).toBe("ok");
    expect(checkTone({ name: "clock_skew", tier: "warning", ok: true, detail: "" }))
      .toBe("ok");
  });
});

describe("isBlocked", () => {
  it("is true only when a blocking check failed", () => {
    expect(isBlocked([{ name: "a", tier: "warning", ok: false, detail: "" }])).toBe(false);
    expect(isBlocked([{ name: "b", tier: "blocking", ok: false, detail: "" }])).toBe(true);
  });

  it("is false for an empty list", () => {
    expect(isBlocked([])).toBe(false);
  });
});

describe("stageProgress", () => {
  it("reports 0 before anything starts", () => {
    expect(stageProgress(null, "start")).toBe(0);
  });

  it("increases monotonically across the stage order", () => {
    const seen = MIGRATION_STAGES.map((s) => stageProgress(s, "ok"));
    for (let i = 1; i < seen.length; i += 1) {
      expect(seen[i]).toBeGreaterThan(seen[i - 1]);
    }
  });

  it("reaches 100 only when the last stage completes", () => {
    expect(stageProgress("verify", "ok")).toBe(100);
    expect(stageProgress("verify", "start")).toBeLessThan(100);
  });

  it("does not advance past a failure", () => {
    expect(stageProgress("transfer", "fail")).toBe(stageProgress("transfer", "start"));
  });
});

describe("parseActionLine", () => {
  it("parses the CLI's emit format", () => {
    expect(parseActionLine("[transfer] ok 12345 bytes")).toEqual({
      stage: "transfer",
      status: "ok",
      detail: "12345 bytes",
    });
  });

  it("tolerates a missing detail", () => {
    expect(parseActionLine("[install] start")).toEqual({
      stage: "install",
      status: "start",
      detail: "",
    });
  });

  it("returns null for unrelated log noise", () => {
    // The log tail carries installer output too; only our own lines count.
    expect(parseActionLine("Downloading hermes...")).toBeNull();
    expect(parseActionLine("[not-a-stage] ok")).toBeNull();
  });
});

describe("validateTargetDraft", () => {
  it("accepts a minimal draft", () => {
    expect(validateTargetDraft({ id: "prod", host: "h", user: "u" })).toBeNull();
  });

  it("rejects a non-slug id", () => {
    expect(validateTargetDraft({ id: "Prod Box", host: "h", user: "u" })).toMatch(/id/);
  });

  it("requires host and user", () => {
    expect(validateTargetDraft({ id: "a", host: "", user: "u" })).toMatch(/host/);
    expect(validateTargetDraft({ id: "a", host: "h", user: "" })).toMatch(/user/);
  });

  it("rejects an out-of-range port", () => {
    expect(validateTargetDraft({ id: "a", host: "h", user: "u", port: "70000" }))
      .toMatch(/port/);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npx vitest run src/lib/migration.test.ts"
```

Expected: `Failed to resolve import "./migration"`.

- [ ] **Step 3: Write the implementation**

Create `web/src/lib/migration.ts`:

```ts
/** Pure mappings for the migration UI.
 *
 *  Lives in lib/ because this repo's frontend tests cover lib/ only — logic
 *  inside a component cannot be tested here at all. */

export type PreflightTier = "blocking" | "warning";

export interface PreflightCheck {
  name: string;
  tier: PreflightTier;
  ok: boolean;
  detail: string;
}

export type MigrationStage =
  | "install"
  | "stop_source"
  | "backup"
  | "transfer"
  | "restore"
  | "verify";

/** Same order as migration_admin.STAGES; install precedes stop_source. */
export const MIGRATION_STAGES: readonly MigrationStage[] = [
  "install",
  "stop_source",
  "backup",
  "transfer",
  "restore",
  "verify",
] as const;

const STAGE_SET = new Set<string>(MIGRATION_STAGES);

/** A failed warning is amber, never red: it does not stop the migration. */
export function checkTone(c: PreflightCheck): "ok" | "warn" | "error" {
  if (c.ok) return "ok";
  return c.tier === "blocking" ? "error" : "warn";
}

export function isBlocked(checks: PreflightCheck[]): boolean {
  return checks.some((c) => c.tier === "blocking" && !c.ok);
}

export function stageProgress(
  stage: MigrationStage | null,
  status: "start" | "ok" | "fail",
): number {
  if (!stage) return 0;
  const idx = MIGRATION_STAGES.indexOf(stage);
  if (idx < 0) return 0;
  const total = MIGRATION_STAGES.length;
  // A started-or-failed stage counts as reaching its own boundary; only "ok"
  // credits its completion, so a failure never appears to advance.
  const completed = status === "ok" ? idx + 1 : idx;
  return Math.round((completed / total) * 100);
}

export function parseActionLine(
  line: string,
): { stage: MigrationStage; status: string; detail: string } | null {
  const m = /^\[([a-z_]+)\]\s+(\S+)\s*(.*)$/.exec(line.trim());
  if (!m || !STAGE_SET.has(m[1])) return null;
  return { stage: m[1] as MigrationStage, status: m[2], detail: m[3] ?? "" };
}

const ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** Client-side mirror of validate_target. The server still validates; this
 *  exists so the form can refuse before a round trip. */
export function validateTargetDraft(d: Record<string, string>): string | null {
  if (!ID_RE.test(d.id ?? "")) {
    return "id must be lowercase alphanumeric with - or _";
  }
  if (!(d.host ?? "").trim()) return "host is required";
  if (!(d.user ?? "").trim()) return "user is required";
  if (d.port) {
    const port = Number(d.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return "port must be between 1 and 65535";
    }
  }
  return null;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `17 passed`.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add web/src/lib/migration.ts web/src/lib/migration.test.ts
git diff --cached --name-only
git commit -m "feat(migration): frontend preflight/stage mappings"
```

---

### Task 8: API client and i18n across all 17 locales

**The locale work is not optional and not cosmetic.** Every locale file is
declared `: Translations` (the full type). `define-locale.ts` exports a
deep-partial `TranslationOverrides`, but **no locale uses it**. Adding a key to
`en`/`zh` only makes `tsc -b` fail on the other 15 and the Docker image
unbuildable. Verify with `npm run build`; `npm run typecheck` passes regardless
because the root tsconfig is `{"files": [], "references": [...]}`.

**Files:**
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/i18n/types.ts`, `en.ts`, `zh.ts`
- Modify: `web/src/i18n/{af,ar,de,es,fr,ga,hu,it,ja,ko,pt,ru,tr,uk,zh-hant}.ts`
- Test: `web/src/lib/api.test.ts` (extend)

**Interfaces:**
- Consumes: `PreflightCheck` (Task 7); the six routes (Task 6).
- Produces on `api`: `listMigrationTargets()`, `createMigrationTarget(body)`, `updateMigrationTarget(id, body)`, `deleteMigrationTarget(id)`, `preflightMigrationTarget(id)`, `startMigration(id, confirmOverwrite)`; plus the `MigrationTarget` interface.

- [ ] **Step 1: Write the failing test**

Append to `web/src/lib/api.test.ts`:

```ts
describe("migration api", () => {
  it("posts a target to the collection route", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    globalThis.fetch = ((url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return Promise.resolve(
        new Response(JSON.stringify({ ok: true }), { status: 200 }),
      );
    }) as typeof fetch;

    await api.createMigrationTarget({ id: "prod", host: "h", user: "u" });
    expect(calls[0][0]).toContain("/api/migration/targets");
    expect(calls[0][1]?.method).toBe("POST");
  });

  it("encodes the id in per-target routes", async () => {
    const calls: string[] = [];
    globalThis.fetch = ((url: string) => {
      calls.push(url);
      return Promise.resolve(
        new Response(JSON.stringify({ ok: true }), { status: 200 }),
      );
    }) as typeof fetch;

    await api.deleteMigrationTarget("a b");
    expect(calls[0]).toContain("a%20b");
  });

  it("passes confirm_overwrite as a query parameter", async () => {
    const calls: string[] = [];
    globalThis.fetch = ((url: string) => {
      calls.push(url);
      return Promise.resolve(
        new Response(JSON.stringify({ action: "migrate-host" }), { status: 200 }),
      );
    }) as typeof fetch;

    await api.startMigration("prod", true);
    expect(calls[0]).toContain("confirm_overwrite=true");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npx vitest run src/lib/api.test.ts"
```

Expected: `api.createMigrationTarget is not a function`.

- [ ] **Step 3: Write the implementation**

Add to `web/src/lib/api.ts`:

```ts
export interface MigrationTarget {
  id: string;
  label: string;
  host: string;
  user: string;
  port: number;
  identity_file: string;
  target_home: string;
  host_fingerprint: string | null;
  last_preflight: { at: string; blocked: boolean } | null;
}
```

and inside the `api` object:

```ts
  listMigrationTargets: () =>
    fetchJSON<{ targets: MigrationTarget[] }>("/api/migration/targets"),

  createMigrationTarget: (body: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean; target: MigrationTarget }>("/api/migration/targets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  updateMigrationTarget: (id: string, body: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean; target: MigrationTarget }>(
      `/api/migration/targets/${encodeURIComponent(id)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),

  deleteMigrationTarget: (id: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/migration/targets/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),

  preflightMigrationTarget: (id: string) =>
    fetchJSON<{ checks: PreflightCheck[]; blocked: boolean }>(
      `/api/migration/targets/${encodeURIComponent(id)}/preflight`,
      { method: "POST" },
    ),

  startMigration: (id: string, confirmOverwrite = false) =>
    fetchJSON<{ ok: boolean; action: string }>(
      `/api/migration/targets/${encodeURIComponent(id)}/migrate` +
        `?confirm_overwrite=${confirmOverwrite ? "true" : "false"}`,
      { method: "POST" },
    ),
```

Import `PreflightCheck` from `./migration` and re-export it alongside the
existing `ProviderProxyState` re-export.

Add to `web/src/i18n/types.ts` inside the top-level translations interface:

```ts
  migration: {
    title: string;
    description: string;
    addTarget: string;
    editTarget: string;
    deleteTarget: string;
    fieldId: string;
    fieldLabel: string;
    fieldHost: string;
    fieldUser: string;
    fieldPort: string;
    fieldIdentityFile: string;
    fieldTargetHome: string;
    identityFileHint: string;
    preflight: string;
    preflightRunning: string;
    preflightBlocked: string;
    preflightPassed: string;
    tierBlocking: string;
    tierWarning: string;
    start: string;
    starting: string;
    confirmOverwrite: string;
    confirmOverwriteHint: string;
    stageInstall: string;
    stageStopSource: string;
    stageBackup: string;
    stageTransfer: string;
    stageRestore: string;
    stageVerify: string;
    doneTitle: string;
    doneNotStarted: string;
    doneStartCommand: string;
    doneResidualCredentials: string;
    fingerprintChanged: string;
  };
```

Add the English block to `en.ts` and **the same block, English strings, to all
15 other locale files**. `zh.ts` gets translated strings:

```ts
  migration: {
    title: "迁移到其他主机",
    description: "把这个实例整体搬到另一台机器。源实例只会被停止，不会被修改。",
    addTarget: "添加目标主机",
    editTarget: "编辑",
    deleteTarget: "删除",
    fieldId: "标识",
    fieldLabel: "名称",
    fieldHost: "主机地址",
    fieldUser: "用户名",
    fieldPort: "端口",
    fieldIdentityFile: "私钥路径",
    fieldTargetHome: "目标 HERMES_HOME",
    identityFileHint: "只保存路径，不保存密钥内容；不支持密码认证。",
    preflight: "预检",
    preflightRunning: "预检中…",
    preflightBlocked: "存在阻断项，无法迁移",
    preflightPassed: "预检通过",
    tierBlocking: "阻断",
    tierWarning: "警告",
    start: "开始迁移",
    starting: "迁移中…",
    confirmOverwrite: "覆盖目标机上已有的数据",
    confirmOverwriteHint: "目标 HERMES_HOME 中已有真实状态，继续会覆盖它。",
    stageInstall: "安装 Hermes",
    stageStopSource: "停止源实例",
    stageBackup: "打包状态",
    stageTransfer: "传输",
    stageRestore: "恢复",
    stageVerify: "验证",
    doneTitle: "迁移完成",
    doneNotStarted: "目标已就绪，但**尚未启动**。确认无误后手动启动。",
    doneStartCommand: "启动命令：",
    doneResidualCredentials:
      "源机器磁盘上仍保留完整凭据（.env、auth.json、state.db）。保留可用于回滚，如不需要请自行清理。",
    fingerprintChanged:
      "目标主机密钥已变更。这可能是重装，也可能是中间人。确认无误前不要继续。",
  },
```

- [ ] **Step 4: Verify — vitest, then the production build**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npx vitest run src/lib/api.test.ts && npm run build"
```

Expected: vitest passes, then `✓ built in …`. If `npm run build` reports
`TS2741: Property 'migration' is missing`, a locale file was missed — the error
names it.

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add web/src/lib/api.ts web/src/lib/api.test.ts web/src/i18n/
git diff --cached --name-only   # expect api.ts, api.test.ts and 18 i18n files
git commit -m "feat(migration): api client and i18n keys for every locale"
```

---

### Task 9: The `/migrate` page, nav entry, and moving backup off SystemPage

**Files:**
- Create: `web/src/pages/MigratePage.tsx`
- Modify: `web/src/App.tsx` (lazy import ~line 81-96; route map ~line 173; nav array ~line 192-218)
- Modify: `web/src/pages/SystemPage.tsx` (remove the backup/restore UI)

**Interfaces:**
- Consumes: everything from Tasks 7 and 8.
- Produces: the route `/migrate` and the nav entry.

No component test — this repo has none, and inventing a setup for one page is
out of scope. The page must therefore contain **no logic**: everything
decidable lives in `lib/migration.ts` and is already covered.

- [ ] **Step 1: Add the route and nav entry**

In `web/src/App.tsx`, beside the other lazy imports:

```tsx
const MigratePage = lazy(() => import("@/pages/MigratePage"));
```

In the route map beside `"/env": EnvPage`:

```tsx
  "/migrate": MigratePage,
```

In the nav array, after the `/env` entry:

```tsx
  { path: "/migrate", labelKey: "migration", label: "Backup & Migration", icon: HardDriveDownload },
```

Import `HardDriveDownload` from `lucide-react` alongside the existing icons.

Add `migration: "迁移与备份"` to the `nav` block in `zh.ts` and
`migration: "Backup & Migration"` to `en.ts` (plus the other 15 locales if the
`nav` block is typed — check `types.ts`).

- [ ] **Step 2: Build to confirm the nav change compiles**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npm run build"
```

Expected: fails with `Cannot find module '@/pages/MigratePage'`.

- [ ] **Step 3: Write the page**

Create `web/src/pages/MigratePage.tsx`. Structure — two sections, no logic:

```tsx
import { useCallback, useEffect, useState } from "react";

import { api, type MigrationTarget } from "@/lib/api";
import {
  checkTone,
  isBlocked,
  parseActionLine,
  stageProgress,
  validateTargetDraft,
  type MigrationStage,
  type PreflightCheck,
} from "@/lib/migration";
import { useI18n } from "@/i18n";

export default function MigratePage() {
  const { t } = useI18n();
  const [targets, setTargets] = useState<MigrationTarget[]>([]);
  const [checks, setChecks] = useState<PreflightCheck[]>([]);
  const [stage, setStage] = useState<MigrationStage | null>(null);
  const [stageStatus, setStageStatus] = useState<"start" | "ok" | "fail">("start");
  const [log, setLog] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    setTargets((await api.listMigrationTargets()).targets);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // While a migration runs, poll the shared action status endpoint and feed
  // each line through parseActionLine; unparseable lines are installer noise
  // and are shown in the log but do not move the progress bar.
  // ... polling effect ...

  return (
    <div className="flex flex-col gap-8">
      <section aria-label={t.migration.title}>
        {/* backup & restore controls moved from SystemPage */}
      </section>
      <section aria-label={t.migration.title}>
        {/* target list, preflight results via checkTone/isBlocked,
            progress via stageProgress(stage, stageStatus), log tail */}
      </section>
    </div>
  );
}
```

Render rules, all delegating to `lib/migration.ts`:

- each preflight row's colour comes from `checkTone(check)` — amber for a failed
  warning, red only for a failed blocking check
- the **Start migration** button is disabled when `isBlocked(checks)`
- when the blocked check is `target_home`, offer the `confirmOverwrite` checkbox
  (unchecked by default) and pass it to `api.startMigration(id, true)`
- the completion panel renders `t.migration.doneNotStarted`,
  `t.migration.doneStartCommand` and `t.migration.doneResidualCredentials`
- an `SshError` mentioning a host-key mismatch renders
  `t.migration.fingerprintChanged` rather than the raw stderr

- [ ] **Step 4: Move backup/restore out of SystemPage**

Cut the backup/restore JSX and its handlers from `web/src/pages/SystemPage.tsx`
into the first section of `MigratePage`. The API calls
(`/api/ops/backup`, `/api/ops/backup/download`, `/api/ops/import`,
`/api/ops/import-upload`) are unchanged — only where they are rendered moves.

- [ ] **Step 5: Verify build and lint**

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npm run build && npx eslint src/pages/MigratePage.tsx src/pages/SystemPage.tsx src/App.tsx"
```

Expected: build succeeds; eslint reports 0 errors. Warnings already present in
untouched files are the recorded baseline — do not "fix" them here.

- [ ] **Step 6: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add web/src/pages/MigratePage.tsx web/src/pages/SystemPage.tsx web/src/App.tsx web/src/i18n/
git diff --cached --name-only
git commit -m "feat(migration): /migrate page holding backup and migration"
```

---

### Task 10: Documentation and As-built

**Files:**
- Modify: `website/docs/user-guide/configuration.md` (or a new
  `website/docs/user-guide/migration.md` if the section outgrows a subsection)
- Modify: `docs/design/instance-migration.md` (append "As built")

- [ ] **Step 1: Write the user-facing guide**

Cover, in this order:

1. What migration does and what it deliberately does not — move, not clone; one
   live instance; the source is stopped, never modified.
2. Prerequisites: SSH key access to the target; **Windows targets need OpenSSH
   Server enabled**, which the tool does not install.
3. Adding a target, and that only a *path* to a key is stored — no key material,
   no password support.
4. Host-key pinning: the first preflight records the fingerprint; a later
   mismatch fails hard and needs a human decision, because this channel carries
   plaintext `.env` and `auth.json`.
5. Reading preflight: blocking versus warning, and specifically that clock skew
   is a warning because it surfaces later as "login expired" on the target.
6. What happens on failure, keyed to the stage — before the stop nothing needs
   undoing; after it, restart the source.
7. That the run stops at a ready-but-unstarted target on purpose.
8. That credentials remain on the source afterwards, and that clearing them is
   the operator's call.

- [ ] **Step 2: Append "As built" to the design doc**

Record every deviation found during implementation. If none, say so explicitly —
an "As built" that says "implemented as designed, with these three deltas" is
what the existing design docs in this repo do, and it is what makes them worth
reading later.

- [ ] **Step 3: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git add website/docs/user-guide/ docs/design/instance-migration.md
git diff --cached --name-only
git commit -m "docs: user guide and as-built notes for instance migration"
```

---

## Self-Review

**Spec coverage** — every section of `docs/design/instance-migration.md` maps to
a task: transport → 2a/2b; components → 1–6; host profiles → 1, 6; flow stages
→ 3, 4, 5; failure and recovery → 4, 5; UI placement/panels/routes/i18n/logic
location → 6–9; testing → every task's own steps plus 2b.

**Deliberately deferred, and why:** resumable transfer (YAGNI at this archive
size), erasing the source (non-goal — destroying user data is not a migrate
button's job), and a real nav submenu (rejected in the design; `NavItem` has no
children).

**Type consistency** — `CheckResult` (Python) and `PreflightCheck` (TS) carry
the same four fields; `STAGES` and `MIGRATION_STAGES` hold the same six ids in
the same order, and Task 7's `stageProgress` test asserts that order
independently. `emit(stage, status, detail)` in Task 5 produces exactly the
`[stage] status detail` format Task 7's `parseActionLine` consumes.

**Known gap left to the implementer:** Task 6 calls `_hermes_version_string()`
as a placeholder for whatever helper `web_server.py` already uses to report the
running version — substitute the real one rather than adding a duplicate.

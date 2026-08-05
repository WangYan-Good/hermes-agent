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
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

TARGETS_FILENAME = "migration_targets.json"
KNOWN_HOSTS_FILENAME = "migration_known_hosts"

# Slug: becomes a dict key, a filename-safe token and a URL path segment.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def targets_path(home: Path) -> Path:
    """Location of the host-profile store inside a HERMES_HOME."""
    return Path(home) / TARGETS_FILENAME


def known_hosts_path(home: Path) -> Path:
    """Host keys this feature has seen, kept separate from ``~/.ssh/known_hosts``.

    The operator's own file is shared with every other ssh use on the account
    and is routinely edited by hand, so a fingerprint read out of it proves
    nothing about what *this* feature saw on first contact.
    """
    return Path(home) / KNOWN_HOSTS_FILENAME


def executor_profile(entry: Dict[str, Any], home: Path) -> Dict[str, Any]:
    """A stored profile plus the connection settings the executor needs."""
    return {**entry, "known_hosts_file": str(known_hosts_path(home))}


def pin_fingerprint(entry: Dict[str, Any], executor) -> bool:
    """TOFU: record the host key seen on first contact.

    Returns True when *entry* gained a fingerprint, so the caller knows to
    persist it. An existing pin is never overwritten — absorbing a changed key
    on the next preflight is exactly the event that must reach a human, since
    this channel carries plaintext ``.env`` and ``auth.json``.
    """
    if entry.get("host_fingerprint"):
        return False
    seen = executor.recorded_fingerprint()
    if not seen:
        return False
    entry["host_fingerprint"] = seen
    return True


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
    # Walk up to nearest existing ancestor directory since target_home may not exist yet.
    # We check the filesystem the home will be created on.
    needed = archive_bytes * 2
    find_df_cmd = (
        f"d={shlex.quote(target_home)}; "
        f"while [ ! -d \"$d\" ] && [ \"$d\" != \"/\" ]; do "
        f"d=$(dirname \"$d\"); done; "
        f"df -Pk \"$d\" | awk 'NR==2 {{print $4 * 1024}}'"
    )
    df = executor.run(find_df_cmd)
    try:
        free = int((df.stdout or "0").strip() or 0)
    except ValueError:
        free = 0
    results.append(CheckResult(
        "disk_space", "blocking", free >= needed,
        f"{free} bytes free, need {needed}",
    ))

    # --- target home safety (blocking) --------------------------------------
    listing = executor.run(f"ls -A {shlex.quote(target_home)}")
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
    ver = executor.run("hermes version | head -1")
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

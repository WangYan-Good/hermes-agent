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

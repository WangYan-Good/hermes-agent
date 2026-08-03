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
    return _base_argv(profile) + [f"cat > '{remote_path}'"]


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

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
import subprocess
from dataclasses import dataclass
from pathlib import Path
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
# can also genuinely exit 255 on its own, so 255 alone is not proof of a
# transport failure -- see _looks_like_ssh_transport_failure below, which is
# what actually decides whether to raise.
_SSH_TRANSPORT_FAILURE = 255

# Substrings ssh itself writes to stderr when *it* fails to establish the
# session -- auth rejected, DNS failure, refused/timed-out connection, a
# reset mid-handshake, or a host-key mismatch. A remote command's own stderr
# won't contain these unless it is deliberately impersonating ssh, so their
# presence (alongside exit 255) is what distinguishes ssh's own failure from
# a remote command that merely happens to exit 255.
_SSH_FAILURE_SIGNATURES = (
    "permission denied",
    "could not resolve hostname",
    "connection refused",
    "connection timed out",
    "connection reset by peer",
    "kex_exchange_identification",
    "host key verification failed",
)


def _looks_like_ssh_transport_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(signature in lowered for signature in _SSH_FAILURE_SIGNATURES)


class SshExecutor:
    """Runs commands and streams files to one host over SSH."""

    def __init__(self, profile: Dict[str, Any]):
        self.profile = profile

    def _invoke(self, argv: List[str], *, stdin_file=None, timeout: int) -> RemoteResult:
        # stdin_file, when given, is an open binary file handed straight to
        # the child as its stdin so the OS pipes it directly -- put_file's
        # payload never gets read into a Python bytes object.
        try:
            proc = subprocess.Popen(
                argv,
                stdin=stdin_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise SshError(f"could not launch ssh: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise SshError(f"timed out after {timeout}s connecting to "
                           f"{self.profile.get('host')!r}") from exc

        stdout = stdout_bytes.decode("utf-8", "replace")
        stderr = stderr_bytes.decode("utf-8", "replace")
        if proc.returncode == _SSH_TRANSPORT_FAILURE and _looks_like_ssh_transport_failure(stderr):
            raise SshError(stderr.strip() or "ssh failed to establish a session")
        return RemoteResult(rc=proc.returncode, stdout=stdout, stderr=stderr)

    def run(self, command: str, *, timeout: int = 60) -> RemoteResult:
        return self._invoke(build_ssh_argv(self.profile, command), timeout=timeout)

    def put_file(self, local: Path, remote_path: str, *, timeout: int = 1800) -> RemoteResult:
        argv = build_put_argv(self.profile, remote_path)
        with open(local, "rb") as fh:
            return self._invoke(argv, stdin_file=fh, timeout=timeout)

    def detect_os(self) -> Dict[str, str]:
        return parse_os_probe(self.run(OS_PROBE_COMMAND).stdout)

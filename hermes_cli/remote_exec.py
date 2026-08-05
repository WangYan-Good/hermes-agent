"""SSH transport for migration. Knows SSH; knows nothing about Hermes.

The only IO boundary in the migration feature. Everything that decides *what*
to run lives in pure functions here so it stays testable without a machine;
:func:`run` and :func:`put_file` are the only parts that need one.

Shells out to the system ``ssh``. The runtime image ships ``ssh`` but not
``scp``/``rsync``, and no paramiko/asyncssh, so file transfer streams through a
single ssh invocation rather than depending on a second binary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

KNOWN_HOSTS_ENV = "HERMES_MIGRATION_KNOWN_HOSTS"

# Probe emitted on the remote side; parsed by parse_os_probe.
OS_PROBE_COMMAND = "uname -s -m 2>/dev/null || echo \"$env:OS $env:PROCESSOR_ARCHITECTURE\""


def known_hosts_file(profile: Dict[str, Any]) -> str:
    """Path of the known_hosts file this connection pins against, if any.

    Taken from the profile, or from ``HERMES_MIGRATION_KNOWN_HOSTS`` so an
    operator can point a one-off CLI run at the same file the dashboard uses.
    Empty means "ssh's own default", i.e. no per-profile pin.
    """
    explicit = str(profile.get("known_hosts_file") or "").strip()
    return explicit or os.environ.get(KNOWN_HOSTS_ENV, "").strip()


def _base_argv(profile: Dict[str, Any]) -> List[str]:
    argv: List[str] = ["ssh"]

    # BatchMode: without it, a wrong or missing key makes ssh wait forever on a
    # password prompt, which the caller sees as a hang rather than an error.
    argv += ["-o", "BatchMode=yes"]

    hosts_file = known_hosts_file(profile)
    if hosts_file:
        argv += ["-o", f"UserKnownHostsFile={hosts_file}"]
        # The pin is read back out of this file by host name, and a hashed
        # entry cannot be matched to a host without recomputing its HMAC.
        argv += ["-o", "HashKnownHosts=no"]

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


def _fingerprint_of(key_b64: str) -> str:
    """OpenSSH's SHA256 fingerprint of a base64 host-key blob.

    Same value ``ssh-keygen -lf`` prints: base64 of the SHA-256 digest with
    the padding stripped. Computed here rather than shelled out because
    ``ssh-keygen`` is not guaranteed to be in the image.
    """
    raw = base64.b64decode(key_b64, validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode(
        "ascii"
    ).rstrip("=")


def parse_known_hosts(text: str, host: str, port: int = 22) -> Optional[str]:
    """Fingerprint recorded for *host* in a known_hosts file, or None.

    OpenSSH writes ``[host]:port`` for a non-default port and a bare ``host``
    otherwise, so the two forms are not interchangeable. Hashed (``|1|…``)
    entries are skipped: they cannot be matched back to a host name, which is
    why the migration file is written with ``HashKnownHosts=no``.
    """
    wanted = f"[{host}]:{port}" if port != 22 else host
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "@")):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        if wanted not in parts[0].split(","):
            continue
        try:
            return _fingerprint_of(parts[2])
        except (ValueError, binascii.Error):
            continue
    return None


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

    def recorded_fingerprint(self) -> Optional[str]:
        """The host key this profile's known_hosts file currently holds."""
        path = known_hosts_file(self.profile)
        if not path:
            return None
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return parse_known_hosts(
            text,
            str(self.profile.get("host") or ""),
            int(self.profile.get("port") or 22),
        )

    def _verify_pin(self) -> None:
        """Refuse to connect when the recorded key is not the pinned one.

        ssh's own ``StrictHostKeyChecking=yes`` already rejects a changed key,
        but only against the file it is told to read. Checking here as well
        means a *tampered* known_hosts file — the one place a swapped key
        would otherwise look legitimate — is caught before any credential
        crosses the wire.
        """
        pinned = str(self.profile.get("host_fingerprint") or "").strip()
        if not pinned:
            return
        seen = self.recorded_fingerprint()
        if seen is None or seen == pinned:
            # Nothing recorded: ssh refuses on its own under
            # StrictHostKeyChecking=yes, and says so more precisely than we could.
            return
        raise SshError(
            f"host key verification failed for {self.profile.get('host')!r}: "
            f"pinned {pinned}, known_hosts now holds {seen}. This channel "
            f"carries plaintext .env and auth.json — resolve this by hand."
        )

    def _invoke(self, argv: List[str], *, stdin_file=None, timeout: int) -> RemoteResult:
        self._verify_pin()
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

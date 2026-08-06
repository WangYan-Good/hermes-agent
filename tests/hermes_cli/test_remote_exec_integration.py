"""SshExecutor against a real sshd.

Marked `integration` because it needs a container running sshd. Everything that
does not need a machine is in test_remote_exec.py and must stay there.

Start the fixture sshd with:
  docker run -d --rm --name hermes-sshd -p 2222:2222 \
    -e USER_NAME=hermes -e USER_PASSWORD= -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" \
    linuxserver/openssh-server

Note the port mapping: this image's sshd listens on 2222 *inside* the
container by default (it runs unprivileged and cannot bind port 22), so the
host and container sides of -p must both be 2222 -- `-p 2222:22` connects to
nothing and the connection gets reset before the SSH banner.
"""

import os
from pathlib import Path

import pytest

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


def test_first_contact_records_a_fingerprint_against_a_real_host_key(
    profile, tmp_path
):
    """TOFU, end to end: the pin is computed from a key a real sshd offered.

    The unit tests parse a hand-written known_hosts line; only this one proves
    the pin matches what OpenSSH actually writes for a live host.
    """
    from hermes_cli.remote_exec import SshExecutor

    known_hosts = tmp_path / "migration_known_hosts"
    ex = SshExecutor({**profile, "known_hosts_file": str(known_hosts)})

    assert ex.recorded_fingerprint() is None, "nothing pinned before first contact"
    assert ex.run("true").rc == 0

    pinned = ex.recorded_fingerprint()
    assert pinned and pinned.startswith("SHA256:")
    assert known_hosts.is_file(), "the pin must not land in ~/.ssh/known_hosts"


def test_a_pin_that_no_longer_matches_refuses_to_connect(profile, tmp_path):
    from hermes_cli.remote_exec import SshError, SshExecutor

    known_hosts = tmp_path / "migration_known_hosts"
    SshExecutor({**profile, "known_hosts_file": str(known_hosts)}).run("true")

    # Same host, same recorded key, but the profile insists on a different one:
    # the target was swapped, or something is sitting in between waiting to be
    # handed plaintext .env and auth.json.
    pinned_elsewhere = SshExecutor({
        **profile,
        "known_hosts_file": str(known_hosts),
        "host_fingerprint": "SHA256:definitely-not-the-key-this-host-offers",
    })
    with pytest.raises(SshError, match="host key"):
        pinned_elsewhere.run("true")


def test_a_matching_pin_connects_normally(profile, tmp_path):
    from hermes_cli.remote_exec import SshExecutor

    known_hosts = tmp_path / "migration_known_hosts"
    first = SshExecutor({**profile, "known_hosts_file": str(known_hosts)})
    first.run("true")

    pinned = SshExecutor({
        **profile,
        "known_hosts_file": str(known_hosts),
        "host_fingerprint": first.recorded_fingerprint(),
    })
    # StrictHostKeyChecking=yes now, so this also proves the recorded entry is
    # one ssh itself accepts -- not merely one we can parse.
    assert pinned.run("echo pinned").stdout.strip() == "pinned"

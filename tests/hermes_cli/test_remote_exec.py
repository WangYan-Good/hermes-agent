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


class TestKnownHostsPinning:
    """The pin lives in a migration-private known_hosts file.

    Not ~/.ssh/known_hosts: that file is shared with every other ssh use on
    the account and is routinely edited by hand, so a fingerprint read out of
    it proves nothing about what this feature actually saw on first contact.
    """

    def test_known_hosts_file_is_passed_to_ssh_unhashed(self):
        from hermes_cli.remote_exec import build_ssh_argv

        argv = build_ssh_argv(
            {**PROFILE, "known_hosts_file": "/state/migration_known_hosts"}, "true"
        )
        joined = " ".join(argv)
        assert "UserKnownHostsFile=/state/migration_known_hosts" in joined
        # A hashed entry cannot be matched back to a host without recomputing
        # its HMAC, and the pin is read back out of this file by host.
        assert "HashKnownHosts=no" in joined

    def test_fingerprint_matches_openssh_for_a_plain_entry(self):
        import base64
        import hashlib

        from hermes_cli.remote_exec import parse_known_hosts

        blob = b"\x00\x00\x00\x0bssh-ed25519" + b"key-material"
        b64 = base64.b64encode(blob).decode()
        expected = "SHA256:" + base64.b64encode(
            hashlib.sha256(blob).digest()
        ).decode().rstrip("=")

        got = parse_known_hosts(f"10.0.0.5 ssh-ed25519 {b64}\n", "10.0.0.5", 22)
        assert got == expected

    def test_matches_the_bracketed_form_used_for_a_non_default_port(self):
        import base64

        from hermes_cli.remote_exec import parse_known_hosts

        b64 = base64.b64encode(b"blob").decode()
        text = f"[10.0.0.5]:2222 ssh-ed25519 {b64}\n"
        assert parse_known_hosts(text, "10.0.0.5", 2222) is not None
        assert parse_known_hosts(text, "10.0.0.5", 22) is None

    def test_hashed_and_absent_entries_yield_nothing(self):
        from hermes_cli.remote_exec import parse_known_hosts

        assert parse_known_hosts("|1|abc=|def= ssh-ed25519 AAAA\n", "10.0.0.5") is None
        assert parse_known_hosts("other.host ssh-ed25519 AAAA\n", "10.0.0.5") is None

    def test_a_changed_host_key_fails_before_ssh_is_launched(
        self, monkeypatch, tmp_path
    ):
        import base64

        import hermes_cli.remote_exec as remote_exec
        from hermes_cli.remote_exec import SshError, SshExecutor

        known_hosts = tmp_path / "migration_known_hosts"
        known_hosts.write_text(
            "10.0.0.5 ssh-ed25519 %s\n" % base64.b64encode(b"a-different-key").decode()
        )

        def explode(*a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("ssh must not run once the pin fails")

        monkeypatch.setattr(remote_exec.subprocess, "Popen", explode)

        ex = SshExecutor({
            **PROFILE,
            "port": 22,
            "known_hosts_file": str(known_hosts),
            "host_fingerprint": "SHA256:something-else",
        })
        with pytest.raises(SshError, match="host key"):
            ex.run("true")

    def test_recorded_fingerprint_is_what_gets_pinned(self, tmp_path):
        import base64

        from hermes_cli.remote_exec import SshExecutor

        known_hosts = tmp_path / "migration_known_hosts"
        blob = b"first-contact-key"
        known_hosts.write_text(
            "10.0.0.5 ssh-ed25519 %s\n" % base64.b64encode(blob).decode()
        )

        ex = SshExecutor({
            **PROFILE, "port": 22, "known_hosts_file": str(known_hosts),
            "host_fingerprint": None,
        })
        got = ex.recorded_fingerprint()
        assert got and got.startswith("SHA256:")

    def test_a_matching_pin_does_not_block_the_connection(self, monkeypatch, tmp_path):
        import base64

        import hermes_cli.remote_exec as remote_exec
        from hermes_cli.remote_exec import SshExecutor

        known_hosts = tmp_path / "migration_known_hosts"
        known_hosts.write_text(
            "10.0.0.5 ssh-ed25519 %s\n" % base64.b64encode(b"the-key").decode()
        )
        ex = SshExecutor({
            **PROFILE, "port": 22, "known_hosts_file": str(known_hosts),
        })
        ex.profile["host_fingerprint"] = ex.recorded_fingerprint()

        monkeypatch.setattr(
            remote_exec.subprocess, "Popen",
            lambda *a, **k: _FakeProc(0, stdout=b"hi\n"),
        )
        assert ex.run("echo hi").rc == 0


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


class _FakeProc:
    """Stand-in for the Popen object `_invoke` gets back from subprocess.Popen."""

    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr


class TestInvokeExitCode255:
    """A remote command that exits 255 on its own must not be mistaken for
    ssh's own "could not establish the session" exit code. Only stderr text
    that looks like ssh's own failure output should raise; real sshd
    behaviour for genuine transport failures is covered by the integration
    suite (test_unreachable_host_raises_ssh_error), which stays fixture-only.
    """

    def _invoke_against(self, monkeypatch, *, stdout=b"", stderr=b""):
        import hermes_cli.remote_exec as remote_exec

        fake_proc = _FakeProc(255, stdout=stdout, stderr=stderr)
        monkeypatch.setattr(
            remote_exec.subprocess, "Popen", lambda *a, **k: fake_proc
        )
        return remote_exec.SshExecutor(PROFILE)._invoke(["ssh", "true"], timeout=5)

    def test_255_without_an_ssh_failure_signature_is_a_normal_result(self, monkeypatch):
        # The remote command itself chose to exit 255; that is data for the
        # caller (e.g. a preflight check), not a broken connection.
        got = self._invoke_against(
            monkeypatch, stderr=b"my-script: unexpected condition, aborting\n"
        )
        assert got.rc == 255
        assert got.stderr.strip() == "my-script: unexpected condition, aborting"

    def test_255_with_an_ssh_failure_signature_raises(self, monkeypatch):
        from hermes_cli.remote_exec import SshError

        with pytest.raises(SshError):
            self._invoke_against(
                monkeypatch, stderr=b"Permission denied (publickey).\n"
            )

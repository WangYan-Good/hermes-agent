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

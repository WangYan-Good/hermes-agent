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


class TestStopVerification:
    def test_aborts_if_gateway_is_still_alive_after_a_successful_looking_stop(
        self, profile, tmp_path, monkeypatch
    ):
        """`hermes gateway stop` can exit 0 without the process actually
        having died (stop_profile_gateway() unconditionally returns True
        after its own bounded wait). execute_migration must independently
        verify liveness and refuse to back up a still-running source."""
        from hermes_cli.migrate import execute_migration
        from hermes_cli.migration_admin import MigrationAborted

        monkeypatch.setattr("hermes_cli.migrate._stop_source_gateway", lambda: 0)
        monkeypatch.setattr("hermes_cli.migrate._gateway_still_running", lambda: True)
        monkeypatch.setattr("hermes_cli.migrate._STOP_VERIFY_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("hermes_cli.migrate._STOP_VERIFY_POLL_INTERVAL_SECONDS", 0.01)

        backup_calls = []
        monkeypatch.setattr(
            "hermes_cli.migrate._run_source_backup",
            lambda archive_path: backup_calls.append(archive_path) or 0,
        )

        ex = FakeExecutor()
        with pytest.raises(MigrationAborted) as err:
            execute_migration(ex, profile, home=tmp_path, confirm_overwrite=False,
                              emit=lambda *a: None)

        assert err.value.stage == "stop_source"
        assert backup_calls == [], \
            "must not back up while the source is still detectably running"


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


class TestVerifyStage:
    """Verification must be able to fail.

    The first implementation ran `test -f ... && stat -c ...` and then emitted
    `verify ok` regardless of the result, so a missing config or a
    world-readable auth.json completed the migration silently. It also quoted
    the target home, which meant a literal `~/.hermes` was never expanded.
    """

    def _verify_command(self, ex):
        return next((c for c in ex.commands if "python3 -c" in c), None)

    def test_a_failed_check_aborts_at_verify(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration
        from hermes_cli.migration_admin import MigrationAborted

        ex = FakeExecutor(fail_on={"python3 -c": 1})
        with pytest.raises(MigrationAborted) as err:
            execute_migration(ex, profile, home=tmp_path, confirm_overwrite=False,
                              emit=lambda *a: None)
        assert err.value.stage == "verify"

    def test_verification_runs_on_the_target_through_python3(
        self, profile, fake_backup, tmp_path
    ):
        from hermes_cli.migrate import execute_migration

        # python3 is a blocking preflight check, so it is guaranteed present —
        # and unlike `stat`, it behaves the same on Linux and macOS.
        ex = FakeExecutor()
        execute_migration(ex, profile, home=tmp_path, confirm_overwrite=False,
                          emit=lambda *a: None)
        cmd = self._verify_command(ex)
        assert cmd is not None, "verify must actually check something"
        assert "config.yaml" in cmd
        assert "auth.json" in cmd
        assert "sqlite3" in cmd, "every SQLite store must open"

    def test_the_target_home_is_expanded_not_quoted_into_a_literal_tilde(
        self, profile, fake_backup, tmp_path
    ):
        from hermes_cli.migrate import execute_migration

        ex = FakeExecutor()
        execute_migration(ex, {**profile, "target_home": "~/.hermes"}, home=tmp_path,
                          confirm_overwrite=False, emit=lambda *a: None)
        cmd = self._verify_command(ex)
        assert "expanduser" in cmd, \
            "a quoted ~ never expands; the script must expand it remotely"


class TestTargetHomeIsHonoured:
    """`target_home` must reach the restore, not just the checks.

    The field is labelled "Target HERMES_HOME" in the dashboard and both
    preflight and verify use it — but the restore ran a bare `hermes import`,
    so the archive landed in whatever home the *target's* environment
    defaulted to. Anyone setting a non-default target home got their data
    restored somewhere else entirely.
    """

    def test_the_restore_runs_against_the_configured_home(
        self, profile, fake_backup, tmp_path
    ):
        from hermes_cli.migrate import execute_migration

        ex = FakeExecutor()
        execute_migration(ex, {**profile, "target_home": "/srv/hermes-home"},
                          home=tmp_path, confirm_overwrite=False,
                          emit=lambda *a: None)
        restore = next(c for c in ex.commands if "hermes import" in c)
        assert "HERMES_HOME=/srv/hermes-home" in restore

    def test_a_home_with_a_space_is_quoted(self, profile, fake_backup, tmp_path):
        from hermes_cli.migrate import execute_migration

        ex = FakeExecutor()
        execute_migration(ex, {**profile, "target_home": "/srv/two words"},
                          home=tmp_path, confirm_overwrite=False,
                          emit=lambda *a: None)
        restore = next(c for c in ex.commands if "hermes import" in c)
        assert "'/srv/two words'" in restore

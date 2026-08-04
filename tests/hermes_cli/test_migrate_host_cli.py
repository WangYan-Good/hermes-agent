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

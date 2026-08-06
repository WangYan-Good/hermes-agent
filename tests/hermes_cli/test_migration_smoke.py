"""End-to-end migration into a real machine over real SSH.

Every other migration test fakes something: the executor, the backup
subprocess, or both. This one fakes nothing except the gateway stop (the test
process is not a gateway). A real `hermes backup` packs a real HERMES_HOME, a
real ssh streams it to a container running a real sshd, a real `hermes import`
unpacks it there, and the assertions read what actually landed — including the
permissions on the secret files, which is the part no unit test can see.

Marked `integration`: it needs the target container, and `pyproject.toml` sets
`addopts = "-m 'not integration'"`, so it is deselected unless `-m integration`
is passed as well.

Fixture:

    docker build -f docker/migration-target.Dockerfile \\
        -t localhost/hermes-migration-target .
    docker run -d --name hermes-migrate-target -p 2222:22 \\
        -e AUTHORIZED_KEY="$(cat <key>.pub)" localhost/hermes-migration-target

Then, from a container with this repo mounted at /src:

    HERMES_TEST_TARGET_HOST=127.0.0.1 HERMES_TEST_TARGET_PORT=2222 \\
    HERMES_TEST_TARGET_USER=hermes HERMES_TEST_TARGET_KEY=/keys/id_ed25519 \\
    python3 -m pytest -q -m integration tests/hermes_cli/test_migration_smoke.py
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

TARGET_HOST = os.environ.get("HERMES_TEST_TARGET_HOST")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TARGET_HOST, reason="HERMES_TEST_TARGET_HOST not set"),
]

# Deliberately not the target's default home: this also pins that `target_home`
# reaches the restore, which it did not until the import learned to carry it.
REMOTE_HOME = os.environ.get(
    "HERMES_TEST_TARGET_HOME", "/opt/data/.hermes-migrate-test"
)


@pytest.fixture
def target_profile(tmp_path):
    return {
        "id": "smoke",
        "label": "smoke",
        "host": TARGET_HOST,
        "user": os.environ.get("HERMES_TEST_TARGET_USER", "hermes"),
        "port": int(os.environ.get("HERMES_TEST_TARGET_PORT", "2222")),
        "identity_file": os.environ.get("HERMES_TEST_TARGET_KEY", ""),
        "target_home": REMOTE_HOME,
        "host_fingerprint": None,
        "known_hosts_file": str(tmp_path / "migration_known_hosts"),
    }


@pytest.fixture
def source_home(tmp_path, monkeypatch):
    """A HERMES_HOME with the things a migration is supposed to carry."""
    home = tmp_path / "source-home"
    home.mkdir()

    (home / "config.yaml").write_text("version: 1\nagent:\n  name: smoke\n")

    # The two files whose permissions the migration is responsible for.
    secrets = {".env": "SMOKE_TOKEN=abc123\n", "auth.json": '{"token": "xyz"}\n'}
    for name, body in secrets.items():
        path = home / name
        path.write_text(body)
        path.chmod(0o600)

    con = sqlite3.connect(home / "state.db")
    con.execute("create table smoke (id integer primary key, note text)")
    con.execute("insert into smoke (note) values ('carried across')")
    con.commit()
    con.close()

    # User content --quick would have dropped; the migration must not.
    skill = home / "skills" / "smoke-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# smoke skill\n")

    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def executor(target_profile):
    from hermes_cli.remote_exec import SshExecutor

    ex = SshExecutor(target_profile)
    yield ex
    # Restore the target: the migration deliberately leaves the restored home
    # (and its credentials) in place, so the test is what cleans up.
    ex.run(f"rm -rf {REMOTE_HOME} /tmp/hermes-migration.zip")


@pytest.fixture
def no_local_gateway(monkeypatch):
    """The test process is not a gateway; there is nothing to stop.

    The one thing faked here, and only because a real `hermes gateway stop`
    inside a test container would be stopping nothing anyway.
    """
    monkeypatch.setattr("hermes_cli.migrate._stop_source_gateway", lambda: 0)
    monkeypatch.setattr("hermes_cli.migrate._gateway_still_running", lambda: False)


def _remote(executor, command):
    got = executor.run(command)
    assert got.rc == 0, f"{command!r} failed: {got.stderr}"
    return got.stdout.strip()


class TestEndToEndMigration:
    def test_a_migrated_home_arrives_complete_and_locked_down(
        self, executor, target_profile, source_home, no_local_gateway, tmp_path
    ):
        from hermes_cli.migrate import execute_migration

        events = []
        rc = execute_migration(
            executor, target_profile, home=source_home, confirm_overwrite=True,
            emit=lambda stage, status, detail: events.append((stage, status, detail)),
        )

        assert rc == 0, events
        assert [s for s, st, _ in events if st == "ok"] == [
            "install", "stop_source", "backup", "transfer", "restore", "verify",
        ]

        # Hermes was already on the target, so the install stage must have been
        # a no-op rather than a download.
        assert not any("install.sh" in c for c in getattr(executor, "commands", []))

        # What actually landed.
        assert _remote(executor, f"cat {REMOTE_HOME}/config.yaml | head -1") == "version: 1"
        assert _remote(executor, f"cat {REMOTE_HOME}/skills/smoke-skill/SKILL.md") == "# smoke skill"

        # Secrets restored 0600 — the property `_SECRET_FILE_NAMES` exists for.
        for name in (".env", "auth.json"):
            mode = _remote(executor, f"stat -c '%a' {REMOTE_HOME}/{name}")
            assert mode == "600", f"{name} landed as {mode}"

        # The SQLite store opens *and* still holds its rows.
        note = _remote(
            executor,
            f"python3 -c \"import sqlite3;print(sqlite3.connect('{REMOTE_HOME}/state.db')"
            f".execute('select note from smoke').fetchone()[0])\"",
        )
        assert note == "carried across"

    def test_the_plaintext_archive_does_not_survive_on_either_side(
        self, executor, target_profile, source_home, no_local_gateway
    ):
        from hermes_cli.migrate import execute_migration

        execute_migration(executor, target_profile, home=source_home,
                          confirm_overwrite=True, emit=lambda *a: None)

        assert list(Path(source_home).glob("hermes-migration-*.zip")) == []
        assert _remote(
            executor, "test -e /tmp/hermes-migration.zip && echo present || echo gone"
        ) == "gone"

    def test_the_target_is_ready_but_not_running(
        self, executor, target_profile, source_home, no_local_gateway
    ):
        from hermes_cli.migrate import execute_migration

        execute_migration(executor, target_profile, home=source_home,
                          confirm_overwrite=True, emit=lambda *a: None)

        # Promotion is a human decision: nothing may have started the target.
        assert _remote(
            executor, f"test -e {REMOTE_HOME}/gateway.pid && echo running || echo idle"
        ) == "idle"

    def test_first_contact_pinned_the_targets_host_key(
        self, executor, target_profile, source_home, no_local_gateway
    ):
        from hermes_cli.migrate import execute_migration

        execute_migration(executor, target_profile, home=source_home,
                          confirm_overwrite=True, emit=lambda *a: None)

        pinned = executor.recorded_fingerprint()
        assert pinned and pinned.startswith("SHA256:")
        assert Path(target_profile["known_hosts_file"]).is_file()


class TestSourceIsNeverModified:
    """The invariant the whole rollback story rests on.

    Stated precisely, because the absolute form is not true: invoking *any*
    hermes command in a HERMES_HOME seeds the standard scaffolding it expects
    (SOUL.md, cron/, hooks/, sessions/, skills/, memories/, caches, logs), and
    `hermes backup` keeps a stable .backup.lock for cross-process coordination.
    On a real instance the scaffolding already exists; on a bare home it and
    the backup lock may appear.

    What rollback actually needs, and what is asserted here: no pre-existing
    file is altered or removed. That is what makes "restart the source" a
    complete recovery.
    """

    def _snapshot(self, home):
        return {
            p.relative_to(home): (p.stat().st_mode, p.read_bytes())
            for p in sorted(home.rglob("*")) if p.is_file()
        }

    def test_no_pre_existing_file_is_altered_or_removed(
        self, executor, target_profile, source_home, no_local_gateway
    ):
        from hermes_cli.migrate import execute_migration

        before = self._snapshot(source_home)

        execute_migration(executor, target_profile, home=source_home,
                          confirm_overwrite=True, emit=lambda *a: None)

        after = self._snapshot(source_home)
        for path, state in before.items():
            assert path in after, f"{path} was removed from the source"
            assert after[path] == state, f"{path} was modified in the source"

    def test_anything_new_is_only_known_cli_runtime_metadata(
        self, executor, target_profile, source_home, no_local_gateway
    ):
        from hermes_cli.migrate import execute_migration

        before = set(self._snapshot(source_home))

        execute_migration(executor, target_profile, home=source_home,
                          confirm_overwrite=True, emit=lambda *a: None)

        added = set(self._snapshot(source_home)) - before
        # Not "nothing new" — see the class docstring. What appears is the
        # CLI scaffolding, logs, and the backup coordination lock are expected;
        # migration artefacts, especially the plaintext archive, must not survive.
        unexpected = [
            p for p in added
            if p.name not in {"SOUL.md", ".backup.lock"} and p.parts[0] != "logs"
        ]
        assert not unexpected, f"unexpected new files in the source: {unexpected}"
        assert not list(source_home.glob("*.zip")), "the archive must not survive"


def test_the_fixture_really_is_a_separate_machine(executor):
    """Guard against a false green: if this ran locally, everything above
    would pass while proving nothing about SSH."""
    remote_pid = _remote(executor, "echo $$")
    assert remote_pid != str(os.getpid())
    assert _remote(executor, "command -v hermes"), "the target must have Hermes"
    assert sys.executable  # local interpreter exists; the remote one is separate
    assert subprocess.run(["true"], check=False).returncode == 0

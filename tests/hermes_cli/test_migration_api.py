"""The six migration routes.

Thin by construction: token check, profile scope, load/save, ValueError -> 400.
Anything with a rule in it lives in migration_admin and is tested there.
"""

from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from starlette.testclient import TestClient
    from hermes_cli.web_server import app

    app.state.auth_required = False
    return TestClient(app)


@pytest.fixture
def token(client):
    from hermes_cli import web_server

    return {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}


class TestTargetsCrud:
    def test_list_is_empty_before_anything_is_added(self, client, token):
        got = client.get("/api/migration/targets", headers=token)
        assert got.status_code == 200
        assert got.json()["targets"] == []

    def test_create_then_list(self, client, token):
        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})
        rows = client.get("/api/migration/targets", headers=token).json()["targets"]
        assert [r["id"] for r in rows] == ["prod"]
        assert rows[0]["port"] == 22

    def test_duplicate_id_is_rejected(self, client, token):
        body = {"id": "prod", "host": "h", "user": "u"}
        client.post("/api/migration/targets", headers=token, json=body)
        again = client.post("/api/migration/targets", headers=token, json=body)
        assert again.status_code == 409

    def test_invalid_profile_is_400_not_500(self, client, token):
        got = client.post("/api/migration/targets", headers=token,
                          json={"id": "prod", "host": "h"})   # no user
        assert got.status_code == 400
        assert "user" in got.json()["detail"]

    def test_password_field_is_refused(self, client, token):
        got = client.post("/api/migration/targets", headers=token,
                          json={"id": "p", "host": "h", "user": "u",
                                "password": "hunter2"})
        assert got.status_code == 400
        assert "hunter2" not in got.text, "never echo a submitted secret back"

    def test_delete_removes_it(self, client, token):
        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})
        assert client.delete("/api/migration/targets/prod",
                             headers=token).status_code == 200
        assert client.get("/api/migration/targets",
                          headers=token).json()["targets"] == []

    def test_unknown_id_on_delete_is_404(self, client, token):
        assert client.delete("/api/migration/targets/nope",
                             headers=token).status_code == 404


class TestAuth:
    def test_every_route_requires_a_token(self, client):
        # These carry SSH connection details and trigger remote execution.
        assert client.get("/api/migration/targets").status_code == 401
        assert client.post("/api/migration/targets", json={}).status_code == 401
        assert client.delete("/api/migration/targets/x").status_code == 401
        assert client.post(
            "/api/migration/targets/x/preflight").status_code == 401
        assert client.post("/api/migration/targets/x/migrate").status_code == 401


class TestPreflightRoute:
    def test_returns_per_check_verdicts_and_stores_the_summary(
        self, client, token, monkeypatch
    ):
        from hermes_cli import migration_admin

        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        class FakeExecutor:
            def __init__(self, profile):
                self.profile = profile

            def recorded_fingerprint(self):
                return None

        monkeypatch.setattr("hermes_cli.web_server.SshExecutor", FakeExecutor)
        monkeypatch.setattr(
            migration_admin, "run_preflight",
            lambda *a, **k: [migration_admin.CheckResult("os", "blocking", True, "linux")],
        )
        got = client.post("/api/migration/targets/prod/preflight", headers=token)
        assert got.status_code == 200
        assert got.json()["checks"][0]["name"] == "os"
        assert got.json()["blocked"] is False

        rows = client.get("/api/migration/targets", headers=token).json()["targets"]
        assert rows[0]["last_preflight"] is not None

    def test_first_preflight_pins_the_host_key(self, client, token, monkeypatch):
        """TOFU. The design's whole host-key story depends on this write
        happening: without it every later connection re-accepts whatever key
        it is offered."""
        from hermes_cli import migration_admin

        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        class FakeExecutor:
            def __init__(self, profile):
                self.profile = profile

            def recorded_fingerprint(self):
                return "SHA256:seen-on-first-contact"

        monkeypatch.setattr("hermes_cli.web_server.SshExecutor", FakeExecutor)
        monkeypatch.setattr(
            migration_admin, "run_preflight",
            lambda *a, **k: [migration_admin.CheckResult("os", "blocking", True, "linux")],
        )
        client.post("/api/migration/targets/prod/preflight", headers=token)

        rows = client.get("/api/migration/targets", headers=token).json()["targets"]
        assert rows[0]["host_fingerprint"] == "SHA256:seen-on-first-contact"

    def test_the_executor_gets_the_migration_private_known_hosts_file(
        self, client, token, monkeypatch, tmp_path
    ):
        from hermes_cli import migration_admin

        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        seen = {}

        class FakeExecutor:
            def __init__(self, profile):
                seen.update(profile)

            def recorded_fingerprint(self):
                return None

        monkeypatch.setattr("hermes_cli.web_server.SshExecutor", FakeExecutor)
        monkeypatch.setattr(
            migration_admin, "run_preflight",
            lambda *a, **k: [migration_admin.CheckResult("os", "blocking", True, "linux")],
        )
        client.post("/api/migration/targets/prod/preflight", headers=token)

        assert seen["known_hosts_file"] == str(
            migration_admin.known_hosts_path(tmp_path)
        )


class TestMigrateRoute:
    def test_spawns_the_cli_action_and_returns_its_name(
        self, client, token, monkeypatch
    ):
        client.post("/api/migration/targets", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        spawned = {}

        def fake_spawn(subcommand, name):
            spawned["subcommand"] = subcommand
            spawned["name"] = name
            return object()

        monkeypatch.setattr("hermes_cli.web_server._spawn_hermes_action", fake_spawn)
        got = client.post("/api/migration/targets/prod/migrate", headers=token)
        assert got.status_code == 200
        assert got.json()["action"] == "migrate-host"
        assert spawned["subcommand"][:3] == ["migrate", "host", "prod"]

    def test_the_scoped_profile_reaches_the_spawned_cli(
        self, client, token, monkeypatch, tmp_path
    ):
        """The routes are profile-scoped, so a target can be created under a
        non-default profile. Launching without passing the profile through
        would send the CLI looking in the default home, where that target does
        not exist."""
        (tmp_path / "profiles" / "work").mkdir(parents=True)
        client.post("/api/migration/targets?profile=work", headers=token,
                    json={"id": "prod", "host": "h", "user": "u"})

        spawned = {}
        monkeypatch.setattr(
            "hermes_cli.web_server._spawn_hermes_action",
            lambda subcommand, name: spawned.update(subcommand=subcommand) or object(),
        )
        got = client.post(
            "/api/migration/targets/prod/migrate?profile=work", headers=token
        )
        assert got.status_code == 200
        assert "work" in spawned["subcommand"]
        assert spawned["subcommand"][-3:] == ["migrate", "host", "prod"]


class TestProfileScoping:
    """``_targets_file()`` must resolve through the request's scoped profile.

    Regression pin: ``get_default_hermes_root()`` reads the process-wide
    default directly and ignores ``_profile_scope``'s context-local
    override, which made ``profile=`` a silent no-op on every route — a
    multi-profile operator would manage the wrong host list without any
    error telling them so.
    """

    def test_targets_created_under_one_profile_are_invisible_under_another(
        self, client, token, tmp_path
    ):
        # A real, on-disk profile: profile_exists() just checks this directory.
        (tmp_path / "profiles" / "work").mkdir(parents=True)

        created = client.post(
            "/api/migration/targets?profile=work", headers=token,
            json={"id": "prod", "host": "h", "user": "u"},
        )
        assert created.status_code == 200

        default_rows = client.get(
            "/api/migration/targets", headers=token
        ).json()["targets"]
        assert default_rows == []

        work_rows = client.get(
            "/api/migration/targets?profile=work", headers=token
        ).json()["targets"]
        assert [r["id"] for r in work_rows] == ["prod"]


class TestEstimateArchiveBytes:
    """Pins the preflight route's disk-space estimate against two bugs:
    counting regenerable trees a real backup never writes, and 500ing on a
    single unreadable entry instead of skipping it.
    """

    def test_excludes_the_same_regenerable_trees_backup_py_does(self, tmp_path):
        from hermes_cli.web_server import _estimate_archive_bytes

        (tmp_path / "config.yaml").write_bytes(b"x" * 100)
        heavy = tmp_path / "node_modules" / "pkg"
        heavy.mkdir(parents=True)
        (heavy / "index.js").write_bytes(b"y" * 10_000)

        assert _estimate_archive_bytes(tmp_path) == 100

    def test_a_stat_failure_on_one_entry_does_not_fail_the_whole_estimate(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.web_server import _estimate_archive_bytes

        (tmp_path / "config.yaml").write_bytes(b"x" * 5)
        (tmp_path / "auth.json").write_bytes(b"y" * 7)

        real_stat = Path.stat

        def flaky_stat(self, *a, **k):
            if self.name == "auth.json":
                raise OSError("permission denied")
            return real_stat(self, *a, **k)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        assert _estimate_archive_bytes(tmp_path) == 5

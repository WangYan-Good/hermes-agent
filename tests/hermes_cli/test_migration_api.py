"""The six migration routes.

Thin by construction: token check, profile scope, load/save, ValueError -> 400.
Anything with a rule in it lives in migration_admin and is tested there.
"""

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

        monkeypatch.setattr(
            "hermes_cli.web_server.SshExecutor",
            lambda profile: object(),
        )
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

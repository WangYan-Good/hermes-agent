"""The authenticated REST display view is additive and profile-scoped."""
import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_state import SessionDB


@pytest.fixture
def client_db(tmp_path, monkeypatch):
    import hermes_state
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "lineage-test")
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("parent", source="webui")
        db.append_message("parent", "user", content="Ancestor question")
        db.append_message("parent", "assistant", content="Ancestor answer")
        db.end_session("parent", "compression")
        db.create_session("tip", source="webui", parent_session_id="parent")
        db.append_message("tip", "user", content="Tip question")
        db.append_message("tip", "assistant", content="Tip answer", reasoning="Thinking",
                          display_metadata={"turn_id": "turn-tip"})
        with TestClient(web_server.app, base_url="http://127.0.0.1",
                        headers={"Authorization": "Bearer lineage-test"}) as client:
            yield client, db


def test_opt_in_rest_resolves_tip_but_pages_complete_display_lineage(client_db):
    client, db = client_db
    url = "/api/sessions/parent/messages"
    legacy = client.get(url).json()
    assert {r["session_id"] for r in legacy["messages"]} == {"tip"}
    latest = client.get(url, params={"view": "display", "order": "latest", "limit": 3}).json()
    assert latest["session_id"] == "tip"
    assert [r["content"] for r in latest["messages"]] == ["Ancestor answer", "Tip question", "Tip answer"]
    assert latest["messages"][-1]["reasoning"] == "Thinking"
    cursor = latest["messages"][0]["id"]
    db.append_message("tip", "assistant", content="Later append")
    older = client.get(url, params={"view": "display", "order": "latest", "limit": 3, "before_id": cursor}).json()
    assert [r["content"] for r in older["messages"]] == ["Ancestor question"]
    assert older["messages"][0]["id"] < cursor


def test_display_view_cap_and_cursor_validation(client_db):
    client, _ = client_db
    url = "/api/sessions/parent/messages"
    assert client.get(url, params={"view": "display", "limit": 999}).json()["pagination"]["limit"] == 500
    assert client.get(url, params={"view": "display", "offset": 1}).status_code == 400
    assert client.get(url, params={"view": "unknown"}).status_code == 400
    assert client.get(url, params={"view": "display", "before_id": 1, "order": "oldest"}).status_code == 400
    assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_old_branch_id_resolves_branch_tip_without_original_root(client_db):
    client, db = client_db
    cfg = {"_branched_from": "parent"}
    db.create_session("branch", source="webui", parent_session_id="parent", model_config=cfg)
    db.append_message("branch", "assistant", content="Branch A")
    db.end_session("branch", "compression")
    db.create_session("branch-tip", source="webui", parent_session_id="branch", model_config=cfg)
    db.append_message("branch-tip", "assistant", content="Branch B")
    response = client.get("/api/sessions/branch/messages", params={
        "view": "display", "order": "latest", "include_compacted": "true",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "branch-tip"
    assert [r["content"] for r in payload["messages"]] == ["Branch A", "Branch B"]

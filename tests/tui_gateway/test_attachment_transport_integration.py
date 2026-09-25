"""Real authenticated HTTP + gateway WebSocket, with a controlled model boundary."""
import json
import threading

from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.attachments import store


def rpc(ws, rid, method, params):
    ws.send_json({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        value = ws.receive_json()
        if value.get("id") == rid:
            return value


def test_upload_submit_and_recover_without_replaying(tmp_path, monkeypatch):
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "integration-token")
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    accepted = threading.Event()
    turns = []

    def controlled_turn(rid, sid, session, text, *, image_paths=None, display_metadata=None):
        # This replaces the model worker, not the ownership/claim/RPC/HTTP path.
        turns.append((text, image_paths))
        with SessionDB(tmp_path / "state.db") as db:
            db.create_session(session["session_key"], source="webui")
            db.append_message(session["session_key"], "user", content=text,
                              display_metadata={**display_metadata, "turn_id": session["_display_turn_id"]})
        session["running"] = False
        accepted.set()
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", controlled_turn)
    client = TestClient(web_server.app, base_url="http://127.0.0.1", client=("127.0.0.1", 43210))
    headers = {"Authorization": "Bearer integration-token"}
    with client.websocket_connect("ws://127.0.0.1/api/ws?token=integration-token") as ws:
        created = rpc(ws, 1, "session.create", {"source": "webui", "close_on_disconnect": False})
        assert "result" in created, created
        sid = created["result"]["session_id"]
        prepared = rpc(ws, 2, "attachment.prepare", {"session_id": sid, "occurrence_id": "occ", "upload_request_id": "req", "name": "sample.txt", "size": 5, "mime": "text/plain"})["result"]
        grant = rpc(ws, 3, "attachment.connection", {"session_id": sid})["result"]
        url = f"/api/chat/attachments/{prepared['draft_id']}"
        response = client.post(url + "/recover", headers={**headers, "X-Hermes-Attachment-Connection": grant["connection_token"], "X-Hermes-Attachment-Token": prepared["draft_token"]})
        assert response.status_code == 200, response.text
        aid = prepared["attachment"]["id"]
        response = client.put(url + "/" + aid, content=b"hello", headers={**headers, "X-Hermes-Runtime": sid, "X-Hermes-Attachment-Token": prepared["draft_token"], "X-Hermes-Attachment-Upload": prepared["upload_token"]})
        assert response.status_code == 200, response.text
        params = {"session_id": sid, "draft_id": prepared["draft_id"], "draft_token": prepared["draft_token"], "attachment_ids": [aid], "text": "Read this"}
        submitted = rpc(ws, 4, "prompt.submit", params)
        assert submitted.get("result", {}).get("status") == "streaming", submitted
        assert accepted.wait(5)
        # Treat ACK as lost: inspect the ledger; do not send a second prompt.
        snapshot = rpc(ws, 5, "attachment.snapshot", params)["result"]["attachments"]
        assert snapshot[0]["state"] == "submitted"
        assert len(turns) == 1
        assert turns[0][1] == []  # explicit files never consume implicit images
        assert "@file:" in turns[0][0]
        rejected = rpc(ws, 6, "prompt.submit", params)
        assert "error" in rejected
        assert len(turns) == 1
        stored = server._sessions[sid]["session_key"]
        history = client.get(f"/api/sessions/{stored}/messages?limit=100&order=latest", headers=headers)
        assert history.status_code == 200, history.text
        attachment = history.json()["messages"][0]["display_metadata"]["attachments"][0]
        assert attachment["id"] == aid
        assert "draft_token" not in json.dumps(history.json())
        path = store.drafts[prepared["draft_id"]].items[aid].path
        resource = client.get("/api/chat/resources", params={"path": str(path)}, headers=headers)
        assert resource.content == b"hello", resource.text
    server._sessions.pop(sid, None)
    store.drafts.pop(prepared["draft_id"], None)

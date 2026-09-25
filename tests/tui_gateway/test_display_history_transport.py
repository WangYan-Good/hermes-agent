"""Real auth/HTTP/WS/SessionDB resume keeps runtime and display authority split."""
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from hermes_cli import web_server
from hermes_state import SessionDB
from tui_gateway import server


def test_compression_resume_rest_failure_retry_and_cross_segment_tool(tmp_path, monkeypatch):
    import hermes_state
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "lineage-transport")
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    def no_submit(*args, **kwargs):
        raise AssertionError("History recovery must never submit a prompt")
    monkeypatch.setattr(server, "_run_prompt_submit", no_submit)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("ancestor", source="webui")
        db.append_message("ancestor", "user", content="Ancestor")
        db.append_message("ancestor", "assistant", content="", tool_calls=[
            {"id": "call", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"report.txt"}'}}
        ])
        db.end_session("ancestor", "compression")
        db.create_session("tip", source="webui", parent_session_id="ancestor")
        db.append_message("tip", "tool", content="tool result", tool_call_id="call", tool_name="read_file")
        db.append_message("tip", "assistant", content="```html\n<h1>Safe source</h1>\n```", display_metadata={"turn_id": "turn"})

    failures = []
    # Wrap only this client's ASGI app, preserving the real auth and routes.
    async def transport(scope, receive, send):
        if scope["type"] == "http" and scope["path"].endswith("/messages") and failures:
            failures.pop()
            await JSONResponse({"detail": "temporary history failure"}, status_code=503)(scope, receive, send)
        else:
            await web_server.app(scope, receive, send)

    headers = {"Authorization": "Bearer lineage-transport"}
    with TestClient(transport, base_url="http://127.0.0.1", client=("127.0.0.1", 43210)) as client:
        with client.websocket_connect("ws://127.0.0.1/api/ws?token=lineage-transport") as ws:
            ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.resume", "params": {"session_id": "ancestor", "omit_messages": True}})
            while True:
                response = ws.receive_json()
                if response.get("id") == 1:
                    break
            assert "result" in response, response
            resumed = response["result"]
            assert resumed["session_key"] == "tip"
            assert resumed.get("messages", []) == []
            url = "/api/sessions/ancestor/messages?view=display&include_compacted=true&order=latest&limit=2"
            assert client.get(url, headers={"Authorization": "Bearer bad"}).status_code == 401
            latest = client.get(url, headers=headers).json()
            assert latest["session_id"] == "tip"
            failures.append(True)
            assert client.get(url, headers=headers).status_code == 503
            assert client.get(url, headers=headers).json() == latest
            older = client.get(url + f"&before_id={latest['messages'][0]['id']}", headers=headers).json()
            rows = older["messages"] + latest["messages"]
            assert [r["content"] for r in rows][0] == "Ancestor"
            assert len({r["id"] for r in rows}) == 4
            assert rows[1]["tool_calls"][0]["id"] == rows[2]["tool_call_id"] == "call"
            assert rows[3]["display_metadata"]["turn_id"] == "turn"
        server._sessions.pop(resumed["session_id"], None)

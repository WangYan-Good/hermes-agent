"""Real registry + authenticated Web routes; external installers/IdPs are stubs."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from tui_gateway.mcp_setup import claim_operation, mark_operation, read_operation


@pytest.fixture
def live(monkeypatch, tmp_path):
    from tui_gateway import server
    from hermes_cli import web_server, mcp_catalog, mcp_config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {})
    monkeypatch.setattr(server, "_answers", {})
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_emit", lambda *_: None)
    monkeypatch.setattr(web_server, "_mcp_oauth_flows", {})
    monkeypatch.setattr(web_server, "_ACTION_LOG_FILES", {})
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server, "_resolve_profile_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(web_server, "_profile_cli_args", lambda profile: ["-p", profile])
    monkeypatch.setattr(mcp_catalog, "get_entry", lambda name: SimpleNamespace(install=True))
    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {"test": {"url": "https://idp.invalid/mcp", "auth": "oauth"}})
    spawn = Mock()
    monkeypatch.setattr(web_server, "_spawn_hermes_action", spawn)
    saved = Mock()
    monkeypatch.setattr(web_server, "save_env_value", saved)
    workers = []

    def pending(rid, action="install", sid="s", profile="work"):
        home = tmp_path / profile
        home.mkdir(exist_ok=True)
        server._sessions[sid] = {"profile_home": str(home), "session_key": sid, "agent": None, "history": [], "history_lock": threading.RLock()}
        server._pending[rid] = (sid, threading.Event())
        server._pending_prompt_payloads[rid] = ("mcp.setup.request", {"server": "test", "action": action, "reason": "needed"})
        return str(home)

    def oauth_worker(flow, cfg):
        workers.append(flow)
        asyncio.run(flow.publish_authorization_url("https://idp.invalid/auth?state=public-state"))

    monkeypatch.setattr(web_server, "_run_dashboard_mcp_oauth", oauth_worker)
    with TestClient(web_server.app) as client:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        yield SimpleNamespace(gateway=server, web=web_server, client=client, pending=pending, spawn=spawn, saved=saved, workers=workers)


def test_install_acceptance_survives_lost_response_and_has_unique_request_profile_identity(live):
    live.pending("a")
    live.pending("b")
    live.pending("c", sid="other", profile="personal")
    params = {"session_id": "s", "request_id": "a", "profile": "work"}
    first = live.client.post("/api/mcp/catalog/install", params=params, json={"name": "test", "env": {"KEY": "MCP-PRIVATE-SENTINEL"}})
    assert first.status_code == 200
    operation = first.json()["operation"]
    # A fresh HTTP client can recover without receiving/retaining first's body.
    recovered = live.client.get("/api/mcp/setup/a/operation", params=params).json()["operation"]
    assert recovered == operation
    duplicate = live.client.post("/api/mcp/catalog/install", params=params, json={"name": "test"})
    assert duplicate.json()["operation"] == operation
    assert live.spawn.call_count == 1
    assert live.saved.call_count == 1
    assert live.spawn.call_args.args[0] == ["-p", "work", "mcp", "install", "test"]
    ids = {operation["id"]}
    for rid, sid, profile in [("b", "s", "work"), ("c", "other", "personal")]:
        response = live.client.post("/api/mcp/catalog/install", params={"session_id": sid, "request_id": rid, "profile": profile}, json={"name": "test"})
        assert response.status_code == 200
        ids.add(response.json()["operation"]["id"])
    assert len(ids) == 3
    snapshot = live.gateway._live_session_payload("s", live.gateway._sessions["s"], omit_messages=True)
    assert snapshot["pending_interactions"][0]["payload"]["operation"] == operation
    assert "MCP-PRIVATE-SENTINEL" not in json.dumps(snapshot)
    active = live.gateway._methods["session.activate"]("rpc", {"session_id": "s", "omit_messages": True})
    assert active["result"]["pending_interactions"][0]["payload"]["operation"] == operation


def test_oauth_acceptance_is_bound_before_return_and_duplicate_does_not_start(live):
    live.pending("a", "authorize")
    params = {"session_id": "s", "request_id": "a", "profile": "work"}
    first = live.client.post("/api/mcp/servers/test/auth", params=params)
    assert first.status_code == 200
    identity = first.json()["flow_id"]
    snapshot = live.gateway._pending_interaction_payloads("s")[0]["payload"]
    assert snapshot["operation"]["id"] == identity
    assert snapshot["operation"]["profile"] == "work"
    second = live.client.post("/api/mcp/servers/test/auth", params=params)
    assert second.status_code == 200
    assert second.json()["flow_id"] == identity
    assert len(live.workers) == 1
    assert live.client.get(f"/api/mcp/oauth/flows/{identity}").json()["status"] == "authorization_required"
    live.workers[0].mark_approved()
    assert live.client.get(f"/api/mcp/oauth/flows/{identity}").json()["status"] == "approved"
    assert "authorization_url" not in json.dumps(snapshot)


@pytest.mark.parametrize("wrong", ["session", "profile", "kind", "server", "expired", "released", "not_mcp", "unsafe_id"])
def test_binding_rejects_wrong_or_stale_identity_without_side_effects(live, wrong):
    home = live.pending("a")
    args = ["s", "a", home, "work", "test", "install", "operation-1"]
    if wrong == "session": args[0] = "other"
    elif wrong == "profile": args[2] = home + "-other"
    elif wrong == "kind": args[5] = "authorize"
    elif wrong == "server": args[4] = "different"
    elif wrong == "unsafe_id": args[6] = "../MCP-PRIVATE-SENTINEL"
    elif wrong == "expired": live.gateway._pending["a"][1].set()
    elif wrong == "released": live.gateway._pending.pop("a")
    elif wrong == "not_mcp": live.gateway._pending_prompt_payloads["a"] = ("secret.request", {})
    with pytest.raises(ValueError): claim_operation(*args)
    assert live.spawn.call_count == 0
    assert live.saved.call_count == 0


def test_same_type_concurrency_late_updates_and_scoped_expiry(live):
    home = live.pending("a")
    live.pending("b")
    a, created = claim_operation("s", "a", home, "work", "test", "install", "action-a")
    b, _ = claim_operation("s", "b", home, "work", "test", "install", "action-b")
    assert created
    again, created = claim_operation("s", "a", home, "work", "test", "install", "late-action")
    assert not created and again == a
    mark_operation("s", "b", home, "action-a", "failed")
    assert read_operation("s", "b", home) == b
    live.gateway._pending["a"][1].set()
    mark_operation("s", "a", home, "action-a", "running")
    assert [i["payload"]["request_id"] for i in live.gateway._pending_interaction_payloads("s")] == ["b"]
    assert read_operation("s", "b", home) == b


def test_starting_snapshot_precedes_installer_and_contains_no_credentials(live):
    live.pending("a")
    def spawn(*_):
        value = live.gateway._pending_interaction_payloads("s")[0]["payload"]["operation"]
        assert value["state"] == "starting"
        assert "MCP-PRIVATE-SENTINEL" not in json.dumps(value)
    live.spawn.side_effect = spawn
    response = live.client.post("/api/mcp/catalog/install?session_id=s&request_id=a&profile=work", json={"name": "test", "env": {"KEY": "MCP-PRIVATE-SENTINEL"}})
    assert response.status_code == 200
    assert response.json()["operation"]["state"] == "running"


def test_wrong_profile_http_request_cannot_write_env_or_start(live):
    live.pending("a")
    response = live.client.post("/api/mcp/catalog/install?session_id=s&request_id=a&profile=personal", json={"name": "test", "env": {"KEY": "MCP-PRIVATE-SENTINEL"}})
    assert response.status_code == 409
    live.saved.assert_not_called()
    live.spawn.assert_not_called()


def test_real_tool_callback_block_resume_and_response(live, monkeypatch):
    from hermes_state import SessionDB
    from pathlib import Path
    from tools.setup_mcp_tool import setup_mcp_tool

    home = live.pending("seed", "authorize")
    live.gateway._pending.pop("seed")
    live.gateway._pending_prompt_payloads.pop("seed")
    db = SessionDB(db_path=Path(home) / "state.db")
    db.create_session("s", source="webui")
    db.close()
    monkeypatch.setattr(live.gateway, "_profile_home", lambda profile: Path(home))
    ready = threading.Event()
    emitted = []
    monkeypatch.setattr(live.gateway, "_emit", lambda event, sid, payload: (emitted.append((event, payload)), ready.set()))
    answers = []
    worker = threading.Thread(target=lambda: answers.append(setup_mcp_tool("test", "authorize", "needed", callback=live.gateway._agent_cbs("s")["setup_mcp_callback"])))
    worker.start()
    try:
        assert ready.wait(3)
        request_id = next(p["request_id"] for event, p in emitted if event == "mcp.setup.request")
        response = live.client.post("/api/mcp/servers/test/auth", params={"session_id": "s", "request_id": request_id, "profile": "work"})
        assert response.status_code == 200
        resumed = live.gateway._methods["session.resume"]("rpc", {"session_id": "s", "profile": "work", "omit_messages": True})
        assert resumed["result"]["pending_interactions"][0]["payload"]["operation"]["id"] == response.json()["flow_id"]
        outcome = json.dumps({"status": "authorized", "server": "test"})
        result = live.gateway._methods["mcp.setup.respond"]("rpc", {"request_id": request_id, "result": outcome})
        assert result["result"]["status"] == "ok"
        worker.join(3)
        assert not worker.is_alive()
        assert json.loads(answers[0])["status"] == "authorized"
        assert live.gateway._pending_interaction_payloads("s") == []
        with pytest.raises(ValueError):
            claim_operation("s", request_id, home, "work", "test", "authorize", "late-flow")
        db = SessionDB(db_path=Path(home) / "state.db")
        assert db.get_messages_as_conversation("s") == []
        db.close()
    finally:
        live.gateway._clear_pending("s")
        worker.join(3)


def test_projection_drops_unrecognized_operation_fields(live):
    home = live.pending("a")
    claim_operation("s", "a", home, "work", "test", "install", "action-a")
    live.gateway._pending_prompt_payloads["a"][1]["operation"].update({"env": "MCP-PRIVATE-SENTINEL", "token": "MCP-PRIVATE-SENTINEL", "authorization_url": "MCP-PRIVATE-SENTINEL"})
    assert "MCP-PRIVATE-SENTINEL" not in json.dumps(live.gateway._pending_interaction_payloads("s"))
    assert "MCP-PRIVATE-SENTINEL" not in json.dumps(read_operation("s", "a", home))


def test_failed_start_cannot_be_replayed(live):
    live.pending("a")
    live.spawn.side_effect = RuntimeError("MCP-PRIVATE-SENTINEL")
    url = "/api/mcp/catalog/install?session_id=s&request_id=a&profile=work"
    first = live.client.post(url, json={"name": "test"})
    assert first.status_code == 500
    assert "MCP-PRIVATE-SENTINEL" not in first.text
    second = live.client.post(url, json={"name": "test"})
    assert not second.json()["ok"]
    assert second.json()["operation"]["state"] == "failed"
    assert live.spawn.call_count == 1


def test_cancel_one_oauth_operation_leaves_other_profile_flow_pending(live):
    live.pending("a", "authorize")
    live.pending("b", "authorize", sid="other", profile="personal")
    flows = []
    for rid, sid, profile in [("a", "s", "work"), ("b", "other", "personal")]:
        result = live.client.post("/api/mcp/servers/test/auth", params={"session_id": sid, "request_id": rid, "profile": profile})
        assert result.status_code == 200
        flows.append(result.json()["flow_id"])
    assert flows[0] != flows[1]
    live.client.delete(f"/api/mcp/oauth/flows/{flows[0]}")
    assert live.client.get(f"/api/mcp/oauth/flows/{flows[0]}").json()["status"] == "error"
    assert live.client.get(f"/api/mcp/oauth/flows/{flows[1]}").json()["status"] == "authorization_required"
    assert live.gateway._pending_interaction_payloads("other")[0]["payload"]["operation"]["id"] == flows[1]

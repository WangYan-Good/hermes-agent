"""Live Native recovery uses real blocking registries, never durable answers."""
import json
import threading

import pytest


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tui_gateway import server
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {})
    monkeypatch.setattr(server, "_answers", {})
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    return server


@pytest.mark.parametrize("kind,key", [("secret", "value"), ("sudo", "password"), ("clarify", "answer"), ("mcp.setup", "result")])
def test_real_block_response_and_expiry_projection(gateway, kind, key, tmp_path):
    result = []
    ready = threading.Event()
    gateway._emit = lambda *args: ready.set()
    worker = threading.Thread(target=lambda: result.append(gateway._block(f"{kind}.request", "one", {"prompt": "Enter", "question": "Which?", "env_var": "KEY", "server": "test", "action": "install", "reason": "Needed", "value": "DO-NOT-PROJECT", "password": "DO-NOT-PROJECT"}, timeout=5)))
    worker.start()
    try:
        assert ready.wait(2)
        snapshot = gateway._pending_interaction_payloads("one")
        assert len(snapshot) == 1
        assert gateway._pending_interaction_payloads("two") == []
        assert "DO-NOT-PROJECT" not in json.dumps(snapshot)
        rid = snapshot[0]["payload"]["request_id"]
        assert gateway._methods[f"{kind}.respond"]("rpc", {"request_id": rid, key: "private-answer"})["result"]["status"] == "ok"
        assert gateway._pending_interaction_payloads("one") == []
    finally:
        worker.join(2)
        if worker.is_alive():
            gateway._clear_pending("one")
            worker.join(2)
    assert not worker.is_alive()
    assert result == ["private-answer"]
    assert gateway._methods[f"{kind}.respond"]("late", {"request_id": rid, key: "late"})["result"]["status"] == "expired"
    assert not list(tmp_path.rglob("*.db"))


def test_multiple_requests_snapshot_copies_and_scoped_interrupt(gateway):
    for rid, sid in [("a", "one"), ("b", "one"), ("c", "two")]:
        gateway._pending[rid] = (sid, threading.Event())
        gateway._pending_prompt_payloads[rid] = ("clarify.request", {"request_id": rid, "question": "Pick", "choices": ["yes", None, " ", {"value": "secret"}]})
    snapshot = gateway._pending_interaction_payloads("one")
    assert [r["payload"]["request_id"] for r in snapshot] == ["a", "b"]
    assert snapshot[0]["payload"]["choices"] == ["yes"]
    snapshot[0]["payload"]["choices"].append("mutated")
    assert "mutated" not in gateway._pending_prompt_payloads["a"][1]["choices"]
    gateway._clear_pending("one")
    assert gateway._pending_interaction_payloads("one") == []
    assert len(gateway._pending_interaction_payloads("two")) == 1


@pytest.mark.parametrize("kind", ["secret", "sudo", "clarify", "mcp.setup"])
def test_timeout_expires_matching_metadata(gateway, kind):
    events = []
    gateway._emit = lambda event, sid, payload: events.append((event, sid, payload))
    assert gateway._block(f"{kind}.request", "one", {}, timeout=0) == ""
    assert gateway._pending_interaction_payloads("one") == []
    assert events[-1][0] == f"{kind}.expire"
    assert events[-1][2]["request_id"] == events[0][2]["request_id"]

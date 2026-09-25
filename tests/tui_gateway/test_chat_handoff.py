"""Handoff checks the real session/interaction state under the submission lock."""

import contextlib
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def owner(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("durable", source="webui")
    db.append_message("durable", "user", content="exactly once")
    transport = SimpleNamespace()
    session = {
        "transport": transport,
        "history_lock": threading.Lock(),
        "session_key": "durable",
        "running": False,
    }
    monkeypatch.setattr(server, "_sess_nowait", lambda *args: (session, None))
    monkeypatch.setattr(server, "current_transport", lambda: transport)
    monkeypatch.setattr(server, "_session_db", lambda s: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_pending_prompt_payloads", {})
    monkeypatch.setattr(server, "_pending_approval_request_payload", lambda key: None)
    popped = []
    monkeypatch.setattr(
        server, "_pop_session_by_id", lambda sid: popped.append(sid) or session
    )
    monkeypatch.setattr(server, "_teardown_popped_session", lambda *args, **kw: True)
    yield session, db, popped
    db.close()


def call(action, **kwargs):
    return server._methods["session.handoff"](
        "rpc", {"session_id": "runtime", "action": action, **kwargs}
    )


@pytest.mark.parametrize(
    "field",
    [
        "running",
        "_presentation_workers",
        "queued_prompt",
        "queued_prompts",
        "resume_hydrating",
        "resume_history_error",
        "_auto_continue_scheduled",
    ],
)
def test_busy_and_uncertain_work_cannot_release(owner, field):
    session, _, popped = owner
    session[field] = True
    assert call("prepare")["result"] == {"ready": False}
    assert "presentation_handoff" not in session
    assert not popped


def test_prepare_cancel_release_preserves_real_durable_history(owner):
    session, db, popped = owner
    prepared = call("prepare")["result"]
    assert prepared["stored_id"] == "durable"
    assert call("release", ticket="forged")["error"]["code"] == 4094
    assert not popped
    assert call("cancel", ticket=prepared["ticket"])["result"]["cancelled"]
    assert "presentation_handoff" not in session
    prepared = call("prepare")["result"]
    assert call("release", ticket=prepared["ticket"])["result"]["released"]
    assert popped == ["runtime"]
    assert [m["content"] for m in db.get_messages("durable")] == ["exactly once"]


@pytest.mark.parametrize("kind", ["sudo", "secret", "clarify", "mcp.setup"])
def test_real_pending_registry_keeps_interaction_owner(owner, kind):
    event = threading.Event()
    server._pending["request"] = ("runtime", event)
    server._pending_prompt_payloads["request"] = (kind + ".request", {})
    assert call("prepare")["result"] == {"ready": False}
    assert not event.is_set()
    event.set()
    assert call("prepare")["result"]["ready"]


def test_approval_does_not_auto_resolve(owner, monkeypatch):
    monkeypatch.setattr(
        server,
        "_pending_approval_request_payload",
        lambda key: {"request_id": "approval"},
    )
    assert call("prepare")["result"] == {"ready": False}


def test_other_transport_cannot_control_owner(owner, monkeypatch):
    monkeypatch.setattr(server, "current_transport", lambda: object())
    assert call("prepare")["error"]["code"] == 4031


@pytest.mark.parametrize("defer_history", [False, True])
@pytest.mark.parametrize("allow", [False, True])
def test_resume_opt_out_never_dispatches_interrupted_prompt(
    tmp_path, monkeypatch, defer_history, allow
):
    """Positive control exercises the real marker -> resume -> worker dispatch."""
    from tui_gateway.turn_marker import record_turn_start

    db = SessionDB(tmp_path / "state.db")
    db.create_session("interrupted", source="webui")
    db.append_message("interrupted", "user", content="do not replay me")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 3600, 3))
    submitted = []
    monkeypatch.setattr(
        server, "_run_prompt_submit", lambda *a, **kw: submitted.append((a, kw))
    )

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    # All background resume and continuation work finishes before assertions.
    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    record_turn_start(tmp_path, "interrupted", "do not replay me")
    try:
        response = server.handle_request({
            "id": "resume",
            "method": "session.resume",
            "params": {
                "session_id": "interrupted",
                "allow_auto_continue": allow,
                "defer_history": defer_history,
            },
        })
        assert "error" not in response, response
        assert len(submitted) == (1 if allow else 0)
        assert [m["content"] for m in db.get_messages("interrupted")] == [
            "do not replay me"
        ]
    finally:
        db.close()


def test_unresolved_durable_identity_never_becomes_a_new_draft(owner):
    session, _, popped = owner
    session['session_key'] = 'missing'
    session['history'] = [{'role': 'user', 'content': 'existing turn'}]
    assert call('prepare')['error']['code'] == 5030
    assert 'presentation_handoff' not in session
    assert not popped

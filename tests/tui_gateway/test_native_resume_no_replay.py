"""Native resume preserves authoritative history without replaying interrupted turns."""

import pytest
from hermes_state import SessionDB
from tui_gateway import server


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

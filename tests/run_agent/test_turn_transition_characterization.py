"""Observable accounting and durability contracts before the P4 extraction.

These tests deliberately do not import the state machine. They run the real
conversation loop and continue to describe the contract after ownership moves.
"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.run_agent.test_run_agent import _mock_response
from tests.run_agent.test_transport_recovery_convergence import (
    ApiRecorder,
    _drop,
    _run,
    loop_agent,
)


class RequestRecorder(ApiRecorder):
    def __init__(self, agent, responses):
        self.requests = []
        super().__init__(agent, responses)

    def _stream(self, api_kwargs, on_first_delta=None, **kwargs):
        self.requests.append(deepcopy(api_kwargs))
        return super()._stream(api_kwargs, on_first_delta, **kwargs)

    def _nonstream(self, api_kwargs):
        self.requests.append(deepcopy(api_kwargs))
        return super()._nonstream(api_kwargs)


@pytest.mark.parametrize("kind", ["normal", "transport_empty", "transport_partial", "length"])
def test_network_attempt_and_model_step_have_distinct_budgets(loop_agent, kind):
    agent = loop_agent
    agent.max_tokens = 4096
    initial_used = agent.iteration_budget.used
    final = _mock_response(content="Finished.", finish_reason="stop")
    first = {
        "normal": [],
        "transport_empty": [_drop(content="", visible=False)],
        "transport_partial": [_drop(content="Part one.", visible=True)],
        "length": [_mock_response(content="Part one.", finish_reason="length")],
    }[kind]
    recorder = RequestRecorder(agent, [*first, final])

    result = _run(agent, "Complete the answer")

    expected_steps = 2 if kind in {"transport_partial", "length"} else 1
    assert result["api_calls"] == expected_steps
    assert agent.iteration_budget.used - initial_used == expected_steps
    assert recorder.call_count == (1 if kind == "normal" else 2)
    if kind.startswith("transport"):
        assert recorder.modes == ["STREAM", "NONSTREAM"]
        caps = [agent._requested_output_cap_from_api_kwargs(r) for r in recorder.requests]
        assert caps[1] == caps[0]
    elif kind == "length":
        assert recorder.modes == ["STREAM", "STREAM"]
        caps = [agent._requested_output_cap_from_api_kwargs(r) for r in recorder.requests]
        assert caps[1] == caps[0] * 2
    assert result["final_response"].count("Finished.") == 1


@pytest.mark.parametrize("gate", ["verification", "pre_verify"])
@pytest.mark.parametrize("failure", ["false", "raised"])
def test_verification_flush_failure_keeps_existing_best_effort_continuation(
    loop_agent, monkeypatch, gate, failure,
):
    agent = loop_agent
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1" if gate == "verification" else "0")
    agent.max_iterations = 3
    recorder = RequestRecorder(agent, [
        _mock_response(content="Candidate.", finish_reason="stop"),
        _mock_response(content="Verified answer.", finish_reason="stop"),
    ])
    events = []

    def flush(messages, *args, **kwargs):
        events.append(("flush", deepcopy(messages)))
        if failure == "raised":
            raise OSError("scripted candidate write failure")
        return False

    def nudge(*args, **kwargs):
        return "Verify the result" if recorder.call_count == 1 else None

    # Setup resets mutation tracking, so simulate an edit when the first
    # response is received, as the real tool executor does during the turn.
    original_next = recorder._next

    def respond():
        agent._turn_file_mutation_paths = {"changed.py"}
        return original_next()

    monkeypatch.setattr(recorder, "_next", respond)
    with (
        patch.object(agent, "_flush_messages_to_session_db", side_effect=flush),
        patch("agent.verification_stop.build_verify_on_stop_nudge", side_effect=nudge),
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: gate == "pre_verify" and name == "pre_verify"),
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: gate == "pre_verify" and name == "pre_verify"),
        patch("hermes_cli.plugins.get_pre_verify_continue_message", side_effect=nudge),
        patch("agent.verify_hooks.max_verify_nudges", return_value=2),
    ):
        result = _run(agent, "Complete and verify the edit")

    assert recorder.call_count == 2
    assert result["final_response"] == "Verified answer."
    assert any(rows[-1].get("content") == "Candidate." for _, rows in events)
    assert all(m.get("content") != "Verify the result" for m in result["messages"])


def test_overflow_rebuild_refunds_logical_budget(loop_agent, monkeypatch):
    from agent import conversation_loop
    from tests.run_agent.test_413_compression import _make_413_error

    agent = loop_agent
    agent.compression_enabled = True
    monkeypatch.setattr(conversation_loop, "time", SimpleNamespace(time=lambda: 10.0, sleep=lambda *_: None))
    recorder = RequestRecorder(agent, [_make_413_error(), _mock_response(content="Recovered.")])
    history = [{"role": "user", "content": "Old question"}, {"role": "assistant", "content": "Old answer"}]
    used = agent.iteration_budget.used
    with patch.object(agent, "_compress_context", return_value=([
        {"role": "user", "content": "Current question"},
    ], "Compressed prompt")) as compress:
        result = _run(agent, "Current question", history)
    compress.assert_called_once()
    assert recorder.call_count == 2
    assert result["final_response"] == "Recovered."
    assert result["api_calls"] == 1
    assert agent.iteration_budget.used - used == 1


def test_stop_during_generic_backoff_prevents_second_request(loop_agent, monkeypatch):
    from agent import conversation_loop

    agent = loop_agent
    agent._disable_streaming = True
    agent._api_max_retries = 2
    error = RuntimeError("Service temporarily unavailable")
    setattr(error, "status_code", 503)
    recorder = RequestRecorder(agent, [error, _mock_response(content="Must not be requested")])
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds
        agent._interrupt_requested = True

    monkeypatch.setattr(conversation_loop, "time", SimpleNamespace(time=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(conversation_loop, "jittered_backoff", lambda *_a, **_k: 1.0)
    with patch.object(agent, "_try_activate_fallback", return_value=False):
        result = _run(agent, "Complete the task")
    assert recorder.call_count == 1
    assert result["interrupted"] is True

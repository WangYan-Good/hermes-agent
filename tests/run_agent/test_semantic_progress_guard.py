"""Real loop/socket/SessionDB gates for semantic convergence (no public network)."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.semantic_progress import SEMANTIC_PROGRESS_NUDGE, SemanticProgressTracker
from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController
from hermes_state import SessionDB
from tests.run_agent.test_transport_recovery_e2e import (
    ScriptedProviderServer, e2e_agent, json_ok, stream_drop,
)
from tests.run_agent.test_transport_fault_injection import _delta_frame, _sse


class ProgressProvider(ScriptedProviderServer):
    def __init__(self, script):
        self.bodies = []
        super().__init__(script)

    def _read_request(self, conn):
        path, body = super()._read_request(conn)
        if path and path.endswith("/chat/completions"):
            self.bodies.append(body)
        return path, body


def call(path="a", *, tool="read_file", args=None, call_id="call"):
    return json_ok(tool_calls=[{
        "id": call_id, "type": "function", "function": {
            "name": tool, "arguments": json.dumps(args or {"path": path}),
        },
    }], finish_reason="tool_calls")


@pytest.fixture
def loop(e2e_agent, tmp_path, monkeypatch):
    servers, databases = [], []
    observations = []
    original_observe = SemanticProgressTracker.observe

    def observe(self, value):
        observations.append(value)
        return original_observe(self, value)

    monkeypatch.setattr(SemanticProgressTracker, "observe", observe)

    def run(script, *, output="same", hard_stop=False, configure=None, history=None):
        server = ProgressProvider(script + [json_ok(content="Script exhausted.")])
        servers.append(server)
        agent = e2e_agent(server)
        agent.stream_delta_callback = None
        agent._disable_streaming = True
        agent.max_iterations = 8
        agent._api_max_retries = 1
        agent.valid_tool_names.update({"read_file", "write_file", "patch", "terminal", "plugin_effect"})
        agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(
            hard_stop_enabled=hard_stop,
        ))
        db = SessionDB(db_path=tmp_path / f"state-{len(servers)}.db")
        databases.append(db)
        agent._session_db = db
        agent._session_json_enabled = True
        agent.logs_dir = tmp_path
        executions = []

        def execute(name, args, task_id=None, **kwargs):
            executions.append((name, args))
            # Real filesystem side effect: every dispatch leaves evidence.
            with (tmp_path / "executions.txt").open("a", encoding="utf-8") as f:
                f.write(name + "\n")
            return output(name, args) if callable(output) else output

        if configure:
            configure(agent)
        with patch("run_agent.handle_function_call", side_effect=execute), patch.object(agent, "_cleanup_task_resources"):
            result = agent.run_conversation("Complete the task", conversation_history=history)
        return agent, server, result, executions, db

    class Harness(SimpleNamespace):
        def __call__(self, *args, **kwargs):
            return run(*args, **kwargs)
    yield Harness(observations=observations)
    for server in servers:
        server.close()
    for db in databases:
        db.close()


def nudge_count(server):
    return sum(SEMANTIC_PROGRESS_NUDGE in json.dumps(body) for body in server.bodies)


def assert_clean(messages):
    assert SEMANTIC_PROGRESS_NUDGE not in json.dumps(messages)
    calls = [tc["id"] for m in messages for tc in (m.get("tool_calls") or [])]
    results = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert sorted(calls) == sorted(results)


@pytest.mark.parametrize("tool,output", [
    ("read_file", "same"), ("write_file", '{"bytes_written":4}'),
    ("plugin_effect", '{"success":true}'), ("terminal", '{"exit_code":1}'),
])
@pytest.mark.parametrize("hard_stop", [False, True])
def test_period_one_halts_before_fourth_dispatch(loop, tool, output, hard_stop):
    agent, server, result, executions, db = loop([
        call(tool=tool, call_id=f"c{i}") for i in range(4)
    ], output=output, hard_stop=hard_stop)
    assert len(server.bodies) == 4
    assert nudge_count(server) == 1
    assert len(executions) == 3
    assert len(loop.observations) == 3
    assert result["completed"] is True
    assert "preserved" in result["final_response"]
    assert sum(m.get("content") == result["final_response"] for m in result["messages"]) == 1
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))


def test_period_two_halts_before_fifth_dispatch(loop):
    _, server, result, executions, _ = loop([
        call(p, call_id=f"c{i}") for i, p in enumerate(["a", "b", "a", "b", "a"])
    ])
    assert len(server.bodies) == 5
    assert nudge_count(server) == 1
    assert len(executions) == 4
    assert "preserved" in result["final_response"]
    assert_clean(result["messages"])


@pytest.mark.parametrize("change", [call("c"), json_ok(content="Concrete blocker.")])
def test_nudge_accepts_strategy_change_or_final(loop, change):
    is_tool = bool(change["payload"]["choices"][0]["message"].get("tool_calls"))
    script = [call(call_id=f"c{i}") for i in range(3)] + [change]
    if is_tool:
        script.append(json_ok(content="Completed new strategy."))
    agent, server, result, executions, db = loop(script)
    assert nudge_count(server) == 1
    assert len(executions) == (4 if is_tool else 3)
    assert "preserved" not in result["final_response"]
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))
    snapshot = json.loads((agent.logs_dir / f"session_{agent.session_id}.json").read_text(encoding="utf-8"))
    assert_clean(snapshot["messages"])
    reloaded = SessionDB(db_path=db.db_path)
    try:
        history = reloaded.get_messages_as_conversation(agent.session_id)
        assert_clean(history)
        _, next_server, next_result, next_exec, _ = loop([
            call(), json_ok(content="New turn completed."),
        ], history=history)
        assert len(next_exec) == 1
        assert nudge_count(next_server) == 0
        assert next_result["completed"] is True
    finally:
        reloaded.close()


def test_real_mutation_allows_same_test_again(loop):
    _, server, result, executions, _ = loop([
        call(tool="terminal"), call(tool="patch", args={"path": "a", "patch": "new"}),
        call(tool="terminal"), json_ok(content="Tests passed."),
    ], output=lambda name, args: '{"success":true}' if name == "patch" else '{"exit_code":0}')
    assert len(executions) == 3
    assert nudge_count(server) == 0
    assert result["final_response"] == "Tests passed."


def test_p1_socket_recovery_is_not_a_tool_round(loop):
    def streaming(agent):
        agent._disable_streaming = False
        agent.stream_delta_callback = lambda text: None
    _, server, result, executions, _ = loop([
        stream_drop([]), json_ok(content="Recovered."),
    ], configure=streaming)
    assert server.modes == ["STREAM", "NONSTREAM"]
    assert nudge_count(server) == 0
    assert loop.observations == []
    assert executions == []
    assert result["completed"] is True


@pytest.mark.parametrize("after_result", [False, True])
def test_persistence_failure_prevents_semantic_continuation(loop, after_result):
    def configure(agent):
        original = agent._flush_messages_to_session_db
        def flush(messages, *args, **kwargs):
            if any(m.get("role") == ("tool" if after_result else "assistant") and
                   (after_result or m.get("tool_calls")) for m in messages):
                return False
            return original(messages, *args, **kwargs)
        agent._flush_messages_to_session_db = flush
    _, server, result, executions, _ = loop([call()], configure=configure)
    assert len(server.bodies) == 1
    assert len(executions) == int(after_result)
    assert nudge_count(server) == 0
    assert loop.observations == []
    assert not result["completed"]


@pytest.mark.parametrize("kind", ["verify", "pre_verify", "ack", "length"])
def test_internal_continuations_are_not_semantic_rounds(loop, monkeypatch, kind):
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1" if kind == "verify" else "0")
    def configure(agent):
        original = agent._interruptible_api_call
        def request(kwargs):
            response = original(kwargs)
            agent._turn_file_mutation_paths = {"changed.py"}
            return response
        agent._interruptible_api_call = request
        if kind == "ack":
            agent._intent_ack_continuation = True
            agent._looks_like_codex_intermediate_ack = lambda assistant_content, **kwargs: assistant_content.startswith("I'll")
    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", side_effect=["verify it", None]),
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: kind == "pre_verify" and name == "pre_verify"),
        patch("hermes_cli.plugins.get_pre_verify_continue_message", side_effect=["run project tests", None]),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        _, server, result, executions, _ = loop([
            json_ok(content="I'll inspect the files now", finish_reason="length" if kind == "length" else "stop"),
            json_ok(content="Verified final answer."),
        ], configure=configure)
    assert len(server.bodies) == 2
    assert nudge_count(server) == 0
    assert loop.observations == []
    assert executions == []
    assert result["completed"] is True


def test_nudged_request_transport_recovery_keeps_one_logical_nudge(loop, caplog):
    script = []
    for i in range(3):
        tc = call(call_id=f"c{i}")["payload"]["choices"][0]["message"]["tool_calls"][0]
        script.append({"kind": "stream_complete", "frames": [
            _delta_frame({"role": "assistant", "tool_calls": [{"index": 0, **tc}]}),
            _sse({"id": "done", "object": "chat.completion.chunk", "created": 1,
                  "model": "test/model", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        ]})
    script += [stream_drop([]), call(call_id="blocked")]
    def streaming(agent):
        agent._disable_streaming = False
        agent.stream_delta_callback = lambda text: None
    _, server, result, executions, _ = loop(script, configure=streaming)
    assert server.modes == ["STREAM", "STREAM", "STREAM", "STREAM", "NONSTREAM"]
    assert nudge_count(server) == 2  # same logical guidance on both physical attempts
    assert sum("semantic progress: action=nudge" in r.message for r in caplog.records) == 1
    assert len(executions) == len(loop.observations) == 3
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_clean(result["messages"])


@pytest.mark.parametrize("redirect", [False, True])
def test_interrupt_or_redirect_after_third_result_beats_pending_nudge(loop, redirect):
    def configure(agent):
        original = agent._execute_tool_calls
        rounds = 0
        def execute(*args, **kwargs):
            nonlocal rounds
            original(*args, **kwargs)
            rounds += 1
            if rounds == 3:
                if redirect:
                    # A correction accepted while the next request is being
                    # prepared is drained at the next outer-loop boundary.
                    agent._pending_redirect = "Try the same action once on my instruction."
                else:
                    agent.interrupt()
        agent._execute_tool_calls = execute
    _, server, result, executions, _ = loop([
        call(call_id=f"c{i}") for i in range(4)
    ] + [json_ok(content="Redirect completed.")], configure=configure)
    assert len(executions) == (4 if redirect else 3)
    assert len(server.bodies) == (5 if redirect else 3)
    assert nudge_count(server) == 0
    assert result["completed"] is redirect


def test_existing_hard_stop_owner_wins(loop, caplog):
    def configure(agent):
        agent._tool_guardrails = ToolCallGuardrailController(ToolCallGuardrailConfig(
            hard_stop_enabled=True, same_tool_failure_halt_after=3,
        ))
    _, server, result, executions, _ = loop([call(tool="terminal", call_id=f"c{i}") for i in range(3)],
        output='{"exit_code":1}', configure=configure)
    assert len(executions) == len(server.bodies) == 3
    assert nudge_count(server) == 0
    assert "same_tool_failure_halt" in result["final_response"]
    assert not any("semantic progress:" in r.message for r in caplog.records)


def test_tool_time_user_steer_rebases_episode(loop):
    def configure(agent):
        original = agent._append_guardrail_observation
        rounds = 0
        def after_call(*args, **kwargs):
            nonlocal rounds
            result = original(*args, **kwargs)
            rounds += 1
            if rounds == 3:
                assert agent.redirect("Try the action once more on my instruction.")
            return result
        agent._append_guardrail_observation = after_call
    _, server, result, executions, _ = loop([
        call(call_id=f"c{i}") for i in range(4)
    ] + [json_ok(content="User correction completed.")], configure=configure)
    assert len(executions) == 4
    assert nudge_count(server) == 0
    assert result["completed"] is True


def test_parallel_worker_completion_keeps_transcript_and_fingerprint_order(loop):
    events = [threading.Event() for _ in range(3)]
    counts = {"a": 0, "b": 0}
    completed = []
    lock = threading.Lock()
    def output(name, args):
        path = args["path"]
        with lock:
            index = counts[path]
            counts[path] += 1
        if path == "a":
            assert events[index].wait(5), "parallel second worker never ran"
        with lock:
            completed.append(path)
        if path == "b":
            events[index].set()
        return path
    script = []
    for i in range(4):
        batch = call("a", call_id=f"a{i}")
        batch["payload"]["choices"][0]["message"]["tool_calls"].extend(
            call("b", call_id=f"b{i}")["payload"]["choices"][0]["message"]["tool_calls"]
        )
        script.append(batch)
    _, server, result, executions, _ = loop(script, output=output)
    assert completed == ["b", "a"] * 3
    assert len(executions) == 6
    assert len(server.bodies) == 4
    assert nudge_count(server) == 1
    assert [m["tool_call_id"] for m in result["messages"] if m["role"] == "tool"] == [
        "a0", "b0", "a1", "b1", "a2", "b2",
    ]
    assert_clean(result["messages"])


def test_preflight_compression_does_not_lose_pending_nudge(loop, monkeypatch, caplog):
    import agent.conversation_loop as cl
    original_estimate = cl.estimate_messages_tokens_rough
    compressions = []
    def estimate(messages, *args, **kwargs):
        if SEMANTIC_PROGRESS_NUDGE in json.dumps(messages) and not compressions:
            return 15000
        return original_estimate(messages, *args, **kwargs)
    monkeypatch.setattr(cl, "estimate_messages_tokens_rough", estimate)
    def configure(agent):
        agent.compression_enabled = True
        compressor = agent.context_compressor
        compressor.threshold_tokens = 10000
        compressor.should_compress = lambda t=None: (t or 0) >= 10000
        compressor.should_defer_preflight_to_real_usage = lambda t: False
        def compress(messages, system_message, **kwargs):
            compressions.append(len(messages))
            assert_clean(messages)
            return list(messages), agent._cached_system_prompt
        agent._compress_context = compress
    _, server, result, executions, _ = loop([call(call_id=f"c{i}") for i in range(4)], configure=configure)
    assert len(compressions) == 1
    assert len(server.bodies) == 4
    assert len(executions) == 3
    assert nudge_count(server) == 1
    assert sum("semantic progress: action=nudge" in r.message for r in caplog.records) == 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_clean(result["messages"])


@pytest.mark.parametrize("arguments", ["[]", "null", '"scalar"'])
def test_non_object_arguments_keep_existing_tool_error_path(loop, arguments):
    malformed = call()
    malformed["payload"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    _, server, result, executions, _ = loop([malformed, json_ok(content="Corrected answer.")])
    assert len(server.bodies) == 2
    assert executions == []
    assert any("Invalid tool arguments" in str(m.get("content")) for m in result["messages"])
    assert result["completed"] is True
    assert_clean(result["messages"])


def test_same_agent_next_real_turn_starts_clean_and_halt_surfaces_once(loop):
    streamed = []
    def configure(agent):
        agent.stream_delta_callback = streamed.append
    agent, server, first, executions, db = loop([
        call(call_id=f"c{i}") for i in range(5)
    ] + [json_ok(content="New turn succeeded.")], configure=configure)
    assert first["completed"] is True
    assert streamed.count(first["final_response"]) == 1
    assert len(executions) == 3
    with patch("run_agent.handle_function_call", return_value="same") as dispatcher, patch.object(agent, "_cleanup_task_resources"):
        second = agent.run_conversation("Try once more now.", conversation_history=first["messages"])
    assert dispatcher.call_count == 1
    assert len(server.bodies) == 6
    assert nudge_count(server) == 1
    assert second["final_response"] == "New turn succeeded."
    assert second["completed"] is True
    assert_clean(second["messages"])
    assert_clean(db.get_messages(agent.session_id))

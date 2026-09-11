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
    assert "proposal_signature" not in json.dumps(messages)
    assert "_hermes_semantic_execution" not in json.dumps(messages)
    calls = [tc["id"] for m in messages for tc in (m.get("tool_calls") or [])]
    results = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert sorted(calls) == sorted(results)


def assert_durable_clean(agent, result, db):
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))
    snapshot = json.loads((agent.logs_dir / f"session_{agent.session_id}.json").read_text())
    assert_clean(snapshot["messages"])
    with SessionDB(db_path=db.db_path) as reopened:
        assert_clean(reopened.get_messages_as_conversation(agent.session_id))


@pytest.fixture
def semantic_plugins(monkeypatch):
    from hermes_cli import plugins
    manager = plugins.PluginManager()
    manager._discovered = True
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    return plugins.PluginContext(plugins.PluginManifest(name="p2-fixture"), manager)


@pytest.mark.parametrize("stage", ["tool_request", "tool_execution", "pre_tool_call"])
@pytest.mark.parametrize("dynamic", [False, True])
def test_real_argument_rewrite_tracks_execution(loop, semantic_plugins, stage, dynamic):
    callbacks = []
    def rewrite(**kwargs):
        callbacks.append(kwargs["tool_call_id"])
        args = {"path": f"/canonical/{len(callbacks)}" if dynamic else "/canonical/a"}
        if stage == "tool_execution":
            return kwargs["next_call"](args)
        return {"action": "modify", "args": args} if stage == "pre_tool_call" else {"args": args}
    register = semantic_plugins.register_hook if stage == "pre_tool_call" else semantic_plugins.register_middleware
    register(stage, rewrite)
    script = [call(call_id=f"rewrite{i}") for i in range(4)]
    if dynamic:
        script.append(json_ok(content="Distinct actions completed."))
    agent, server, result, executions, db = loop(script)
    count = 4 if dynamic else 3
    assert callbacks == [f"rewrite{i}" for i in range(count)]
    assert executions == [("read_file", {"path": f"/canonical/{i+1}" if dynamic else "/canonical/a"}) for i in range(count)]
    assert len(loop.observations) == count
    assert nudge_count(server) == (0 if dynamic else 1)
    assert result["turn_exit_reason"] != "guardrail_halt" if dynamic else result["turn_exit_reason"] == "guardrail_halt"
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))
    snapshot = json.loads((agent.logs_dir / f"session_{agent.session_id}.json").read_text(encoding="utf-8"))
    assert_clean(snapshot["messages"])
    reloaded = SessionDB(db_path=db.db_path)
    try:
        assert_clean(reloaded.get_messages_as_conversation(agent.session_id))
    finally:
        reloaded.close()


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_execution", "approval"])
def test_policy_blocked_novel_attempt_does_not_rearm(loop, semantic_plugins, monkeypatch, fresh, stage):
    callbacks = []
    approvals = []
    if stage == "approval":
        def deny(*args, **kwargs):
            approvals.append(args)
            return {"approved": False, "message": "Denied"}
        monkeypatch.setattr("tools.approval.request_tool_approval", deny)
    def policy(**kwargs):
        callbacks.append(kwargs["tool_call_id"])
        if kwargs["args"]["path"] == "b":
            if stage == "tool_execution":
                return '{"error":"Denied"}'
            return {"action": "approve" if stage == "approval" else "block", "message": "Denied"}
        return kwargs["next_call"]() if stage == "tool_execution" else None
    register = semantic_plugins.register_middleware if stage == "tool_execution" else semantic_plugins.register_hook
    register("pre_tool_call" if stage == "approval" else stage, policy)
    def mixed(i):
        a = call(call_id=f"a{i}")
        a["payload"]["choices"][0]["message"]["tool_calls"].extend(
            call("b", call_id=f"b{i}")["payload"]["choices"][0]["message"]["tool_calls"])
        return a
    script = [mixed(i) if fresh else call(call_id=f"a{i}") for i in range(3)]
    script += [mixed(i) for i in range(3, 7)]
    agent, server, result, executions, db = loop(script)
    expected = 3 if fresh else 4
    assert executions == [("read_file", {"path": "a"})] * expected
    assert len(callbacks) == len(set(callbacks)) == (6 if fresh else 5)
    assert len(approvals) == ((3 if fresh else 1) if stage == "approval" else 0)
    assert len(loop.observations) == expected
    assert nudge_count(server) == 1
    assert len(server.bodies) == expected + 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))


def test_novel_proposal_rewritten_to_old_effect_does_not_rearm(loop, semantic_plugins):
    callbacks = []
    def rewrite(**kwargs):
        callbacks.append(kwargs["tool_call_id"])
        return {"args": {"path": "/canonical/a"}}
    semantic_plugins.register_middleware("tool_request", rewrite)
    agent, server, result, executions, db = loop(
        [call(call_id=f"a{i}") for i in range(3)] + [call("b", call_id=f"b{i}") for i in range(3)]
    )
    assert executions == [("read_file", {"path": "/canonical/a"})] * 4
    assert callbacks == ["a0", "a1", "a2", "b0"]
    assert len(loop.observations) == 4
    assert nudge_count(server) == 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))


def test_exploratory_dynamic_execution_rearms_after_nudge(loop, semantic_plugins):
    callbacks = []
    def rewrite(**kwargs):
        callbacks.append(kwargs["tool_call_id"])
        if kwargs["args"]["path"] == "a" and len(callbacks) >= 4:
            return {"args": {"path": f"a{len(callbacks)}"}}
    semantic_plugins.register_middleware("tool_request", rewrite)
    semantic_plugins.register_hook("pre_tool_call", lambda **kw:
        {"action": "block", "message": "Denied"} if kw["args"]["path"] == "b" else None)
    mixed = call(call_id="explore-a")
    mixed["payload"]["choices"][0]["message"]["tool_calls"].extend(
        call("b", tool="terminal", call_id="explore-b")["payload"]["choices"][0]["message"]["tool_calls"])
    _, server, result, executions, _ = loop(
        [call(call_id=f"a{i}") for i in range(3)] + [mixed, call(call_id="continued"), json_ok(content="Progress landed.")]
    )
    assert executions == [("read_file", {"path": p}) for p in ("a", "a", "a", "a4", "a6")]
    assert len(callbacks) == len(set(callbacks)) == 6
    assert nudge_count(server) == 1
    assert result["final_response"] == "Progress landed."
    assert_clean(result["messages"])


def test_unpersisted_novel_result_cannot_rearm(loop):
    def configure(agent):
        original = agent._flush_messages_to_session_db
        def flush(messages, *args, **kwargs):
            if any(m.get("tool_call_id") == "novel" for m in messages):
                return False
            return original(messages, *args, **kwargs)
        agent._flush_messages_to_session_db = flush
    _, server, result, executions, _ = loop(
        [call(call_id=f"a{i}") for i in range(3)] + [call("b", call_id="novel")], configure=configure,
    )
    assert len(executions) == 4
    assert len(loop.observations) == 3
    assert nudge_count(server) == 1
    assert not result["completed"]
    assert result["turn_exit_reason"] == "session_persistence_failed"


def test_raised_rewritten_tool_retains_actual_execution(loop, semantic_plugins):
    semantic_plugins.register_middleware("tool_request", lambda **kw: {"args": {"path": "/canonical/a"}})
    def execute(name, args):
        raise RuntimeError("fixture tool failed")
    agent, server, result, executions, db = loop([call(call_id=f"raise{i}") for i in range(4)], output=execute)
    assert executions == [("read_file", {"path": "/canonical/a"})] * 3
    assert len(loop.observations) == 3
    assert all(observation.results[0].failed for observation in loop.observations)
    assert nudge_count(server) == 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))


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


@pytest.mark.parametrize("arguments", ["[]", "null", '"scalar"'])
def test_post_nudge_invalid_proposal_preserves_episode(loop, arguments):
    malformed = call(call_id="invalid")
    malformed["payload"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    agent, server, result, executions, db = loop(
        [call(call_id=f"c{i}") for i in range(3)] + [malformed, call(call_id="blocked")]
    )
    assert len(executions) == 3
    assert sum(bool(o.results) for o in loop.observations) == 3
    assert nudge_count(server) == 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert any("Invalid tool arguments" in str(m.get("content")) for m in result["messages"])
    assert_clean(result["messages"])
    reloaded = SessionDB(db_path=db.db_path)
    try:
        assert_clean(reloaded.get_messages_as_conversation(agent.session_id))
    finally:
        reloaded.close()


def test_ordered_overlapping_writes_are_not_an_unordered_cycle(loop, tmp_path):
    target = tmp_path / "ordered.txt"
    script = []
    orders = ["XYZ", "YZX", "ZXY"]
    for i, order in enumerate(orders):
        calls = []
        for j, content in enumerate(order):
            calls.extend(call(tool="write_file", args={"path": str(target), "content": content},
                              call_id=f"w{i}-{j}")["payload"]["choices"][0]["message"]["tool_calls"])
        script.append(json_ok(tool_calls=calls, finish_reason="tool_calls"))
    script.append(json_ok(content="Ordered writes completed."))
    states = []
    def write(name, args):
        target.write_text(args["content"], encoding="utf-8")
        states.append(target.read_text(encoding="utf-8"))
        return '{"bytes_written":1}'
    _, server, result, executions, _ = loop(script, output=write)
    assert len(executions) == 9
    assert states == list("".join(orders))
    assert target.read_text(encoding="utf-8") == "Y"
    assert nudge_count(server) == 0
    assert result["final_response"] == "Ordered writes completed."
    assert_clean(result["messages"])


@pytest.mark.parametrize("next_action", ["same", "novel", "invalid_args", "out_of_scope", "probe"])
def test_deferred_bridge_preserves_semantic_episode(loop, monkeypatch, next_action):
    import model_tools
    from tools.registry import registry
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    landed = []
    for name in ("mcp_p2_action_a", "mcp_p2_action_b", "mcp_p2_denied"):
        def handler(args, task_id=None, _name=name, **kwargs):
            landed.append((_name, dict(args)))
            return '{"success":true}'
        registry.register(name=name, toolset="mcp-p2" if name != "mcp_p2_denied" else "mcp-p2-denied",
                          handler=handler, schema={"name": name, "description": "Test deferred effect",
                          "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                         "required": ["value"]}})
    def bridge(name="mcp_p2_action_a", arguments=None, call_id="bridge"):
        return call(tool="tool_call", args={"name": name, "arguments":
                    {"value": "X"} if arguments is None else arguments}, call_id=call_id)
    script = [bridge(call_id=f"b{i}") for i in range(3)]
    if next_action == "novel":
        script += [bridge("mcp_p2_action_b"), json_ok(content="New strategy completed.")]
    else:
        if next_action == "invalid_args":
            script.append(bridge(arguments=[]))
        elif next_action == "out_of_scope":
            script.append(bridge("mcp_p2_denied"))
        elif next_action == "probe":
            script.append(bridge(arguments={}))
        script.append(bridge(call_id="blocked"))
    def configure(agent):
        agent.valid_tool_names.add("tool_call")
        agent.enabled_toolsets = ["mcp-p2"]
        agent.disabled_toolsets = []
    def dispatch(name, args):
        return model_tools.handle_function_call(name, args, enabled_toolsets=["mcp-p2"])
    agent, server, result, _, db = loop(script, output=dispatch, configure=configure)
    assert len(landed) == (4 if next_action == "novel" else 3)
    assert nudge_count(server) == 1
    if next_action == "novel":
        assert landed[-1][0] == "mcp_p2_action_b"
        assert result["final_response"] == "New strategy completed."
    else:
        assert result["turn_exit_reason"] == "guardrail_halt"
        assert sum(bool(o.results) for o in loop.observations) == 3
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))


@pytest.mark.parametrize("invalid_kind", ["name", "arguments", "scope", "probe"])
@pytest.mark.parametrize("scenario", ["repeated", "post_nudge", "novel"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_mixed_invalid_batch_tracks_executable_subset(loop, monkeypatch, invalid_kind, scenario, invalid_first):
    from tools.registry import registry
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    registry.register(
        name="mcp_p2_mixed_blocked", toolset="mcp-p2-mixed",
        handler=lambda *args, **kwargs: pytest.fail("invalid bridge executed"),
        schema={"name": "mcp_p2_mixed_blocked", "description": "Deferred test tool",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"]}},
    )

    def mixed(path, index):
        valid = call(path, call_id=f"valid{index}")
        if invalid_kind == "name":
            invalid = call(tool="not_a_valid_tool", call_id=f"invalid{index}")
        elif invalid_kind == "arguments":
            invalid = call(call_id=f"invalid{index}")
            invalid["payload"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "[]"
        else:
            invalid = call(tool="tool_call", call_id=f"invalid{index}", args={
                "name": "mcp_p2_mixed_blocked", "arguments": {} if invalid_kind == "probe" else {"value": "X"},
            })
        batches = [invalid, valid] if invalid_first else [valid, invalid]
        calls = [tc for batch in batches for tc in batch["payload"]["choices"][0]["message"]["tool_calls"]]
        return json_ok(tool_calls=calls, finish_reason="tool_calls")

    def configure(agent):
        agent.valid_tool_names.add("tool_call")
        agent.enabled_toolsets = ["mcp-p2-mixed"] if invalid_kind == "probe" else ["terminal"]
        agent.disabled_toolsets = []

    script = [mixed("a", i) if scenario == "repeated" else call(call_id=f"a{i}") for i in range(3)]
    script.append(mixed("b" if scenario == "novel" else "a", 3))
    if scenario == "novel":
        script += [call("c", call_id="continued"), json_ok(content="New strategy completed.")]
    agent, server, result, executions, db = loop(script, configure=configure)
    expected_paths = ["a"] * 3 + (["b", "c"] if scenario == "novel" else [])
    assert executions == [("read_file", {"path": path}) for path in expected_paths]
    assert len(loop.observations) == len(expected_paths)
    assert all(len(observation.results) == 1 for observation in loop.observations)
    assert nudge_count(server) == 1
    assert len(server.bodies) == (6 if scenario == "novel" else 4)
    assert result["completed"] is True
    if scenario == "novel":
        assert result["final_response"] == "New strategy completed."
        assert any(m.get("tool_call_id") == "invalid3" for m in result["messages"])
    else:
        assert result["turn_exit_reason"] == "guardrail_halt"
        assert not any(m.get("tool_call_id") == "valid3" for m in result["messages"])
    assert_clean(result["messages"])
    assert_clean(db.get_messages(agent.session_id))
    snapshot = json.loads((agent.logs_dir / f"session_{agent.session_id}.json").read_text(encoding="utf-8"))
    assert_clean(snapshot["messages"])
    reloaded = SessionDB(db_path=db.db_path)
    try:
        assert_clean(reloaded.get_messages_as_conversation(agent.session_id))
    finally:
        reloaded.close()


@pytest.mark.parametrize("stage", ["pre_tool_call", "approval", "tool_execution"])
def test_fresh_zero_dispatch_converges(loop, semantic_plugins, monkeypatch, stage):
    callbacks, approvals = [], []
    def deny(*args, **kwargs):
        approvals.append(1)
        return {"approved": False, "message": "Denied"}
    monkeypatch.setattr("tools.approval.request_tool_approval", deny)
    def block(**kwargs):
        callbacks.append(kwargs["tool_call_id"])
        if stage == "tool_execution":
            return '{"error":"Denied"}'
        return {"action": "approve" if stage == "approval" else "block", "message": "Denied"}
    register = semantic_plugins.register_middleware if stage == "tool_execution" else semantic_plugins.register_hook
    register("pre_tool_call" if stage == "approval" else stage, block)
    agent, server, result, executions, db = loop([call(call_id=f"blocked{i}") for i in range(6)])
    assert executions == []
    assert len(callbacks) == len(set(callbacks)) == 3
    assert len(approvals) == (3 if stage == "approval" else 0)
    assert len(server.bodies) == 4
    assert nudge_count(server) == 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_durable_clean(agent, result, db)


def test_post_nudge_reordered_writes_land(loop, tmp_path):
    target = tmp_path / "ordered.txt"
    def batch(order, i):
        item = call(tool="write_file", args={"path": str(target), "content": order[0]}, call_id=f"x{i}")
        item["payload"]["choices"][0]["message"]["tool_calls"].extend(
            call(tool="write_file", args={"path": str(target), "content": order[1]}, call_id=f"y{i}")["payload"]["choices"][0]["message"]["tool_calls"])
        return item
    def write(name, args):
        target.write_text(args["content"])
        return '{"success":true,"bytes_written":1}'
    agent, server, result, executions, db = loop(
        [batch("XY", i) for i in range(3)] + [batch("YX", 3), json_ok(content="Reordered work landed.")], output=write)
    assert [args["content"] for _, args in executions] == list("XYXYXYYX")
    assert target.read_text() == "X"
    assert nudge_count(server) == 1
    assert result["final_response"] == "Reordered work landed."
    assert_durable_clean(agent, result, db)


@pytest.mark.parametrize("post_nudge", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_concurrent_timeout_preserves_episode(loop, monkeypatch, post_nudge, missing):
    release = threading.Event()
    if missing:
        from concurrent.futures import Future
        from tools.daemon_pool import DaemonThreadPoolExecutor
        original_submit = DaemonThreadPoolExecutor.submit
        def no_worker_result(self, fn, *args, **kwargs):
            if not args:
                return original_submit(self, fn, *args, **kwargs)
            future = Future()
            future.set_result(None)
            return future
        monkeypatch.setattr("tools.daemon_pool.DaemonThreadPoolExecutor.submit", no_worker_result)
    monkeypatch.setattr("agent.tool_executor._resolve_concurrent_tool_timeout", lambda: 0.05)
    def batch(i):
        item = call("timeout-a", call_id=f"ta{i}")
        item["payload"]["choices"][0]["message"]["tool_calls"].extend(
            call("timeout-b", call_id=f"tb{i}")["payload"]["choices"][0]["message"]["tool_calls"])
        return item
    def output(name, args):
        if args["path"].startswith("timeout"):
            release.wait(10)
        return "same"
    try:
        script = ([call(call_id=f"a{i}") for i in range(3)] if post_nudge else []) + [batch(i) for i in range(6)]
        agent, server, result, executions, db = loop(script, output=output)
        assert len(server.bodies) == (5 if post_nudge else 4)
        assert len(executions) <= (5 if post_nudge else 6)
        assert nudge_count(server) == 1
        assert result["turn_exit_reason"] == "guardrail_halt"
        assert_durable_clean(agent, result, db)
    finally:
        release.set()


@pytest.mark.parametrize("kind", ["scope", "probe", "non_object"])
def test_fresh_invalid_no_effect_converges(loop, monkeypatch, kind):
    from tools.registry import registry
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    registry.register(name="mcp_p2_no_effect", toolset="mcp-p2-no-effect",
                      handler=lambda *args, **kwargs: pytest.fail("Invalid bridge dispatched"),
                      schema={"name": "mcp_p2_no_effect", "description": "Test",
                              "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                             "required": ["value"]}})
    def configure(agent):
        agent.valid_tool_names.add("tool_call")
        agent.enabled_toolsets = ["mcp-p2-no-effect"] if kind == "probe" else ["terminal"]
        agent.disabled_toolsets = []
    script = []
    for i in range(6):
        item = call(tool="tool_call", args={"name": "mcp_p2_no_effect", "arguments": {} if kind == "probe" else {"value": "x"}}, call_id=f"invalid{i}")
        if kind == "non_object":
            item = call(call_id=f"invalid{i}")
            item["payload"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "[]"
        script.append(item)
    agent, server, result, executions, db = loop(script, configure=configure)
    assert executions == []
    assert len(server.bodies) == 4
    assert nudge_count(server) == 1
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_durable_clean(agent, result, db)


@pytest.mark.parametrize("stage", ["pre_tool_call", "approval", "tool_execution"])
@pytest.mark.parametrize("post_nudge", [False, True])
def test_unique_no_effect_proposals_are_bounded(loop, semantic_plugins, monkeypatch, stage, post_nudge):
    callbacks, approvals = [], []
    def deny(*args, **kwargs):
        approvals.append(1)
        return {"approved": False, "message": f"Denial {len(approvals)}"}
    monkeypatch.setattr("tools.approval.request_tool_approval", deny)
    def policy(**kwargs):
        callbacks.append(kwargs["tool_call_id"])
        if kwargs["args"]["path"] == "a" and post_nudge:
            return kwargs["next_call"]() if stage == "tool_execution" else None
        if stage == "tool_execution":
            return json.dumps({"error": f"Denied attempt {len(callbacks)}"})
        return {"action": "approve" if stage == "approval" else "block", "message": f"Denied attempt {len(callbacks)}"}
    register = semantic_plugins.register_middleware if stage == "tool_execution" else semantic_plugins.register_hook
    register("pre_tool_call" if stage == "approval" else stage, policy)
    script = ([call(call_id=f"a{i}") for i in range(3)] if post_nudge else [])
    script += [call(f"blocked-{i}", call_id=f"b{i}") for i in range(7)]
    agent, server, result, executions, db = loop(script)
    assert len(executions) == (3 if post_nudge else 0)
    assert len(callbacks) == len(set(callbacks)) == 4
    assert len(approvals) == ((1 if post_nudge else 4) if stage == "approval" else 0)
    assert len(server.bodies) == 5
    assert nudge_count(server) == 1
    assert SEMANTIC_PROGRESS_NUDGE in json.dumps(server.bodies[3])
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert len(loop.observations) == 4
    assert_durable_clean(agent, result, db)


@pytest.mark.parametrize("scenario", ["fresh", "post_nudge", "unique"])
def test_sequential_timeout_is_not_progress(loop, semantic_plugins, monkeypatch, scenario):
    from tools.daemon_pool import DaemonThreadPoolExecutor
    release = threading.Event()
    futures, post_events = [], []
    original_submit = DaemonThreadPoolExecutor.submit
    def submit(self, fn, *args, **kwargs):
        future = original_submit(self, fn, *args, **kwargs)
        futures.append(future)
        return future
    monkeypatch.setattr(DaemonThreadPoolExecutor, "submit", submit)
    monkeypatch.setattr("agent.tool_executor._resolve_sequential_tool_timeout", lambda: 0.1)
    semantic_plugins.register_hook("post_tool_call", lambda **kwargs: post_events.append(kwargs["tool_call_id"]))
    def output(name, args):
        if args["path"].startswith("timeout"):
            assert release.wait(10), "fixture was not released"
        return "same"
    script = ([call(call_id=f"a{i}") for i in range(3)] if scenario == "post_nudge" else [])
    script += [call(f"timeout-{i if scenario == 'unique' else 0}", call_id=f"t{i}") for i in range(7)]
    try:
        agent, server, result, executions, db = loop(script, output=output)
        expected = 3 if scenario == "fresh" else 4
        assert len(executions) == expected
        assert len(server.bodies) == expected + 1
        assert nudge_count(server) == 1
        assert result["turn_exit_reason"] == "guardrail_halt"
        assert len(loop.observations) == expected
        assert sum(bool(o.results) for o in loop.observations) == (3 if scenario == "post_nudge" else 0)
        before = (len(executions), len(loop.observations), len(post_events), json.dumps(result["messages"]))
        assert len(post_events) == len(set(post_events)) == expected
        assert_durable_clean(agent, result, db)
    finally:
        release.set()
        for future in futures:
            future.result(timeout=5)
    assert before == (len(executions), len(loop.observations), len(post_events), json.dumps(result["messages"]))
    assert_durable_clean(agent, result, db)


@pytest.mark.parametrize("kind", ["scope", "probe", "non_object"])
def test_changing_invalid_proposals_are_bounded(loop, monkeypatch, kind):
    from tools.registry import registry
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    for i in range(7):
        registry.register(name=f"mcp_p2_churn_{i}", toolset="mcp-p2-churn",
                          handler=lambda *args, **kwargs: pytest.fail("Invalid underlying dispatch"),
                          schema={"name": f"mcp_p2_churn_{i}", "description": "Test",
                                  "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                                 "required": ["value"]}})
    def configure(agent):
        agent.valid_tool_names.add("tool_call")
        agent.enabled_toolsets = ["mcp-p2-churn"] if kind == "probe" else ["terminal"]
        agent.disabled_toolsets = []
    script = []
    for i in range(7):
        item = call(tool="tool_call", args={"name": f"mcp_p2_churn_{i if kind == 'scope' else 0}",
                    "arguments": {"other": str(i)} if kind == "probe" else {"value": "x"}}, call_id=f"invalid{i}")
        if kind == "non_object":
            item = call(call_id=f"invalid{i}")
            item["payload"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps([] if i == 0 else [str(i)])
        script.append(item)
    agent, server, result, executions, db = loop(script, configure=configure)
    assert executions == []
    assert len(server.bodies) == 5
    assert nudge_count(server) == 1
    assert len(loop.observations) == 4
    assert result["turn_exit_reason"] == "guardrail_halt"
    assert_durable_clean(agent, result, db)


def test_previously_blocked_strategy_can_land_and_rearm(loop, semantic_plugins):
    blocked = []
    def policy(**kwargs):
        if kwargs["args"]["path"] == "b" and not blocked:
            blocked.append(1)
            return {"action": "block", "message": "Temporarily denied"}
    semantic_plugins.register_hook("pre_tool_call", policy)
    agent, server, result, executions, db = loop(
        [call("b", call_id="initial-block")] + [call(call_id=f"a{i}") for i in range(3)]
        + [call("b", call_id="land-b"), call("c", call_id="land-c"), json_ok(content="New work landed.")])
    assert executions == [("read_file", {"path": p}) for p in ("a", "a", "a", "b", "c")]
    assert nudge_count(server) == 1
    assert result["final_response"] == "New work landed."
    assert_durable_clean(agent, result, db)

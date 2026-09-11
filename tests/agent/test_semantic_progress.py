"""Deterministic round contracts; all inputs are synthetic, non-private fixtures."""

import pytest

from agent import semantic_progress as sp


def round_(path="a", result="same", *, tool="read_file", args=None):
    return sp.SemanticRoundObservation.from_results([
        (tool, args if args is not None else {"path": path}, result)
    ])


@pytest.mark.parametrize("tool,result", [
    ("read_file", "same"), ("write_file", '{"bytes_written":4}'),
    ("plugin_effect", '{"success":true}'), ("terminal", '{"exit_code":1}'),
])
def test_third_identical_round_nudges_then_blocks(tool, result):
    tracker = sp.SemanticProgressTracker()
    observation = round_(tool=tool, result=result)
    assert [tracker.observe(observation).action for _ in range(3)] == [
        "allow", "allow", "nudge",
    ]
    assert tracker.guidance_pending
    assert tracker.mark_request_started().action == "nudge"
    assert tracker.mark_request_started().action == "allow"
    assert tracker.before_dispatch(observation.actions).action == "halt"


def test_period_two_cycle():
    tracker = sp.SemanticProgressTracker()
    a, b = round_("a"), round_("b")
    assert [tracker.observe(r).action for r in (a, b, a, b)] == [
        "allow", "allow", "allow", "nudge",
    ]
    assert tracker.mark_request_started().cycle == 2
    assert tracker.before_dispatch(a.actions).action == "halt"


def test_canonical_arguments_and_results():
    a = round_(args={"path": "a", "line": 1}, result='{"x":1,"y":2}')
    b = round_(args={"line": 1, "path": "a"}, result='{ "y": 2, "x": 1 }')
    assert a == b
    tracker = sp.SemanticProgressTracker()
    assert [tracker.observe(r).action for r in (a, b, a)] == ["allow", "allow", "nudge"]


@pytest.mark.parametrize("middle", [
    round_(args={"path": "a", "line": 200}),
    round_(tool="web_search", args={"query": "new query"}),
    round_(tool="patch", args={"path": "a", "patch": "new"}, result='{"success":true}'),
    round_(result="changed evidence"),
])
def test_intervening_progress_breaks_old_repetition(middle):
    tracker = sp.SemanticProgressTracker()
    a = round_()
    assert all(tracker.observe(r).action == "allow" for r in (a, a, middle, a, a))


def test_failure_to_success_is_new_evidence():
    tracker = sp.SemanticProgressTracker()
    failure = round_(tool="terminal", result='{"exit_code":1}')
    success = round_(tool="terminal", result='{"exit_code":0}')
    assert all(tracker.observe(r).action == "allow" for r in (failure, failure, success))


def test_strategy_change_rearms_even_for_older_action():
    tracker = sp.SemanticProgressTracker()
    a, c = round_("a"), round_("c")
    for r in (c, a, a, a):
        tracker.observe(r)
    tracker.mark_request_started()
    tracker.request_completed()
    assert tracker.before_dispatch(c.actions).action == "allow"
    assert not tracker.guidance_pending
    assert [tracker.observe(c).action for _ in range(3)] == ["allow", "allow", "nudge"]


def test_call_id_only_associates_proposal_and_execution():
    from agent.tool_guardrails import ToolCallGuardrailController, ToolCallSignature
    proposal = ToolCallSignature.from_call("read_file", {"path": "private proposal"})
    observations = []
    for call_id in ("first-id", "another-id"):
        guard = ToolCallGuardrailController()
        guard.start_semantic_round({call_id: proposal})
        guard.record_semantic_call(call_id, "read_file", {"path": "private effective"}, "private result",
                                   failed=False, dispatched=True, blocked=False)
        records = guard.take_semantic_round()
        assert len(records) == 1
        assert call_id not in repr(records)
        assert "private" not in repr(records)
        observations.append(sp.SemanticRoundObservation.from_executions(records))
    assert observations[0] == observations[1]
    assert observations[0].actions == {proposal}
    assert observations[0].results[0].signature != proposal


def test_only_durable_new_execution_rearms_spent_episode():
    from agent.tool_guardrails import SemanticToolObservation, ToolCallSignature
    tracker = sp.SemanticProgressTracker()
    a = round_()
    for _ in range(3):
        tracker.observe(a)
    tracker.mark_request_started()
    b = ToolCallSignature.from_call("read_file", {"path": "b"})
    assert tracker.before_dispatch([b]).action == "allow"
    assert tracker.before_dispatch(a.actions).action == "halt"  # no durable B yet
    blocked = sp.SemanticRoundObservation.from_executions([SemanticToolObservation(b, None, False, True)])
    tracker.observe(blocked)
    assert tracker.before_dispatch([b]).action == "halt"
    changed = sp.SemanticRoundObservation.from_executions([
        SemanticToolObservation(next(iter(a.actions)), round_("a2").results[0], True, False),
    ])
    tracker.observe(changed)
    assert tracker.before_dispatch(a.actions).action == "allow"


def test_redirect_and_independent_trackers_clear_pending_episode():
    first, sibling = sp.SemanticProgressTracker(), sp.SemanticProgressTracker()
    a = round_()
    for _ in range(3):
        first.observe(a)
    assert not sibling.guidance_pending
    first.reset()
    assert not first.guidance_pending
    assert first.before_dispatch(a.actions).action == "allow"
    assert first.observe(a).action == "allow"


def test_model_order_changes_identity():
    results = [("read_file", {"path": "a"}, "A"), ("read_file", {"path": "b"}, "B")]
    a = sp.SemanticRoundObservation.from_results(results)
    b = sp.SemanticRoundObservation.from_results(list(reversed(results)))
    assert a != b
    assert results[0][1] == {"path": "a"}
    tracker = sp.SemanticProgressTracker()
    assert [tracker.observe(r).action for r in (a, b, a)] == ["allow", "allow", "allow"]


@pytest.mark.parametrize("result", [
    {"_multimodal": True, "content": [{"type": "text", "text": "image"}]},
    [{"type": "text", "text": "plain result"}],
])
def test_structured_result_without_explicit_failure(result):
    from agent.tool_guardrails import fingerprint_tool_result
    assert not fingerprint_tool_result("vision_analyze", {}, result).failed


def test_request_rebuild_does_not_consume_pending_nudge():
    tracker = sp.SemanticProgressTracker()
    for _ in range(3):
        tracker.observe(round_())
    for _ in range(3):
        assert tracker.guidance_pending
    assert tracker.mark_request_started().action == "nudge"
    assert tracker.guidance_pending  # transport/fallback rebuild retains delivery
    tracker.request_completed()
    assert not tracker.guidance_pending
    assert tracker.before_dispatch(round_().actions).action == "halt"


def test_multimodal_capture_is_content_free_and_preserves_guardrail_behavior():
    from agent.tool_guardrails import ToolCallGuardrailController
    guard = ToolCallGuardrailController()
    result = {"_multimodal": True, "content": [{"type": "text", "text": "private image"}]}
    guard.start_semantic_round()
    decision = guard.after_call("vision_analyze", {"image": "private path"}, result, failed=False)
    evidence = guard.take_semantic_round()
    assert decision.action == "allow"
    assert len(evidence) == 1
    assert "private" not in repr(evidence)


def test_zero_effect_alternation_is_stagnation_not_progress():
    from agent.tool_guardrails import SemanticToolObservation, ToolCallSignature
    def denied(path):
        signature = ToolCallSignature.from_call("read_file", {"path": path})
        return sp.SemanticRoundObservation.from_executions([
            SemanticToolObservation(signature, None, False, True, "blocked", "stable-hash"),
        ])
    a, b = denied("a"), denied("b")
    tracker = sp.SemanticProgressTracker()
    assert [tracker.observe(r).action for r in (a, b, a)] == ["allow", "allow", "nudge"]
    tracker.mark_request_started()
    assert tracker.before_dispatch(a.proposal_sequence).action == "halt"
    c = denied("c")
    assert tracker.before_dispatch(c.proposal_sequence).action == "allow"
    tracker.observe(c)
    assert tracker.before_dispatch(c.proposal_sequence).action == "halt"
    assert tracker.before_dispatch(a.proposal_sequence).action == "halt"


def test_ordered_durable_evidence_rearms_but_timeout_does_not():
    from agent.tool_guardrails import SemanticToolObservation, ToolCallSignature
    x, y = round_(args={"path": "a", "content": "X"}, tool="write_file"), round_(args={"path": "a", "content": "Y"}, tool="write_file")
    xy = sp.SemanticRoundObservation.from_fingerprints(x.results + y.results)
    yx = sp.SemanticRoundObservation.from_fingerprints(y.results + x.results)
    tracker = sp.SemanticProgressTracker()
    for _ in range(3):
        tracker.observe(xy)
    tracker.mark_request_started()
    assert tracker.before_dispatch(yx.proposal_sequence).action == "allow"
    tracker.observe(yx)
    assert tracker.before_dispatch(xy.proposal_sequence).action == "allow"
    for _ in range(2):
        tracker.observe(yx)
    tracker.mark_request_started()
    novel = ToolCallSignature.from_call("read_file", {"path": "novel"})
    timeout = sp.SemanticRoundObservation.from_executions([
        SemanticToolObservation(novel, None, False, False, "timeout", "stable-timeout"),
    ])
    tracker.observe(timeout)
    assert tracker.before_dispatch(timeout.proposal_sequence).action == "halt"
    assert tracker.before_dispatch(yx.proposal_sequence).action == "halt"
    tracker.observe(round_("new-durable-evidence"))
    assert tracker.before_dispatch(yx.proposal_sequence).action == "allow"


def test_unique_no_effect_streak_and_exploration_are_bounded():
    from agent.tool_guardrails import SemanticToolObservation, ToolCallSignature
    def denied(i):
        return sp.SemanticRoundObservation.from_executions([
            SemanticToolObservation(ToolCallSignature.from_call("read_file", {"path": str(i)}),
                                    None, False, True, "blocked", str(i)),
        ])
    tracker = sp.SemanticProgressTracker()
    assert [tracker.observe(denied(i)).action for i in range(3)] == ["allow", "allow", "nudge"]
    tracker.mark_request_started()
    assert tracker.before_dispatch(denied(3).proposal_sequence).action == "allow"
    tracker.observe(denied(3))
    assert tracker.before_dispatch(denied(4).proposal_sequence).action == "halt"
    tracker.reset()  # an actual user redirect rebases even exhausted exploration
    assert tracker.before_dispatch(denied(4).proposal_sequence).action == "allow"
    assert tracker.observe(denied(4)).action == "allow"


def test_durable_execution_clears_no_effect_streak():
    from agent.tool_guardrails import SemanticToolObservation, ToolCallSignature
    denied = sp.SemanticRoundObservation.from_executions([
        SemanticToolObservation(ToolCallSignature.from_call("read_file", {"path": "blocked"}),
                                None, False, True, "blocked", "denied"),
    ])
    tracker = sp.SemanticProgressTracker()
    assert [tracker.observe(r).action for r in (denied, denied, round_("landed"), denied, denied)] == ["allow"] * 5
    assert tracker.observe(denied).action == "nudge"


@pytest.mark.parametrize("period", [1, 2])
def test_stale_progress_projection_ignores_no_effect_noise(period):
    from agent.tool_guardrails import SemanticToolObservation, ToolCallSignature
    def mixed(i):
        execution = round_("b" if period == 2 and i % 2 else "a").results[0]
        blocked = ToolCallSignature.from_call("read_file", {"path": f"blocked-{i}"})
        return sp.SemanticRoundObservation.from_executions([
            SemanticToolObservation(execution.signature, execution, True, False),
            SemanticToolObservation(blocked, None, False, True, "blocked", f"denial-{i}"),
        ])
    tracker = sp.SemanticProgressTracker()
    count = 3 if period == 1 else 4
    assert [tracker.observe(mixed(i)).action for i in range(count)] == ["allow"] * (count - 1) + ["nudge"]
    tracker.mark_request_started()
    assert tracker.before_dispatch(mixed(count).proposal_sequence).action == "allow"
    tracker.observe(mixed(count))
    assert tracker.before_dispatch(mixed(count + 1).proposal_sequence).action == "halt"

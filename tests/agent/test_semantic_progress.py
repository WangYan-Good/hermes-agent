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
    assert tracker.before_dispatch(c.actions).action == "allow"
    assert not tracker.guidance_pending
    assert [tracker.observe(c).action for _ in range(3)] == ["allow", "allow", "nudge"]


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


def test_parallel_order_does_not_change_identity():
    results = [("read_file", {"path": "a"}, "A"), ("read_file", {"path": "b"}, "B")]
    a = sp.SemanticRoundObservation.from_results(results)
    b = sp.SemanticRoundObservation.from_results(list(reversed(results)))
    assert a == b
    assert results[0][1] == {"path": "a"}
    tracker = sp.SemanticProgressTracker()
    assert [tracker.observe(r).action for r in (a, b, a)] == ["allow", "allow", "nudge"]


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

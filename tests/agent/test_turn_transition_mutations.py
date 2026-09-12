"""P4 mutation gates: one in-memory mutation in one isolated interpreter.

Production files are never rewritten. Each child runs the real behavioral
test and must fail its assertion, rather than failing to import or collect.
"""

import ast
import inspect
import subprocess
import sys
import textwrap

import pytest


def _rewrite_conditions(function, predicate):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    matched = []

    class Rewrite(ast.NodeTransformer):
        def visit_If(self, node):
            self.generic_visit(node)
            if predicate(ast.unparse(node.test)):
                matched.append(node.lineno)
                node.test = ast.Constant(False)
            return node

    tree = Rewrite().visit(tree)
    assert matched, "Mutation no longer matches the production condition"
    ast.fix_missing_locations(tree)
    namespace = {}
    exec(compile(tree, function.__code__.co_filename, "exec"), function.__globals__, namespace)
    return namespace[function.__name__]


def install_mutation(name):
    from agent import conversation_loop as loop
    from agent import request_cycle, turn_continuation, turn_state_machine

    patcher = pytest.MonkeyPatch()
    if name == "transport_as_length":
        patcher.setattr(loop, "is_transport_interrupted", lambda response: False)
    elif name == "streaming_recovery":
        patcher.setattr(loop, "run_request_cycle", _rewrite_conditions(
            request_cycle.run_request_cycle,
            lambda condition: condition == "_recovery.transport is not _loop.TransportRecoveryState.NONE",
        ))
    elif name == "replay_completed_tool":
        from run_agent import AIAgent
        original_execute = AIAgent._execute_tool_calls
        original_request = loop.run_request_cycle
        completed = []

        def execute(*args, **kwargs):
            value = original_execute(*args, **kwargs)
            completed.append((args, kwargs))
            return value

        def request(*args, **kwargs):
            if completed:
                saved_args, saved_kwargs = completed.pop()
                original_execute(*saved_args, **saved_kwargs)
            return original_request(*args, **kwargs)

        patcher.setattr(AIAgent, "_execute_tool_calls", execute)
        patcher.setattr(loop, "run_request_cycle", request)
    elif name == "compression_no_refund":
        patcher.setattr(request_cycle, "refund_step", lambda agent, count: count)
    elif name == "accept_crossed_response":
        patcher.setattr(loop, "run_request_cycle", _rewrite_conditions(
            request_cycle.run_request_cycle,
            lambda condition: condition == "_redirect_crossed_response",
        ))
    elif name == "fallback_stale_cache":
        def stale_cache(agent, messages, **kwargs):
            return messages, kwargs.get("moa_prepared"), kwargs["tools_for_api"]
        patcher.setattr(loop, "_redecorate_prompt_cache_for_provider", stale_cache)
    elif name == "continue_after_persistence_failure":
        patcher.setattr(loop, "resolve_tool_completion", lambda **kwargs: turn_state_machine.TurnReason.TOOLS_COMPLETED)
    elif name == "durable_semantic_nudge":
        original_request = loop.run_request_cycle

        def request(*args, **kwargs):
            for message in kwargs["api_messages"]:
                if message.get("content") == loop.SEMANTIC_PROGRESS_NUDGE:
                    kwargs["_ctx"].messages.append(dict(message))
            return original_request(*args, **kwargs)

        patcher.setattr(loop, "run_request_cycle", request)
    elif name == "dispatch_after_semantic_halt":
        from agent.semantic_progress import SemanticProgressDecision, SemanticProgressTracker
        patcher.setattr(SemanticProgressTracker, "before_dispatch", lambda *_args: SemanticProgressDecision())
    elif name == "lose_pending_answer":
        patcher.setattr(turn_continuation.TurnContinuation, "hold_answer", lambda *_args: None)
    elif name == "swallow_backoff_interrupt":
        # These are the request cycle's cancellation checks, including its
        # backoff checks. The test injects stop only from the sleep callback.
        patcher.setattr(loop, "run_request_cycle", _rewrite_conditions(
            request_cycle.run_request_cycle,
            lambda condition: condition == "agent._interrupt_requested",
        ))
    elif name == "apply_transition_twice":
        patcher.setattr(turn_state_machine.TurnController, "apply", _rewrite_conditions(
            turn_state_machine.TurnController.apply,
            lambda condition: "decision.revision != self.revision" in condition,
        ))
    else:
        raise AssertionError(f"Unknown mutation: {name}")


GATES = [
    ("transport_as_length", "tests/run_agent/test_turn_transition_characterization.py::test_network_attempt_and_model_step_have_distinct_budgets[transport_partial]"),
    ("streaming_recovery", "tests/run_agent/test_transport_recovery_convergence.py::TestDropBeforeAnyOutput::test_exactly_one_nonstreaming_retry_and_no_nudge"),
    ("replay_completed_tool", "tests/run_agent/test_transport_recovery_e2e.py::TestSideEffectAtMostOnceOverRealSockets::test_completed_tool_is_not_replayed_when_the_next_stream_drops"),
    ("compression_no_refund", "tests/run_agent/test_turn_transition_characterization.py::test_overflow_rebuild_refunds_logical_budget"),
    ("accept_crossed_response", "tests/run_agent/test_run_agent.py::TestRunConversation::test_redirect_wins_race_with_response_completion"),
    ("fallback_stale_cache", "tests/agent/test_failover_identity.py::TestRedecoratePromptCacheOnPolicyChange::test_replans_tools_for_the_active_destination"),
    ("continue_after_persistence_failure", "tests/run_agent/test_semantic_progress_guard.py::test_persistence_failure_prevents_semantic_continuation[True]"),
    ("durable_semantic_nudge", "tests/run_agent/test_semantic_progress_guard.py::test_nudge_accepts_strategy_change_or_final"),
    ("dispatch_after_semantic_halt", "tests/run_agent/test_semantic_progress_guard.py::test_period_two_halts_before_fifth_dispatch"),
    ("lose_pending_answer", "tests/run_agent/test_verification_continuation_budget.py::test_verify_on_stop_preserves_composed_report_at_budget_limit"),
    ("swallow_backoff_interrupt", "tests/run_agent/test_turn_transition_characterization.py::test_stop_during_generic_backoff_prevents_second_request"),
    ("apply_transition_twice", "tests/agent/test_turn_state_machine.py::test_decision_is_single_use_even_when_destination_is_same_state"),
]


@pytest.mark.parametrize("mutation,target", GATES, ids=[name for name, _ in GATES])
def test_mutation_is_killed_by_behavior(mutation, target):
    program = """
import runpy, sys
namespace = runpy.run_path('tests/agent/test_turn_transition_mutations.py')
namespace['install_mutation'](sys.argv[1])
import pytest
raise SystemExit(pytest.main([sys.argv[2], '-q', '--tb=short', '--disable-warnings']))
"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program), mutation, target],
        capture_output=True, text=True, timeout=90,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "FAILED " in output, output
    assert "ERROR collecting" not in output, output
    assert "assert " in output or "DID NOT RAISE" in output, output

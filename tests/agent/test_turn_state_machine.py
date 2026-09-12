"""Logical transitions are single-use decisions, separate from network retries."""

import pytest

from agent.turn_state_machine import (
    BudgetEffect, DurabilityBoundary, TransitionKind, TurnController,
    TurnPhase, TurnReason, resolve_request_exit, resolve_tool_completion,
)


def request_controller():
    controller = TurnController()
    controller.apply(controller.plan(TransitionKind.REQUEST, TurnReason.PREPARED))
    return controller


def test_retry_stays_in_request_and_cannot_dispatch_tools():
    controller = request_controller()
    for _ in range(3):
        decision = controller.plan(TransitionKind.RETRY_ATTEMPT, TurnReason.AUTH_RECOVERY)
        assert decision.budget == BudgetEffect.KEEP
        controller.apply(decision)
        assert controller.phase == TurnPhase.REQUEST
    with pytest.raises(ValueError):
        controller.plan(TransitionKind.TOOL_ROUND, TurnReason.TOOLS)


def test_decision_is_single_use_even_when_destination_is_same_state():
    controller = request_controller()
    decision = controller.plan(TransitionKind.RETRY_ATTEMPT, TurnReason.TRANSPORT_RETRY)
    controller.apply(decision)
    with pytest.raises(ValueError):
        controller.apply(decision)


def test_alternative_decisions_cannot_both_execute():
    controller = request_controller()
    first = controller.plan(TransitionKind.REBUILD, TurnReason.COMPRESSION)
    second = controller.plan(TransitionKind.REBUILD, TurnReason.PROVIDER_SWITCH)
    controller.apply(first)
    with pytest.raises(ValueError):
        controller.apply(second)


@pytest.mark.parametrize("reason", [TurnReason.INTERRUPT, TurnReason.FAILURE, TurnReason.FINAL_RESPONSE])
def test_terminal_cannot_restart(reason):
    controller = request_controller()
    controller.apply(controller.plan(TransitionKind.FINALIZE, reason))
    controller.apply(controller.plan(TransitionKind.DONE, reason))
    for kind in (TransitionKind.REQUEST, TransitionKind.REBUILD, TransitionKind.NEXT_STEP):
        with pytest.raises(ValueError):
            controller.plan(kind, TurnReason.PREPARED)


def test_request_checkpoint_preserves_redirect_and_interrupt_precedence():
    assert resolve_request_exit(TurnReason.REDIRECT, interrupted=True) == TurnReason.REDIRECT
    assert resolve_request_exit(TurnReason.COMPRESSION, interrupted=True) == TurnReason.INTERRUPT
    assert resolve_request_exit(TurnReason.PROVIDER_SWITCH, interrupted=False) == TurnReason.PROVIDER_SWITCH


def test_tool_result_persistence_failure_precedes_guardrail_and_next_request():
    assert resolve_tool_completion(persistence_failed=True, guardrail_halted=True) == TurnReason.PERSISTENCE_FAILURE
    assert resolve_tool_completion(persistence_failed=False, guardrail_halted=True) == TurnReason.GUARDRAIL
    assert resolve_tool_completion(persistence_failed=False, guardrail_halted=False) == TurnReason.TOOLS_COMPLETED


def test_rebuild_and_continuation_have_explicit_distinct_effects():
    controller = request_controller()
    compressed = controller.plan(
        TransitionKind.REBUILD, TurnReason.COMPRESSION,
        budget=BudgetEffect.REFUND_STEP,
        durability=DurabilityBoundary.COMPRESSION,
    )
    partial = controller.plan(
        TransitionKind.NEXT_STEP, TurnReason.TRANSPORT_PARTIAL,
        durability=DurabilityBoundary.RECOVERY_SCAFFOLD,
    )
    assert compressed.rebuild_request and partial.rebuild_request
    assert compressed.budget == BudgetEffect.REFUND_STEP
    assert partial.budget == BudgetEffect.KEEP
    assert partial.durability == DurabilityBoundary.RECOVERY_SCAFFOLD


def test_tool_round_is_reachable_only_after_response_and_cannot_be_retried():
    controller = request_controller()
    controller.apply(controller.plan(TransitionKind.INTERPRET, TurnReason.RESPONSE))
    controller.apply(controller.plan(
        TransitionKind.TOOL_ROUND, TurnReason.TOOLS,
        durability=DurabilityBoundary.TOOL_CALLS,
    ))
    with pytest.raises(ValueError):
        controller.plan(TransitionKind.RETRY_ATTEMPT, TurnReason.TRANSPORT_RETRY)
    controller.apply(controller.plan(
        TransitionKind.NEXT_STEP, TurnReason.TOOLS_COMPLETED,
        durability=DurabilityBoundary.TOOL_RESULTS,
    ))
    assert controller.phase == TurnPhase.PREPARE


@pytest.mark.parametrize("reason,refund", [
    (TurnReason.COMPRESSION, True), (TurnReason.PROVIDER_SWITCH, True),
    (TurnReason.REDIRECT, True), (TurnReason.LENGTH, False),
    (TurnReason.TRANSPORT_PARTIAL, False),
])
def test_cycle_applies_selected_budget_once(reason, refund):
    from types import SimpleNamespace
    from agent.iteration_budget import IterationBudget
    from agent.request_cycle import RequestCycle

    agent = SimpleNamespace(iteration_budget=IterationBudget(5))
    assert agent.iteration_budget.consume()
    controller = request_controller()
    cycle = RequestCycle(3)
    cycle.restart(controller, reason)
    with pytest.raises(ValueError):
        cycle.restart(controller, TurnReason.PROVIDER_SWITCH)
    assert cycle.apply_restart(controller, agent, 1) == (0 if refund else 1)
    assert agent.iteration_budget.used == (0 if refund else 1)
    with pytest.raises(ValueError):
        cycle.apply_restart(controller, agent, 1)
    assert agent.iteration_budget.used == (0 if refund else 1)


def test_guards_survive_network_retries_but_not_a_rebuilt_cycle():
    from agent.request_cycle import RequestCycle

    controller = request_controller()
    cycle = RequestCycle(3)
    cycle.recovery.codex_auth_retry_attempted = True
    controller.move(TransitionKind.RETRY_ATTEMPT, TurnReason.AUTH_RECOVERY)
    assert cycle.recovery.codex_auth_retry_attempted
    rebuilt = RequestCycle(3)
    assert not rebuilt.recovery.codex_auth_retry_attempted
    assert rebuilt.pending is None

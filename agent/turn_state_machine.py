"""Logical-turn control, independent of providers, tools, and storage.

Policies remain with their existing owners. A decision records the boundary
they selected; the controller validates and commits exactly one decision at
that boundary. Revisions prevent two candidates from applying side effects to
the same step. No transcript, provider payload, or callable lives in an event.
"""

from dataclasses import dataclass
from enum import Enum


class TurnPhase(Enum):
    PREPARE = "prepare"
    REQUEST = "request"
    INTERPRET = "interpret"
    TOOL_ROUND = "tool_round"
    FINALIZE = "finalize"
    DONE = "done"


class TransitionKind(Enum):
    REQUEST = "request"
    RETRY_ATTEMPT = "retry_attempt"
    REBUILD = "rebuild"
    NEXT_STEP = "next_step"
    INTERPRET = "interpret"
    TOOL_ROUND = "tool_round"
    FINALIZE = "finalize"
    DONE = "done"


class TurnReason(Enum):
    PREPARED = "prepared"
    RESPONSE = "response"
    GENERIC_RETRY = "generic_retry"
    INVALID_RESPONSE = "invalid_response"
    AUTH_RECOVERY = "auth_recovery"
    PAYLOAD_REPAIR = "payload_repair"
    RATE_LIMIT = "rate_limit"
    PRIMARY_RECOVERY = "primary_recovery"
    PROVIDER_SWITCH = "provider_switch"
    COMPRESSION = "compression"
    TRANSPORT_RETRY = "transport_retry"
    TRANSPORT_PARTIAL = "transport_partial"
    LENGTH = "length"
    TRUNCATED_TOOL_CALL = "truncated_tool_call"
    SCRATCHPAD = "scratchpad"
    CODEX_INCOMPLETE = "codex_incomplete"
    CODEX_ACK = "codex_ack"
    INVALID_TOOL = "invalid_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    DROPPED_TOOL_CALL = "dropped_tool_call"
    POST_TOOL_EMPTY = "post_tool_empty"
    THINKING_PREFILL = "thinking_prefill"
    EMPTY_RESPONSE = "empty_response"
    VERIFICATION = "verification"
    PRE_VERIFY = "pre_verify"
    KANBAN_STOP = "kanban_stop"
    TOOLS = "tools"
    TOOLS_COMPLETED = "tools_completed"
    REDIRECT = "redirect"
    INTERRUPT = "interrupt"
    GUARDRAIL = "guardrail"
    PERSISTENCE_FAILURE = "persistence_failure"
    BUDGET = "budget"
    HANDOFF = "handoff"
    FINAL_RESPONSE = "final_response"
    PARTIAL_RESPONSE = "partial_response"
    FAILURE = "failure"
    PROCESSING_ERROR = "processing_error"


class BudgetEffect(Enum):
    KEEP = "keep"
    REFUND_STEP = "refund_api_count_and_iteration"
    REFUND_ITERATION = "refund_iteration_only"


class DurabilityBoundary(Enum):
    NONE = "none"
    API_ONLY = "api_only"
    RECOVERY_SCAFFOLD = "recovery_scaffold"
    COMPRESSION = "compression"
    REDIRECT = "redirect"
    TOOL_CALLS = "tool_calls"
    TOOL_RESULTS = "tool_results"
    VERIFICATION_CANDIDATE = "verification_candidate_best_effort"
    FINAL_ANSWER = "final_answer_best_effort"


@dataclass(frozen=True)
class TurnTransition:
    source: TurnPhase
    revision: int
    kind: TransitionKind
    reason: TurnReason
    budget: BudgetEffect = BudgetEffect.KEEP
    durability: DurabilityBoundary = DurabilityBoundary.NONE

    @property
    def rebuild_request(self) -> bool:
        return self.kind in {TransitionKind.REBUILD, TransitionKind.NEXT_STEP}


_ACTIVE = frozenset({TurnPhase.PREPARE, TurnPhase.REQUEST, TurnPhase.INTERPRET, TurnPhase.TOOL_ROUND})
_ROUTES = {
    TransitionKind.REQUEST: (frozenset({TurnPhase.PREPARE}), TurnPhase.REQUEST),
    TransitionKind.RETRY_ATTEMPT: (frozenset({TurnPhase.REQUEST}), TurnPhase.REQUEST),
    TransitionKind.REBUILD: (_ACTIVE, TurnPhase.PREPARE),
    TransitionKind.NEXT_STEP: (_ACTIVE, TurnPhase.PREPARE),
    TransitionKind.INTERPRET: (frozenset({TurnPhase.REQUEST}), TurnPhase.INTERPRET),
    TransitionKind.TOOL_ROUND: (frozenset({TurnPhase.INTERPRET}), TurnPhase.TOOL_ROUND),
    TransitionKind.FINALIZE: (_ACTIVE, TurnPhase.FINALIZE),
    TransitionKind.DONE: (frozenset({TurnPhase.FINALIZE}), TurnPhase.DONE),
}


class TurnController:
    """One controller per run_conversation, never shared with worker threads."""

    def __init__(self):
        self.phase = TurnPhase.PREPARE
        self.revision = 0
        self.last_transition: TurnTransition | None = None

    def plan(
        self, kind: TransitionKind, reason: TurnReason, *,
        budget: BudgetEffect = BudgetEffect.KEEP,
        durability: DurabilityBoundary = DurabilityBoundary.NONE,
    ) -> TurnTransition:
        allowed, _ = _ROUTES[kind]
        if self.phase not in allowed:
            raise ValueError(f"Cannot {kind.value} from {self.phase.value}")
        return TurnTransition(self.phase, self.revision, kind, reason, budget, durability)

    def apply(self, decision: TurnTransition) -> TurnTransition:
        if decision.revision != self.revision or decision.source is not self.phase:
            raise ValueError("Stale or already applied turn transition")
        allowed, destination = _ROUTES[decision.kind]
        if self.phase not in allowed:
            raise ValueError(f"Cannot {decision.kind.value} from {self.phase.value}")
        self.phase = destination
        self.revision += 1
        self.last_transition = decision
        return decision

    def move(
        self, kind: TransitionKind, reason: TurnReason, *,
        budget: BudgetEffect = BudgetEffect.KEEP,
        durability: DurabilityBoundary = DurabilityBoundary.NONE,
    ) -> TurnTransition:
        return self.apply(self.plan(kind, reason, budget=budget, durability=durability))


def resolve_request_exit(pending: TurnReason | None, *, interrupted: bool) -> TurnReason:
    """The existing post-attempt checkpoint, NOT a global priority order.

    The caller already resolved hard stop versus redirect under the existing
    redirect lock. A selected redirect is consumed before the local interrupted
    flag; otherwise interruption wins over compression/provider restart.
    """
    if pending is TurnReason.REDIRECT:
        return pending
    if interrupted:
        return TurnReason.INTERRUPT
    return pending or TurnReason.RESPONSE


def resolve_tool_completion(*, persistence_failed: bool, guardrail_halted: bool) -> TurnReason:
    """Only durable tool results can reach semantic observation/continuation."""
    if persistence_failed:
        return TurnReason.PERSISTENCE_FAILURE
    if guardrail_halted:
        return TurnReason.GUARDRAIL
    return TurnReason.TOOLS_COMPLETED


def complete_finalized_turn(controller, result):
    if controller.last_transition is None:
        raise ValueError("Cannot complete a turn without a finalization decision")
    controller.move(TransitionKind.DONE, controller.last_transition.reason)
    return result


def complete_direct_turn(controller, result):
    # The direct path has already performed its historical cleanup/persist
    # contract. Do not run normal finalizer hooks a second time.
    reason = (
        TurnReason.INTERRUPT if result.get("interrupted")
        else TurnReason.PARTIAL_RESPONSE if result.get("partial")
        else TurnReason.FAILURE
    )
    controller.move(TransitionKind.FINALIZE, reason)
    return complete_finalized_turn(controller, result)

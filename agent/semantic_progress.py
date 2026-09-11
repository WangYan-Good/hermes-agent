"""Bounded, deterministic progress detection for completed logical tool rounds.

The loop owns persistence and delivery; this controller sees only fingerprints.
It never classifies provider attempts or internal continuations as tool work.
"""

from collections import deque
from dataclasses import dataclass, field

from agent.tool_guardrails import ToolCallSignature, ToolResultFingerprint, fingerprint_tool_result


SEMANTIC_PROGRESS_NUDGE = (
    "You are repeating tool actions without producing new evidence or state changes. "
    "Do not repeat the same tool calls unchanged. Use the results already available, "
    "change strategy materially, or conclude with the concrete blocker."
)
SEMANTIC_PROGRESS_HALT = (
    "Stopped this tool loop because recent rounds repeated the same actions and results "
    "without new evidence or state changes. Work completed before the loop has been "
    "preserved. A new user instruction can redirect or resume the task."
)


@dataclass(frozen=True)
class SemanticRoundObservation:
    results: tuple[ToolResultFingerprint, ...]
    proposals: tuple[ToolCallSignature, ...] = field(default_factory=tuple, compare=False)
    no_effects: tuple[tuple[ToolCallSignature, str, str], ...] = ()

    @classmethod
    def from_fingerprints(cls, results):
        # The executor collects parallel results in model-call order. Preserve
        # that order: sequential mutations need not commute.
        return cls(tuple(results))

    @classmethod
    def from_results(cls, results):
        return cls.from_fingerprints(fingerprint_tool_result(*r) for r in results)

    @classmethod
    def from_executions(cls, records):
        # Correlation already happened by call ID in the executor collector.
        # No-effect outcomes identify stagnant rounds without becoming
        # positive execution evidence that could rearm a spent episode.
        observation = cls.from_fingerprints(
            record.execution for record in records if record.dispatched and record.execution is not None
        )
        return cls(
            observation.results, tuple(record.proposal_signature for record in records),
            tuple((r.proposal_signature, r.outcome_kind, r.outcome_hash) for r in records
                  if not r.dispatched or r.execution is None),
        )

    @property
    def actions(self) -> frozenset[ToolCallSignature]:
        return frozenset(self.proposal_sequence)

    @property
    def proposal_sequence(self) -> tuple[ToolCallSignature, ...]:
        return self.proposals or tuple(r.signature for r in self.results)


@dataclass(frozen=True)
class SemanticProgressDecision:
    action: str = "allow"
    stalled_rounds: int = 0
    cycle: int = 0
    unique_actions: int = 0
    tool_count: int = 0


class SemanticProgressTracker:
    """A four-round suffix suffices for period-1 and period-2 evidence.

    New actions/results naturally break suffix equality, including revisiting
    an older action after a mutation. Success alone never clears history.
    After delivery, proposals made solely of stalled actions are stopped before
    dispatch, with their execution order preserved. Novel proposals may attempt a
    strategy; only durable new execution/evidence rearms the guard.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._rounds = deque(maxlen=4)
        self._stalled_actions = frozenset()
        self._stalled_evidence = set()
        self._stalled_sequences = set()
        self._decision = SemanticProgressDecision()
        self._nudged = False
        self.guidance_pending = False

    def observe(self, observation: SemanticRoundObservation) -> SemanticProgressDecision:
        if self._nudged:
            if observation.results and observation.results not in self._stalled_evidence:
                # A proposed strategy is only progress once real execution and
                # its evidence have crossed the loop's durability boundary.
                self.reset()
            else:
                # One exploratory novel proposal may be blocked or rewrite to
                # old work. It must not buy another no-progress execution cycle.
                self._stalled_actions |= observation.actions
                self._stalled_sequences.add(observation.proposal_sequence)
                return SemanticProgressDecision()
        if not observation.results and not observation.no_effects:
            return SemanticProgressDecision()
        self._rounds.append(observation)
        rounds = list(self._rounds)
        cycle = 0
        if len(rounds) >= 3 and rounds[-1] == rounds[-2] == rounds[-3]:
            cycle = 1
        elif len(rounds) == 4 and rounds[:2] == rounds[2:]:
            cycle = 2
        if not cycle or self._stalled_actions:
            return SemanticProgressDecision()
        self._stalled_actions = frozenset().union(*(r.actions for r in rounds[-cycle:]))
        self._stalled_evidence = {r.results for r in rounds[-cycle:]}
        self._stalled_sequences = {r.proposal_sequence for r in rounds[-cycle:]}
        self._decision = SemanticProgressDecision(
            "nudge", 3 if cycle == 1 else 4, cycle,
            len(self._stalled_actions), len(observation.results),
        )
        self.guidance_pending = True
        return self._decision

    def mark_request_started(self) -> SemanticProgressDecision:
        if self.guidance_pending and not self._nudged:
            self._nudged = True
            return self._decision
        return SemanticProgressDecision()

    def request_completed(self):
        self.guidance_pending = False

    def before_dispatch(self, actions) -> SemanticProgressDecision:
        if not self._nudged or not actions:
            return SemanticProgressDecision()
        sequence = tuple(actions)
        if sequence in self._stalled_sequences or (len(sequence) == 1 and sequence[0] in self._stalled_actions):
            d = self._decision
            return SemanticProgressDecision("halt", d.stalled_rounds + 1, d.cycle,
                                            d.unique_actions, len(actions))
        return SemanticProgressDecision()

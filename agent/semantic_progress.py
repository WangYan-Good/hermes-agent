"""Bounded, deterministic progress detection for completed logical tool rounds.

The loop owns persistence and delivery; this controller sees only fingerprints.
It never classifies provider attempts or internal continuations as tool work.
"""

from collections import deque
from dataclasses import dataclass

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

    @classmethod
    def from_fingerprints(cls, results):
        return cls(tuple(sorted(results, key=lambda r: (
            r.signature.tool_name, r.signature.args_hash, r.result_hash, r.failed, r.landed,
        ))))

    @classmethod
    def from_results(cls, results):
        return cls.from_fingerprints(fingerprint_tool_result(*r) for r in results)

    @property
    def actions(self) -> frozenset[ToolCallSignature]:
        return frozenset(r.signature for r in self.results)


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
    dispatch, including split/reordered batches. A novel action rearms the guard.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._rounds = deque(maxlen=4)
        self._stalled_actions = frozenset()
        self._decision = SemanticProgressDecision()
        self._nudged = False
        self.guidance_pending = False

    def observe(self, observation: SemanticRoundObservation) -> SemanticProgressDecision:
        if not observation.results:
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
        if not self._nudged:
            return SemanticProgressDecision()
        if actions and set(actions) <= self._stalled_actions:
            d = self._decision
            return SemanticProgressDecision("halt", d.stalled_rounds + 1, d.cycle,
                                            d.unique_actions, len(actions))
        self.reset()
        return SemanticProgressDecision()

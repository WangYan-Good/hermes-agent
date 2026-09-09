"""Bounded recovery policy for mid-stream transport interruptions.

A streaming model request that dies mid-body — ``peer closed connection``,
``incomplete chunked read``, ``RemoteProtocolError``, an SSE stream that
simply stops before its terminator — is NOT the same failure as a provider
that completed the response protocol and reported ``finish_reason=length``.

Historically Hermes conflated the two: the streaming layer swallowed the
transport error into a ``PARTIAL_STREAM_STUB_ID`` stub stamped
``finish_reason="length"``, and the turn loop then handed that stub the full
genuine-output-truncation budget (4 continuation nudges, or 4 truncated
tool-call retries with a doubling ``max_tokens``). Each of those attempts is
itself a fresh API call that can drop again and re-enter its own transport
retry budget, so ONE dropped connection could multiply into a long chain of
requests, continuation nudges, and repeated planning/execution.

This module owns the replacement policy. One logical transport interruption
gets **at most one strategy change** — streaming → non-streaming — and then
must resolve into success, provider fallback, or an honest terminal result.
It never escalates the output-token budget (a network drop is not an output
cap) and it never switches back to streaming to try again.

The policy is a pure function of (state, whether visible text already
reached the user) so the turn loop's branch stays small and the decision
itself is unit-testable without an agent, a client, or a socket.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class TransportRecoveryState(enum.Enum):
    """How much bounded transport recovery this turn has already spent.

    Turn-scoped: a fresh turn starts at ``NONE``, and a recovery that
    *succeeds* returns to ``NONE`` so a genuinely separate interruption
    later in the same turn gets its own (equally bounded) budget. What the
    state forbids is a second recovery for the SAME incident.
    """

    NONE = "none"
    #: A non-streaming retry of the original logical request was issued.
    NONSTREAM_RETRY = "nonstream_retry"
    #: A single non-streaming continuation from already-delivered visible
    #: text was issued.
    PARTIAL_CONTINUATION = "partial_continuation"
    #: Recovery is spent — the only moves left are fallback or terminal.
    EXHAUSTED = "exhausted"


class TransportRecoveryAction(enum.Enum):
    """What the turn loop should do about this interruption."""

    #: Discard whatever partial candidate arrived and re-issue the SAME
    #: logical request without streaming. No synthetic prompt is appended.
    NONSTREAM_RETRY = "nonstream_retry"
    #: Visible text already reached the user, so the request cannot simply be
    #: replayed (the user would see the text twice). Checkpoint the partial
    #: text and ask for exactly one non-streaming continuation.
    PARTIAL_CONTINUATION = "partial_continuation"
    #: Bounded recovery is spent — hand off to the fallback chain, or return
    #: an honest partial/failed result.
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class TransportRecoveryPlan:
    """The decision, plus the state to store for the next interruption."""

    action: TransportRecoveryAction
    next_state: TransportRecoveryState

    @property
    def force_nonstreaming(self) -> bool:
        """True while a recovery attempt is in flight.

        The streaming path is what just failed; re-entering it is how the old
        behavior looped. Both live recovery states pin the next request to the
        non-streaming path.
        """
        return self.next_state in {
            TransportRecoveryState.NONSTREAM_RETRY,
            TransportRecoveryState.PARTIAL_CONTINUATION,
        }


def plan_transport_recovery(
    *,
    state: TransportRecoveryState,
    has_visible_text: bool,
) -> TransportRecoveryPlan:
    """Decide how to recover from one mid-stream transport interruption.

    Args:
        state: Recovery already spent on the current incident.
        has_visible_text: Whether model-authored text from this response was
            already delivered to the user. Hermes-authored warnings do not
            count — only text the user would see duplicated if the request
            were replayed from scratch.

    Returns:
        The action to take and the state to remember.

    The budget is deliberately one attempt, not a tunable count: a transport
    drop that survives a non-streaming retry is not a transient hiccup, and
    re-trying it is precisely the multiplicative recovery this replaces.
    """
    if state is not TransportRecoveryState.NONE:
        return TransportRecoveryPlan(
            action=TransportRecoveryAction.EXHAUSTED,
            next_state=TransportRecoveryState.EXHAUSTED,
        )
    if has_visible_text:
        return TransportRecoveryPlan(
            action=TransportRecoveryAction.PARTIAL_CONTINUATION,
            next_state=TransportRecoveryState.PARTIAL_CONTINUATION,
        )
    return TransportRecoveryPlan(
        action=TransportRecoveryAction.NONSTREAM_RETRY,
        next_state=TransportRecoveryState.NONSTREAM_RETRY,
    )


# ── Transport identity on a response object ────────────────────────────

#: Set on the swallowed-stream stub by the streaming layer. This is the
#: precise, machine-decidable signal that a response is a transport
#: interruption rather than a completed provider response.
TRANSPORT_INTERRUPTED_ATTR = "_transport_interrupted"
#: Set alongside it: whether MODEL-authored text from this response already
#: reached the user. Hermes-authored warning text is excluded deliberately —
#: it is not content the model would duplicate on a replay.
TRANSPORT_VISIBLE_TEXT_ATTR = "_transport_visible_text"


def is_transport_interrupted(response: Any) -> bool:
    """True when ``response`` is a swallowed mid-stream transport failure.

    Prefers the explicit marker stamped by the streaming layer. Falls back to
    the legacy ``PARTIAL_STREAM_STUB_ID`` shape so stubs built before the
    marker existed (persisted transcripts, older adapters, hand-built test
    fixtures) still take the bounded path rather than silently inheriting the
    genuine-output-truncation budget.
    """
    if getattr(response, TRANSPORT_INTERRUPTED_ATTR, False):
        return True
    from hermes_constants import PARTIAL_STREAM_STUB_ID

    return getattr(response, "id", "") == PARTIAL_STREAM_STUB_ID


def transport_had_visible_text(response: Any, *, fallback_content: Any = None) -> bool:
    """Whether model text from this interrupted response reached the user.

    Uses the explicit marker when the streaming layer stamped one. Without it
    (legacy stubs) fall back to "the stub carries content", which is the best
    available approximation.
    """
    marker = getattr(response, TRANSPORT_VISIBLE_TEXT_ATTR, None)
    if marker is not None:
        return bool(marker)
    if fallback_content is not None:
        return bool(str(fallback_content).strip())
    try:
        choices = getattr(response, "choices", None) or []
        content = getattr(getattr(choices[0], "message", None), "content", None)
        return bool(str(content or "").strip())
    except Exception:
        return False


# ── Raw transport exceptions ───────────────────────────────────────────


def is_transport_retryable_error(classified: Any) -> bool:
    """True when an already-classified error is a transport drop the turn owns.

    A streaming request only produces a ``PARTIAL_STREAM_STUB_ID`` stub when
    deltas already reached the consumer. A drop with nothing delivered — the
    most common shape — raises instead, so the bounded policy has to accept
    RAW exceptions too, or Case A/C silently keep the old multiplicative path.

    This takes the turn's existing :class:`ClassifiedError` rather than
    re-deriving one. The classifier already knows ``RemoteProtocolError``,
    ``incomplete chunked read``, ``peer closed connection``, ``ConnectError``
    and the SSL/timeout families, and — critically — it resolves the
    *ambiguous* cases against session size, routing a bare disconnect on an
    oversized request to ``context_overflow`` (compress) rather than
    transport. Reusing that single verdict keeps this decision from ever
    disagreeing with the one the rest of the turn acts on.

    Accepting only ``FailoverReason.timeout`` leaves rate limits, billing,
    auth, content policy, overload, 413/context overflow and deterministic
    client errors on their existing paths. ``ssl_cert_verification`` is
    excluded by that same test: it is deterministic for the host, so a
    non-streaming retry would reproduce the identical handshake failure.
    """
    from agent.error_classifier import FailoverReason

    return (
        getattr(classified, "reason", None) is FailoverReason.timeout
        and bool(getattr(classified, "retryable", False))
    )

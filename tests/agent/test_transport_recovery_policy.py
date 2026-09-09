"""Unit tests for the bounded transport-recovery policy.

``agent.transport_recovery`` owns the decision "what do we do about ONE
mid-stream connection drop". It is a pure function of (state already spent,
whether visible model text reached the user), so it is pinned here without an
agent, a client, or a socket. The loop wiring is covered end-to-end in
``tests/run_agent/test_transport_recovery_convergence.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.transport_recovery import (
    TRANSPORT_INTERRUPTED_ATTR,
    TRANSPORT_VISIBLE_TEXT_ATTR,
    TransportRecoveryAction,
    TransportRecoveryState,
    is_transport_interrupted,
    plan_transport_recovery,
    transport_had_visible_text,
)
from hermes_constants import PARTIAL_STREAM_STUB_ID


class TestFirstInterruption:
    """A fresh incident picks its single strategy from what the user saw."""

    def test_no_visible_text_retries_without_streaming(self):
        plan = plan_transport_recovery(
            state=TransportRecoveryState.NONE, has_visible_text=False,
        )
        assert plan.action is TransportRecoveryAction.NONSTREAM_RETRY
        assert plan.next_state is TransportRecoveryState.NONSTREAM_RETRY
        assert plan.force_nonstreaming is True

    def test_visible_text_continues_instead_of_replaying(self):
        """Replaying a request whose text already reached the user would show
        that text twice — checkpoint and continue instead."""
        plan = plan_transport_recovery(
            state=TransportRecoveryState.NONE, has_visible_text=True,
        )
        assert plan.action is TransportRecoveryAction.PARTIAL_CONTINUATION
        assert plan.next_state is TransportRecoveryState.PARTIAL_CONTINUATION
        assert plan.force_nonstreaming is True


class TestBudgetIsExactlyOne:
    """Mutation gate: raising the transport budget above one must fail here."""

    @pytest.mark.parametrize("spent", [
        TransportRecoveryState.NONSTREAM_RETRY,
        TransportRecoveryState.PARTIAL_CONTINUATION,
        TransportRecoveryState.EXHAUSTED,
    ])
    @pytest.mark.parametrize("visible", [True, False])
    def test_second_interruption_is_always_exhausted(self, spent, visible):
        plan = plan_transport_recovery(state=spent, has_visible_text=visible)
        assert plan.action is TransportRecoveryAction.EXHAUSTED, (
            "One transport incident gets ONE strategy change. A second "
            "attempt is the multiplicative recovery this policy replaces."
        )
        assert plan.next_state is TransportRecoveryState.EXHAUSTED

    def test_exhausted_never_forces_another_request(self):
        plan = plan_transport_recovery(
            state=TransportRecoveryState.NONSTREAM_RETRY, has_visible_text=False,
        )
        assert plan.force_nonstreaming is False, (
            "EXHAUSTED means hand off or stop — not 'issue another request'."
        )


class TestNoStreamingReentry:
    """Mutation gate: a recovery attempt must never go back to streaming."""

    @pytest.mark.parametrize("visible", [True, False])
    def test_live_recovery_pins_nonstreaming(self, visible):
        plan = plan_transport_recovery(
            state=TransportRecoveryState.NONE, has_visible_text=visible,
        )
        assert plan.force_nonstreaming is True


class TestTransportIdentity:
    """The stub's transport identity must be machine-decidable, not inferred
    from the id + finish_reason pair it shares with genuine truncation."""

    def test_explicit_marker_identifies_a_drop(self):
        resp = SimpleNamespace(id="chatcmpl-real", **{TRANSPORT_INTERRUPTED_ATTR: True})
        assert is_transport_interrupted(resp) is True

    def test_legacy_stub_id_still_identified(self):
        """Stubs built before the marker existed (persisted transcripts, older
        adapters) must not silently inherit the genuine-length budget."""
        resp = SimpleNamespace(id=PARTIAL_STREAM_STUB_ID)
        assert is_transport_interrupted(resp) is True

    def test_ordinary_response_is_not_a_drop(self):
        resp = SimpleNamespace(id="chatcmpl-123")
        assert is_transport_interrupted(resp) is False

    def test_genuine_length_response_is_not_a_drop(self):
        """Mutation gate: a real output-cap truncation must never be routed
        into transport recovery."""
        resp = SimpleNamespace(
            id="chatcmpl-abc",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="half an answer"),
                finish_reason="length",
            )],
        )
        assert is_transport_interrupted(resp) is False


class TestVisibleTextDetection:
    def test_marker_wins_over_stub_content(self):
        """Hermes appends its own "stream stalled mid tool-call" warning to
        the stub's content. That warning is not model output, so it must not
        make the loop think the user saw partial text."""
        resp = SimpleNamespace(**{
            TRANSPORT_INTERRUPTED_ATTR: True,
            TRANSPORT_VISIBLE_TEXT_ATTR: False,
        })
        assert transport_had_visible_text(
            resp, fallback_content="\n\n⚠ Stream stalled mid tool-call (write_file)",
        ) is False

    def test_marker_true_reports_visible(self):
        resp = SimpleNamespace(**{TRANSPORT_VISIBLE_TEXT_ATTR: True})
        assert transport_had_visible_text(resp) is True

    def test_legacy_stub_falls_back_to_content(self):
        resp = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            choices=[SimpleNamespace(message=SimpleNamespace(content="part one "))],
        )
        assert transport_had_visible_text(resp) is True

    def test_legacy_empty_stub_reports_no_visible_text(self):
        resp = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
        )
        assert transport_had_visible_text(resp) is False

    def test_whitespace_only_is_not_visible_text(self):
        resp = SimpleNamespace(id=PARTIAL_STREAM_STUB_ID)
        assert transport_had_visible_text(resp, fallback_content="   \n ") is False

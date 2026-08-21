"""Output-subscription authorization state for the TUI gateway.

Phase 3B-1 stores subscription contracts and lifecycle state only.  A
subscription does *not* imply event delivery; session events continue to use
the existing owner-only ``write_json`` routing until the later fan-out phase.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


OUTPUT_OBSERVABLE_EVENTS = frozenset({
    "background.complete",
    "error",
    "message.complete",
    "message.delta",
    "message.interim",
    "message.start",
    "status.update",
    "subagent.complete",
    "subagent.progress",
    "subagent.spawn_requested",
    "subagent.start",
    "tool.complete",
    "tool.progress",
    "tool.start",
})

MAX_OUTPUT_SUBSCRIPTIONS_PER_TRANSPORT = 16


def classify_session_event(event: str) -> str:
    """Classify a session event for future observer routing.

    The default is deliberately owner-only: newly-added control or privileged
    events cannot become observable until they are explicitly reviewed and
    added to :data:`OUTPUT_OBSERVABLE_EVENTS`.
    """

    return "output" if event in OUTPUT_OBSERVABLE_EVENTS else "owner_only"


@dataclass(frozen=True)
class OutputSubscription:
    """One transport's authorization to observe one live output session."""

    profile_scope: str
    subscriber_session_id: str
    # Deliberately opaque and process-local. Runtime ids and durable keys can
    # both be reused while an earlier Session object is still tearing down.
    subscriber_session_incarnation: object
    subscriber_transport: object
    target_session_id: str
    target_session_key: str
    target_session_incarnation: object


class OutputSubscriptionRegistry:
    """Thread-safe bidirectional index of output subscription contracts.

    The registry never writes to a transport.  Its lock therefore protects
    only bounded in-memory state and can safely sit after the gateway's
    ``_sessions_lock`` in the global lock order.
    """

    def __init__(self, *, max_subscriptions_per_transport: int) -> None:
        self._max_subscriptions_per_transport = max(
            1, int(max_subscriptions_per_transport)
        )
        self._lock = threading.RLock()
        self._by_target_incarnation: dict[object, set[OutputSubscription]] = {}
        self._by_anchor_incarnation: dict[object, set[OutputSubscription]] = {}
        self._by_transport: dict[object, set[OutputSubscription]] = {}

    def subscribe(self, record: OutputSubscription) -> str:
        """Add or refresh *record*, returning ``added``, ``existing``, or ``limit``."""

        with self._lock:
            transport_records = self._by_transport.setdefault(
                record.subscriber_transport, set()
            )
            if record in transport_records:
                return "existing"
            if len(transport_records) >= self._max_subscriptions_per_transport:
                if not transport_records:
                    self._by_transport.pop(record.subscriber_transport, None)
                return "limit"

            transport_records.add(record)
            self._by_target_incarnation.setdefault(
                record.target_session_incarnation, set()
            ).add(record)
            self._by_anchor_incarnation.setdefault(
                record.subscriber_session_incarnation, set()
            ).add(record)
            return "added"

    def unsubscribe(
        self,
        transport: object,
        target_session_id: str,
        *,
        subscriber_session_id: str | None = None,
        subscriber_session_incarnation: object | None = None,
    ) -> bool:
        """Remove one subscription, optionally requiring its anchor to match."""

        with self._lock:
            records = [
                record
                for record in self._by_transport.get(transport, set())
                if record.target_session_id == target_session_id
                and (
                    subscriber_session_id is None
                    or record.subscriber_session_id == subscriber_session_id
                )
                and (
                    subscriber_session_incarnation is None
                    or record.subscriber_session_incarnation
                    is subscriber_session_incarnation
                )
            ]
            if not records:
                return False
            for record in records:
                self._remove_record(record)
            return True

    def remove_transport(self, transport: object) -> int:
        """Remove every subscription owned by *transport* in O(its subscriptions)."""

        with self._lock:
            records = list(self._by_transport.get(transport, set()))
            for record in records:
                self._remove_record(record)
            return len(records)

    def remove_session(
        self,
        target_session_id: str,
        *,
        target_session_key: str | None = None,
        target_session_incarnation: object | None = None,
    ) -> int:
        """Remove subscribers of one target session or one exact incarnation."""

        with self._lock:
            if target_session_incarnation is not None:
                candidates = self._by_target_incarnation.get(
                    target_session_incarnation, set()
                )
            else:
                candidates = {
                    record
                    for records in self._by_transport.values()
                    for record in records
                }
            records = [
                record
                for record in candidates
                if record.target_session_id == target_session_id
                and (
                    target_session_key is None
                    or record.target_session_key == target_session_key
                )
            ]
            for record in records:
                self._remove_record(record)
            return len(records)

    def remove_anchor(
        self,
        transport: object,
        subscriber_session_id: str,
        *,
        subscriber_session_incarnation: object | None = None,
    ) -> int:
        """Drop subscriptions whose request-time owner anchor is no longer owned."""

        with self._lock:
            if subscriber_session_incarnation is not None:
                candidates = self._by_anchor_incarnation.get(
                    subscriber_session_incarnation, set()
                )
            else:
                candidates = self._by_transport.get(transport, set())
            records = [
                record
                for record in candidates
                if record.subscriber_transport is transport
                and record.subscriber_session_id == subscriber_session_id
                and (
                    subscriber_session_incarnation is None
                    or record.subscriber_session_incarnation
                    is subscriber_session_incarnation
                )
            ]
            for record in records:
                self._remove_record(record)
            return len(records)

    def subscribers_for_session(
        self,
        target_session_id: str,
        *,
        target_session_incarnation: object | None = None,
    ) -> frozenset[object]:
        with self._lock:
            if target_session_incarnation is not None:
                records = self._by_target_incarnation.get(
                    target_session_incarnation, set()
                )
            else:
                records = {
                    record
                    for transport_records in self._by_transport.values()
                    for record in transport_records
                }
            return frozenset(
                record.subscriber_transport
                for record in records
                if record.target_session_id == target_session_id
            )

    def session_ids_for_transport(self, transport: object) -> frozenset[str]:
        with self._lock:
            return frozenset(
                record.target_session_id
                for record in self._by_transport.get(transport, set())
            )

    def subscription_for(
        self,
        transport: object,
        target_session_id: str,
        *,
        subscriber_session_incarnation: object | None = None,
        target_session_incarnation: object | None = None,
    ) -> OutputSubscription | None:
        with self._lock:
            return next(
                (
                    record
                    for record in self._by_transport.get(transport, set())
                    if record.target_session_id == target_session_id
                    and (
                        subscriber_session_incarnation is None
                        or record.subscriber_session_incarnation
                        is subscriber_session_incarnation
                    )
                    and (
                        target_session_incarnation is None
                        or record.target_session_incarnation
                        is target_session_incarnation
                    )
                ),
                None,
            )

    def _remove_record(self, record: OutputSubscription) -> None:
        transport_records = self._by_transport.get(record.subscriber_transport)
        if transport_records is not None:
            transport_records.discard(record)
            if not transport_records:
                self._by_transport.pop(record.subscriber_transport, None)

        target_records = self._by_target_incarnation.get(
            record.target_session_incarnation
        )
        if target_records is not None:
            target_records.discard(record)
            if not target_records:
                self._by_target_incarnation.pop(record.target_session_incarnation, None)

        anchor_records = self._by_anchor_incarnation.get(
            record.subscriber_session_incarnation
        )
        if anchor_records is not None:
            anchor_records.discard(record)
            if not anchor_records:
                self._by_anchor_incarnation.pop(
                    record.subscriber_session_incarnation, None
                )

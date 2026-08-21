"""Bounded asynchronous delivery of subscribed Session output.

The source Session thread only performs a non-blocking queue insertion.  One
worker per observer transport owns physical delivery, which keeps a slow or
dead observer from adding latency to the Session owner.  Queue items retain
the complete immutable subscription record and are re-authorized immediately
before sending so lifecycle cleanup invalidates stale queued output.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable

from tui_gateway.output_subscriptions import (
    OutputSubscription,
    OutputSubscriptionRegistry,
)


logger = logging.getLogger(__name__)

OBSERVER_QUEUE_CAPACITY = 256
OBSERVER_SEND_TIMEOUT_S = 10.0
_WorkerThread = threading.Thread


@dataclass(frozen=True)
class _DeliveryItem:
    subscription: OutputSubscription
    frame: dict


@dataclass
class _WorkerState:
    transport: object
    items: queue.Queue[_DeliveryItem]
    wake: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    dropped_reason: str | None = None
    dropped_record: OutputSubscription | None = None
    thread: threading.Thread | None = None


class ObserverDeliveryManager:
    """Own bounded FIFO workers for all subscribed observer transports."""

    def __init__(
        self,
        registry: OutputSubscriptionRegistry,
        *,
        queue_capacity: int = OBSERVER_QUEUE_CAPACITY,
        send_timeout_s: float = OBSERVER_SEND_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._queue_capacity = max(1, int(queue_capacity))
        self._send_timeout_s = max(0.001, float(send_timeout_s))
        self._lock = threading.RLock()
        self._workers: dict[object, _WorkerState] = {}
        self._closed = False

    def enqueue(self, subscription: OutputSubscription, frame: dict) -> bool:
        """Queue one observer frame without waiting for transport I/O."""

        if not self._registry.contains(subscription):
            return False
        transport = subscription.subscriber_transport
        with self._lock:
            if self._closed:
                return False
            state = self._workers.get(transport)
            if state is None or state.stop.is_set():
                state = self._start_worker(transport)
            try:
                state.items.put_nowait(_DeliveryItem(subscription, frame))
            except queue.Full:
                # Fail closed immediately.  The worker observes the drop flag,
                # emits the best-effort owner-only notice, then exits.
                self._registry.remove_transport(transport)
                state.dropped_reason = "observer_queue_overflow"
                state.dropped_record = subscription
                state.stop.set()
                state.wake.set()
                return False
            state.wake.set()
            return True

    def remove_transport(self, transport: object) -> None:
        """Stop one transport worker without producing a dropped notice."""

        with self._lock:
            state = self._workers.get(transport)
            if state is None:
                return
            state.stop.set()
            state.wake.set()

    def close(self) -> None:
        """Stop every worker; intended for process/test teardown."""

        with self._lock:
            self._closed = True
            states = list(self._workers.values())
            for state in states:
                state.stop.set()
                state.wake.set()
        for state in states:
            thread = state.thread
            if thread is not None:
                thread.join(timeout=self._send_timeout_s + 0.2)

    def _start_worker(self, transport: object) -> _WorkerState:
        state = _WorkerState(
            transport=transport,
            items=queue.Queue(maxsize=self._queue_capacity),
        )
        thread = _WorkerThread(
            target=self._run_worker,
            args=(state,),
            name=f"output-observer-{id(transport):x}",
            daemon=True,
        )
        state.thread = thread
        self._workers[transport] = state
        thread.start()
        return state

    def _run_worker(self, state: _WorkerState) -> None:
        try:
            while True:
                state.wake.wait(0.1)
                state.wake.clear()
                if state.stop.is_set():
                    break

                batch: list[_DeliveryItem] = []
                while True:
                    try:
                        batch.append(state.items.get_nowait())
                    except queue.Empty:
                        break
                if not batch:
                    continue

                valid_items = [
                    item
                    for item in batch
                    if self._registry.authorizes_delivery(item.subscription)
                ]
                if not valid_items:
                    continue
                if not self._send(
                    state.transport,
                    [
                        (
                            item.frame,
                            lambda record=item.subscription: (
                                self._registry.authorizes_delivery(record)
                            ),
                        )
                        for item in valid_items
                    ],
                ):
                    self._registry.remove_transport(state.transport)
                    state.dropped_reason = "observer_send_failed"
                    state.dropped_record = valid_items[-1].subscription
                    state.stop.set()
                    break
        except Exception:
            logger.exception("observer output worker crashed")
            self._registry.remove_transport(state.transport)
        finally:
            if state.dropped_reason and state.dropped_record is not None:
                self._send_dropped_notice(
                    state.transport,
                    state.dropped_record,
                    state.dropped_reason,
                )
            with self._lock:
                if self._workers.get(state.transport) is state:
                    self._workers.pop(state.transport, None)

    def _send(
        self,
        transport: object,
        deliveries: list[tuple[dict, Callable[[], bool] | None]],
    ) -> bool:
        observer_writer: Callable[..., bool] | None = getattr(
            transport, "write_observer_batch", None
        )
        if observer_writer is not None:
            try:
                return bool(
                    observer_writer(
                        [frame for frame, _validator in deliveries],
                        timeout=self._send_timeout_s,
                        validators=[validator for _frame, validator in deliveries],
                    )
                )
            except Exception:
                logger.debug("observer batch send failed", exc_info=True)
                return False

        writer: Callable[[dict], bool] | None = getattr(transport, "write", None)
        if writer is None:
            return False
        try:
            return all(
                writer(frame)
                for frame, validator in deliveries
                if validator is None or validator()
            )
        except Exception:
            logger.debug("observer transport write failed", exc_info=True)
            return False

    def _send_dropped_notice(
        self,
        transport: object,
        record: OutputSubscription,
        reason: str,
    ) -> None:
        # Direct transport delivery deliberately bypasses Session fan-out.  The
        # notice is control-plane data for this observer's owned anchor only.
        notice = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.output_dropped",
                "session_id": record.subscriber_session_id,
                "payload": {
                    "reason": reason,
                    "target_session_id": record.target_session_id,
                },
            },
        }
        self._send(transport, [(notice, None)])

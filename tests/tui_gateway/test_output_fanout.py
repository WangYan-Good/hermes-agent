"""Phase 3B-2 contracts for bounded, non-blocking observer delivery."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tui_gateway import server
from tui_gateway.output_delivery import ObserverDeliveryManager
from tui_gateway.output_subscriptions import (
    OUTPUT_OBSERVABLE_EVENTS,
    OutputSubscription,
    OutputSubscriptionRegistry,
)
from tui_gateway.transport import Transport, bind_transport, reset_transport
from tui_gateway.ws import WSTransport


class RecordingTransport(Transport):
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.written = threading.Event()

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        self.written.set()
        return True

    def close(self) -> None:
        return None


class ObserverTransport(RecordingTransport):
    def __init__(self, *, block: bool = False, result: bool = True) -> None:
        super().__init__()
        self.block = block
        self.result = result
        self.send_started = threading.Event()
        self.release_send = threading.Event()
        self.observer_batches: list[list[dict]] = []

    def write_observer_batch(
        self,
        frames: list[dict],
        *,
        timeout: float,
        validators=None,
    ) -> bool:
        self.send_started.set()
        if self.block and not self.release_send.wait(timeout):
            return False
        validators = validators or [None] * len(frames)
        authorized = [
            frame
            for frame, validator in zip(frames, validators, strict=True)
            if validator is None or validator()
        ]
        self.observer_batches.append(authorized)
        self.frames.extend(authorized)
        self.written.set()
        return self.result


def _session(transport: Transport, *, key: str) -> dict:
    return server._prepare_output_session_record({
        "agent": object(),
        "hidden": False,
        "profile_home": "/profiles/default",
        "running": True,
        "session_key": key,
        "source": "test",
        "transport": transport,
    })


def _rpc(transport: Transport, method: str, params: dict) -> dict:
    token = bind_transport(transport)
    try:
        return server.handle_request({"id": "phase-3b2", "method": method, "params": params})
    finally:
        reset_transport(token)


def _event_types(transport: RecordingTransport) -> list[str]:
    return [frame["params"]["type"] for frame in transport.frames]


def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


@pytest.fixture()
def fanout_state(monkeypatch):
    previous_sessions = dict(server._sessions)
    server._sessions.clear()
    registry = OutputSubscriptionRegistry(max_subscriptions_per_transport=16)
    delivery = ObserverDeliveryManager(registry, queue_capacity=256, send_timeout_s=0.1)
    monkeypatch.setattr(server, "_output_subscriptions", registry)
    monkeypatch.setattr(server, "_output_delivery", delivery)
    try:
        yield registry, delivery
    finally:
        delivery.close()
        server._sessions.clear()
        server._sessions.update(previous_sessions)


def _subscribe(observer: Transport, owner: Transport) -> None:
    server._sessions.update({
        "sid-a": _session(observer, key="stored-a"),
        "sid-b": _session(owner, key="stored-b"),
    })
    response = _rpc(
        observer,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    assert response["result"]["rejected"] == []


@pytest.mark.parametrize("event", sorted(OUTPUT_OBSERVABLE_EVENTS))
def test_whitelisted_output_event_reaches_owner_then_observer(fanout_state, event):
    order: list[str] = []

    class OrderedTransport(ObserverTransport):
        def __init__(self, label: str) -> None:
            super().__init__()
            self.label = label

        def write(self, obj: dict) -> bool:
            order.append(self.label)
            return super().write(obj)

        def write_observer_batch(
            self, frames: list[dict], *, timeout: float, validators=None
        ) -> bool:
            order.append(self.label)
            return super().write_observer_batch(
                frames, timeout=timeout, validators=validators
            )

    observer = OrderedTransport("observer")
    owner = OrderedTransport("owner")
    _subscribe(observer, owner)

    server._emit(event, "sid-b", {"sequence": 1})
    _wait_for(lambda: _event_types(observer) == [event])

    assert _event_types(owner) == [event]
    assert order[:2] == ["owner", "observer"]


@pytest.mark.parametrize("event", ["approval.request", "session.owner_lost", "future.event"])
def test_control_and_unknown_events_remain_owner_only(fanout_state, event):
    observer = ObserverTransport()
    owner = RecordingTransport()
    _subscribe(observer, owner)

    server._emit(event, "sid-b", {"private": True})
    time.sleep(0.02)

    assert _event_types(owner) == [event]
    assert observer.frames == []


def test_multiple_subscribers_receive_one_copy_and_owner_transport_is_deduplicated(
    fanout_state,
):
    owner = ObserverTransport()
    observer = ObserverTransport()
    server._sessions.update({
        "sid-owner-anchor": _session(owner, key="stored-owner-anchor"),
        "sid-target": _session(owner, key="stored-target"),
        "sid-observer": _session(observer, key="stored-observer"),
    })
    _rpc(
        owner,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-owner-anchor", "session_ids": ["sid-target"]},
    )
    _rpc(
        observer,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-observer", "session_ids": ["sid-target"]},
    )

    server._emit("message.delta", "sid-target", {"text": "one"})
    _wait_for(lambda: len(observer.frames) == 1)

    assert len(owner.frames) == 1
    assert len(observer.frames) == 1
    assert owner.observer_batches == []


def test_runtime_watch_stream_takeover_preserves_single_owner_and_isolates_unwatched_transport(
    fanout_state,
):
    """Exercise the production runtime sequence validated with three browser tabs.

    A watches B while C stays unobserved, B streams several ordered frames, and
    A then explicitly takes control.  The transfer must notify only B's former
    owner and subsequent output must reach A exactly once through the owner
    path, never through a stale observation contract.
    """

    observer_a = ObserverTransport()
    owner_b = RecordingTransport()
    unobserved_c = ObserverTransport()
    target_b = _session(owner_b, key="stored-b")
    target_b.update({
        "created_at": time.time(),
        "display_history_prefix": [],
        "history": [],
        "history_lock": threading.RLock(),
    })
    server._sessions.update({
        "sid-a": _session(observer_a, key="stored-a"),
        "sid-b": target_b,
        "sid-c": _session(unobserved_c, key="stored-c"),
    })

    subscribed = _rpc(
        observer_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    assert subscribed["result"]["rejected"] == []

    for event, payload in (
        ("message.start", {"turn": "runtime"}),
        ("message.delta", {"text": "one"}),
        ("message.delta", {"text": "two"}),
        ("message.complete", {"status": "complete"}),
    ):
        server._emit(event, "sid-b", payload)

    expected = [
        "message.start",
        "message.delta",
        "message.delta",
        "message.complete",
    ]
    _wait_for(lambda: _event_types(observer_a) == expected)
    assert _event_types(owner_b) == expected
    assert unobserved_c.frames == []
    observer_batch_count = len(observer_a.observer_batches)

    activated = _rpc(
        observer_a,
        "session.activate",
        {"session_id": "sid-b", "omit_messages": True},
    )

    assert activated["result"]["session_id"] == "sid-b"
    assert server._sessions["sid-b"]["transport"] is observer_a
    assert _event_types(owner_b) == [*expected, "session.owner_lost"]
    assert owner_b.frames[-1]["params"]["payload"] == {
        "new_owner": True,
        "reason": "take_control",
    }
    assert _event_types(observer_a) == expected
    assert unobserved_c.frames == []

    server._emit("message.delta", "sid-b", {"text": "after takeover"})
    _wait_for(lambda: _event_types(observer_a) == [*expected, "message.delta"])

    assert len(observer_a.observer_batches) == observer_batch_count
    assert unobserved_c.frames == []


def test_multiple_anchor_contracts_on_one_observer_transport_are_deduplicated(
    fanout_state,
):
    observer = ObserverTransport(block=True)
    owner = RecordingTransport()
    server._sessions.update({
        "sid-a1": _session(observer, key="stored-a1"),
        "sid-a2": _session(observer, key="stored-a2"),
        "sid-target": _session(owner, key="stored-target"),
    })
    for anchor in ("sid-a1", "sid-a2"):
        response = _rpc(
            observer,
            "session.output_subscribe",
            {"subscriber_session_id": anchor, "session_ids": ["sid-target"]},
        )
        assert response["result"]["rejected"] == []

    server._emit("message.delta", "sid-target", {"text": "once"})
    assert observer.send_started.wait(1)
    _rpc(
        observer,
        "session.output_unsubscribe",
        {"subscriber_session_id": "sid-a1", "session_ids": ["sid-target"]},
    )
    observer.release_send.set()
    _wait_for(lambda: len(observer.frames) == 1)

    assert _event_types(observer) == ["message.delta"]


def test_queued_record_stays_authorized_by_another_anchor_on_same_transport():
    registry = OutputSubscriptionRegistry(max_subscriptions_per_transport=16)
    delivery = ObserverDeliveryManager(registry, queue_capacity=8, send_timeout_s=1.0)
    transport = ObserverTransport(block=True)
    target_incarnation = object()

    def record(anchor: str) -> OutputSubscription:
        return OutputSubscription(
            profile_scope="/profiles/default",
            subscriber_session_id=anchor,
            subscriber_session_incarnation=object(),
            subscriber_transport=transport,
            target_session_id="sid-target",
            target_session_key="stored-target",
            target_session_incarnation=target_incarnation,
        )

    selected = record("sid-a1")
    alternate = record("sid-a2")
    registry.subscribe(selected)
    registry.subscribe(alternate)
    delivery.enqueue(
        selected,
        server._event_frame("message.delta", "sid-target", {"text": "once"}),
    )
    assert transport.send_started.wait(1)

    registry.remove_anchor(
        transport,
        "sid-a1",
        subscriber_session_incarnation=selected.subscriber_session_incarnation,
    )
    transport.release_send.set()
    _wait_for(lambda: len(transport.frames) == 1)

    assert registry.contains(selected) is False
    assert registry.contains(alternate) is True
    assert _event_types(transport) == ["message.delta"]
    delivery.close()


def test_unsubscribe_invalidates_an_already_queued_record_before_send(fanout_state):
    registry, delivery = fanout_state
    transport = ObserverTransport(block=True)
    anchor_incarnation = object()
    target_incarnation = object()
    first = OutputSubscription(
        profile_scope="/profiles/default",
        subscriber_session_id="sid-a",
        subscriber_session_incarnation=anchor_incarnation,
        subscriber_transport=transport,
        target_session_id="sid-b",
        target_session_key="stored-b",
        target_session_incarnation=target_incarnation,
    )
    second = OutputSubscription(
        profile_scope="/profiles/default",
        subscriber_session_id="sid-a",
        subscriber_session_incarnation=anchor_incarnation,
        subscriber_transport=transport,
        target_session_id="sid-c",
        target_session_key="stored-c",
        target_session_incarnation=object(),
    )
    registry.subscribe(first)
    registry.subscribe(second)
    delivery.enqueue(first, server._event_frame("message.delta", "sid-b", {"n": 1}))
    assert transport.send_started.wait(1)
    delivery.enqueue(second, server._event_frame("message.delta", "sid-c", {"n": 2}))

    registry.unsubscribe(transport, "sid-c", subscriber_session_id="sid-a")
    transport.release_send.set()
    _wait_for(lambda: len(transport.frames) >= 1)
    time.sleep(0.02)

    assert [frame["params"]["session_id"] for frame in transport.frames] == ["sid-b"]


def test_slow_observer_never_blocks_source_owner_and_timeout_revokes_transport():
    registry = OutputSubscriptionRegistry(max_subscriptions_per_transport=16)
    delivery = ObserverDeliveryManager(registry, queue_capacity=4, send_timeout_s=0.03)
    transport = ObserverTransport(block=True)
    record = OutputSubscription(
        profile_scope="/profiles/default",
        subscriber_session_id="sid-a",
        subscriber_session_incarnation=object(),
        subscriber_transport=transport,
        target_session_id="sid-b",
        target_session_key="stored-b",
        target_session_incarnation=object(),
    )
    registry.subscribe(record)
    started = time.monotonic()
    assert delivery.enqueue(
        record, server._event_frame("message.delta", "sid-b", {"text": "slow"})
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.02
    assert transport.send_started.wait(1)
    _wait_for(lambda: registry.session_ids_for_transport(transport) == frozenset())
    delivery.close()


def test_observer_queue_overflow_revokes_all_contracts_without_blocking():
    registry = OutputSubscriptionRegistry(max_subscriptions_per_transport=16)
    delivery = ObserverDeliveryManager(registry, queue_capacity=2, send_timeout_s=1.0)
    transport = ObserverTransport(block=True)
    record = OutputSubscription(
        profile_scope="/profiles/default",
        subscriber_session_id="sid-a",
        subscriber_session_incarnation=object(),
        subscriber_transport=transport,
        target_session_id="sid-b",
        target_session_key="stored-b",
        target_session_incarnation=object(),
    )
    registry.subscribe(record)
    frame = server._event_frame("message.delta", "sid-b", {"text": "queued"})
    assert delivery.enqueue(record, frame)
    assert transport.send_started.wait(1)
    assert delivery.enqueue(record, frame)
    assert delivery.enqueue(record, frame)

    started = time.monotonic()
    assert delivery.enqueue(record, frame) is False
    elapsed = time.monotonic() - started

    assert elapsed < 0.02
    assert registry.session_ids_for_transport(transport) == frozenset()
    transport.release_send.set()
    _wait_for(
        lambda: "session.output_dropped"
        in [frame["params"]["type"] for frame in transport.frames]
    )
    delivery.close()


def test_replacement_incarnation_discards_queued_old_output_but_delivers_new():
    registry = OutputSubscriptionRegistry(max_subscriptions_per_transport=16)
    delivery = ObserverDeliveryManager(registry, queue_capacity=8, send_timeout_s=1.0)
    transport = ObserverTransport(block=True)
    anchor_incarnation = object()
    old_incarnation = object()
    new_incarnation = object()

    def record(incarnation: object) -> OutputSubscription:
        return OutputSubscription(
            profile_scope="/profiles/default",
            subscriber_session_id="sid-a",
            subscriber_session_incarnation=anchor_incarnation,
            subscriber_transport=transport,
            target_session_id="same-sid",
            target_session_key="same-key",
            target_session_incarnation=incarnation,
        )

    old = record(old_incarnation)
    new = record(new_incarnation)
    registry.subscribe(old)
    delivery.enqueue(
        old, server._event_frame("message.delta", "same-sid", {"sequence": 1})
    )
    assert transport.send_started.wait(1)
    delivery.enqueue(
        old, server._event_frame("message.delta", "same-sid", {"sequence": 2})
    )
    registry.remove_session(
        "same-sid", target_session_incarnation=old_incarnation
    )
    registry.subscribe(new)
    delivery.enqueue(
        new, server._event_frame("message.delta", "same-sid", {"sequence": 3})
    )

    transport.release_send.set()
    _wait_for(lambda: len(transport.frames) == 1)

    assert [
        frame["params"]["payload"]["sequence"] for frame in transport.frames
    ] == [3]
    delivery.close()


@pytest.mark.parametrize("cleanup", ["hidden_or_close", "disconnect"])
def test_lifecycle_cleanup_invalidates_queued_output(cleanup):
    registry = OutputSubscriptionRegistry(max_subscriptions_per_transport=16)
    delivery = ObserverDeliveryManager(registry, queue_capacity=8, send_timeout_s=1.0)
    transport = ObserverTransport(block=True)
    incarnation = object()
    record = OutputSubscription(
        profile_scope="/profiles/default",
        subscriber_session_id="sid-a",
        subscriber_session_incarnation=object(),
        subscriber_transport=transport,
        target_session_id="sid-b",
        target_session_key="stored-b",
        target_session_incarnation=incarnation,
    )
    registry.subscribe(record)
    delivery.enqueue(
        record, server._event_frame("message.delta", "sid-b", {"sequence": 1})
    )
    assert transport.send_started.wait(1)
    delivery.enqueue(
        record, server._event_frame("message.delta", "sid-b", {"sequence": 2})
    )

    if cleanup == "disconnect":
        registry.remove_transport(transport)
    else:
        registry.remove_session("sid-b", target_session_incarnation=incarnation)
    transport.release_send.set()
    time.sleep(0.02)

    assert transport.frames == []
    delivery.close()


def test_per_transport_fifo_preserves_session_emit_order(fanout_state):
    observer = ObserverTransport()
    owner = RecordingTransport()
    _subscribe(observer, owner)

    for sequence in range(20):
        server._emit("message.delta", "sid-b", {"sequence": sequence})
    _wait_for(lambda: len(observer.frames) == 20)

    assert [frame["params"]["payload"]["sequence"] for frame in owner.frames] == list(range(20))
    assert [frame["params"]["payload"]["sequence"] for frame in observer.frames] == list(range(20))


@pytest.mark.asyncio
async def test_ws_observer_batch_awaits_physical_send_and_bypasses_token_buffer():
    sent: list[str] = []

    class FakeWS:
        async def send_text(self, line: str) -> None:
            sent.append(line)

    loop = asyncio.get_running_loop()
    transport = WSTransport(FakeWS(), loop, peer="observer-test")
    frame = server._event_frame("message.delta", "sid-b", {"text": "visible"})

    ok = await asyncio.to_thread(
        transport.write_observer_batch, [frame], timeout=0.5
    )

    assert ok is True
    assert len(sent) == 1
    assert transport._pending_tokens == []


@pytest.mark.asyncio
async def test_ws_observer_batch_revalidates_each_frame_inside_send_lock():
    sent: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    authorized = [True, True]

    class FakeWS:
        async def send_text(self, line: str) -> None:
            sent.append(line)
            if len(sent) == 1:
                first_started.set()
                await release_first.wait()

    loop = asyncio.get_running_loop()
    transport = WSTransport(FakeWS(), loop, peer="observer-revoke-test")
    frames = [
        server._event_frame("message.delta", "sid-b", {"sequence": sequence})
        for sequence in (1, 2)
    ]
    task = asyncio.create_task(
        asyncio.to_thread(
            transport.write_observer_batch,
            frames,
            timeout=0.5,
            validators=[lambda: authorized[0], lambda: authorized[1]],
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    authorized[1] = False
    release_first.set()

    assert await task is True
    assert len(sent) == 1

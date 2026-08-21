"""Contracts for output-subscription authorization and lifecycle state."""

from concurrent.futures import ThreadPoolExecutor
import threading
import types

import pytest

from tui_gateway import server
from tui_gateway import output_subscriptions as subscriptions
from tui_gateway.transport import Transport, bind_transport, reset_transport


OUTPUT_EVENTS = (
    "message.start",
    "message.delta",
    "message.interim",
    "message.complete",
    "tool.start",
    "tool.progress",
    "tool.complete",
    "status.update",
    "background.complete",
    "subagent.spawn_requested",
    "subagent.start",
    "subagent.progress",
    "subagent.complete",
    "error",
)

OWNER_ONLY_EVENTS = (
    "approval.request",
    "clarify.request",
    "clarify.expire",
    "sudo.request",
    "sudo.expire",
    "secret.request",
    "secret.expire",
    "terminal.read.request",
    "terminal.read.expire",
    "preview.read.request",
    "preview.read.expire",
    "window.read.request",
    "window.read.expire",
    "mcp.setup.request",
    "mcp.setup.expire",
    "billing.step_up.verification",
    "dashboard.new_session_requested",
    "notification.show",
    "notification.clear",
    "voice.status",
    "voice.transcript",
    "voice.interrupted",
    "wake.detected",
    "tool.output_risk",
    "some.future.event",
)


@pytest.mark.parametrize("event", OUTPUT_EVENTS)
def test_output_event_classifier_allows_only_the_phase_3b1_whitelist(event):
    classifier = getattr(subscriptions, "classify_session_event", None)

    assert callable(classifier), "Phase 3B-1 classifier is not implemented"
    assert classifier(event) == "output"


@pytest.mark.parametrize("event", OWNER_ONLY_EVENTS)
def test_output_event_classifier_fails_closed_for_control_and_unknown_events(event):
    classifier = getattr(subscriptions, "classify_session_event", None)

    assert callable(classifier), "Phase 3B-1 classifier is not implemented"
    assert classifier(event) == "owner_only"


def _subscription(
    *, anchor="sid-a", profile="/profiles/p1", target="sid-b", transport=None
):
    record_type = getattr(subscriptions, "OutputSubscription", None)

    assert record_type is not None, "Phase 3B-1 subscription record is not implemented"

    return record_type(
        profile_scope=profile,
        subscriber_session_id=anchor,
        subscriber_session_incarnation=object(),
        subscriber_transport=transport or object(),
        target_session_id=target,
        target_session_key=f"stored-{target}",
        target_session_incarnation=object(),
    )


def _incarnation_subscription(
    *,
    anchor_incarnation,
    target_incarnation,
    anchor="sid-a",
    profile="/profiles/p1",
    target="sid-b",
    transport=None,
):
    """Build the exact-incarnation contract required by replacement cleanup."""

    record_type = getattr(subscriptions, "OutputSubscription", None)
    assert record_type is not None
    return record_type(
        profile_scope=profile,
        subscriber_session_id=anchor,
        subscriber_session_incarnation=anchor_incarnation,
        subscriber_transport=transport or object(),
        target_session_id=target,
        target_session_key=f"stored-{target}",
        target_session_incarnation=target_incarnation,
    )


def _registry(limit=16):
    registry_type = getattr(subscriptions, "OutputSubscriptionRegistry", None)

    assert registry_type is not None, (
        "Phase 3B-1 subscription registry is not implemented"
    )

    return registry_type(max_subscriptions_per_transport=limit)


def _assert_consistent(registry, records):
    expected_by_session = {}
    expected_by_transport = {}

    for record in records:
        expected_by_session.setdefault(record.target_session_id, set()).add(
            record.subscriber_transport
        )
        expected_by_transport.setdefault(record.subscriber_transport, set()).add(
            record.target_session_id
        )

    for session_id, transports in expected_by_session.items():
        assert registry.subscribers_for_session(session_id) == frozenset(transports)
    for transport, session_ids in expected_by_transport.items():
        assert registry.session_ids_for_transport(transport) == frozenset(session_ids)


def test_registry_adds_one_bidirectional_subscription():
    registry = _registry()
    transport = object()
    record = _subscription(transport=transport)

    assert registry.subscribe(record) == "added"
    assert registry.subscription_for(transport, "sid-b") == record
    _assert_consistent(registry, [record])


def test_registry_repeated_subscribe_is_idempotent():
    registry = _registry()
    transport = object()
    record = _subscription(transport=transport)

    assert registry.subscribe(record) == "added"
    assert registry.subscribe(record) == "existing"
    assert registry.subscribers_for_session("sid-b") == frozenset({transport})
    assert registry.session_ids_for_transport(transport) == frozenset({"sid-b"})


def test_registry_enforces_per_transport_limit_without_losing_existing_entries():
    registry = _registry(limit=2)
    transport = object()
    first = _subscription(target="sid-b", transport=transport)
    second = _subscription(target="sid-c", transport=transport)
    rejected = _subscription(target="sid-d", transport=transport)

    assert registry.subscribe(first) == "added"
    assert registry.subscribe(second) == "added"
    assert registry.subscribe(rejected) == "limit"
    assert registry.session_ids_for_transport(transport) == frozenset({
        "sid-b",
        "sid-c",
    })
    assert registry.subscribers_for_session("sid-d") == frozenset()


def test_registry_unsubscribe_is_idempotent_and_preserves_other_targets():
    registry = _registry()
    transport = object()
    first = _subscription(target="sid-b", transport=transport)
    second = _subscription(target="sid-c", transport=transport)
    registry.subscribe(first)
    registry.subscribe(second)

    assert (
        registry.unsubscribe(transport, "sid-b", subscriber_session_id="sid-a") is True
    )
    assert (
        registry.unsubscribe(transport, "sid-b", subscriber_session_id="sid-a") is False
    )
    assert registry.subscribers_for_session("sid-b") == frozenset()
    _assert_consistent(registry, [second])


def test_registry_transport_cleanup_uses_reverse_index():
    registry = _registry()
    transport = object()
    other_transport = object()
    removed = [
        _subscription(target="sid-b", transport=transport),
        _subscription(target="sid-c", transport=transport),
    ]
    retained = _subscription(anchor="sid-z", target="sid-b", transport=other_transport)
    for record in [*removed, retained]:
        registry.subscribe(record)

    assert registry.remove_transport(transport) == 2
    assert registry.session_ids_for_transport(transport) == frozenset()
    _assert_consistent(registry, [retained])


def test_registry_session_cleanup_removes_every_reverse_reference():
    registry = _registry()
    first_transport = object()
    second_transport = object()
    first = _subscription(transport=first_transport)
    second = _subscription(anchor="sid-c", transport=second_transport)
    retained = _subscription(target="sid-d", transport=first_transport)
    for record in [first, second, retained]:
        registry.subscribe(record)

    assert registry.remove_session("sid-b") == 2
    assert registry.subscribers_for_session("sid-b") == frozenset()
    _assert_consistent(registry, [retained])


def test_registry_anchor_cleanup_removes_only_records_authorized_by_that_anchor():
    registry = _registry()
    transport = object()
    removed = _subscription(anchor="sid-a", target="sid-b", transport=transport)
    retained = _subscription(anchor="sid-c", target="sid-d", transport=transport)
    registry.subscribe(removed)
    registry.subscribe(retained)

    assert registry.remove_anchor(transport, "sid-a") == 1
    assert registry.subscribers_for_session("sid-b") == frozenset()
    _assert_consistent(registry, [retained])


def test_registry_target_cleanup_is_exact_when_runtime_and_durable_ids_are_reused():
    """Removing S1 must preserve S2 even when every public identity is reused."""

    registry = _registry()
    transport = object()
    anchor_incarnation = object()
    target_s1 = object()
    target_s2 = object()
    first = _incarnation_subscription(
        anchor_incarnation=anchor_incarnation,
        target_incarnation=target_s1,
        transport=transport,
    )
    replacement = _incarnation_subscription(
        anchor_incarnation=anchor_incarnation,
        target_incarnation=target_s2,
        transport=transport,
    )
    registry.subscribe(first)
    registry.subscribe(replacement)

    assert registry.remove_session(
        "sid-b", target_session_incarnation=target_s1
    ) == 1
    assert registry.subscription_for(
        transport,
        "sid-b",
        target_session_incarnation=target_s2,
    ) == replacement
    assert registry.subscribers_for_session(
        "sid-b", target_session_incarnation=target_s2
    ) == frozenset({transport})


def test_registry_anchor_cleanup_is_exact_when_sid_key_and_transport_are_reused():
    """Failed S1 cleanup must not revoke outgoing contracts authorized by S2."""

    registry = _registry()
    transport = object()
    target_incarnation = object()
    anchor_s1 = object()
    anchor_s2 = object()
    first = _incarnation_subscription(
        anchor_incarnation=anchor_s1,
        target_incarnation=target_incarnation,
        transport=transport,
    )
    replacement = _incarnation_subscription(
        anchor_incarnation=anchor_s2,
        target_incarnation=target_incarnation,
        transport=transport,
    )
    registry.subscribe(first)
    registry.subscribe(replacement)

    assert registry.remove_anchor(
        transport,
        "sid-a",
        subscriber_session_incarnation=anchor_s1,
    ) == 1
    assert registry.subscription_for(
        transport,
        "sid-b",
        subscriber_session_incarnation=anchor_s2,
    ) == replacement


def test_registry_concurrent_duplicate_subscribe_keeps_indexes_symmetric():
    registry = _registry()
    transport = object()
    record = _subscription(transport=transport)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: registry.subscribe(record), range(64)))

    assert outcomes.count("added") == 1
    assert outcomes.count("existing") == 63
    _assert_consistent(registry, [record])


def test_registry_interleaved_subscribe_unsubscribe_keeps_indexes_symmetric():
    registry = _registry()
    transport = object()
    record = _subscription(transport=transport)

    def toggle(index):
        if index % 2:
            return registry.unsubscribe(
                transport,
                "sid-b",
                subscriber_session_id="sid-a",
            )
        return registry.subscribe(record)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(toggle, range(64)))

    registry.subscribe(record)
    _assert_consistent(registry, [record])


class RecordingTransport(Transport):
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None


class CloseTrackingAgent:
    def __init__(self) -> None:
        self.model = "test"
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _install_session_side_machinery_spies(monkeypatch):
    from tools import approval

    poller_stop = threading.Event()
    registered = []
    unregistered = []
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda _sid, _session: poller_stop,
    )
    monkeypatch.setattr(
        approval,
        "register_gateway_notify",
        lambda session_key, _callback: registered.append(session_key),
    )
    monkeypatch.setattr(
        approval,
        "unregister_gateway_notify",
        lambda session_key: unregistered.append(session_key),
    )
    return poller_stop, registered, unregistered


def _live_session(
    transport,
    *,
    key,
    profile_home=None,
    source="tui",
    title="",
    model="test/model",
    **extra,
):
    return {
        "agent": types.SimpleNamespace(model=model),
        "attached_images": [],
        "cols": 80,
        "created_at": 10.0,
        "cwd": "/tmp",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "image_counter": 0,
        "last_active": 20.0,
        "pending_title": title,
        "profile_home": profile_home,
        "running": False,
        "session_key": key,
        "show_reasoning": False,
        "slash_worker": None,
        "source": source,
        "tool_progress_mode": "all",
        "transport": transport,
        "_output_incarnation": object(),
        **extra,
    }


def _rpc(transport, method, params):
    token = bind_transport(transport)
    try:
        return server.handle_request({
            "id": "phase-3b1",
            "method": method,
            "params": params,
        })
    finally:
        reset_transport(token)


@pytest.fixture()
def gateway_subscription_state(monkeypatch):
    registry = getattr(server, "_output_subscriptions", None)
    previous_sessions = dict(server._sessions)
    server._sessions.clear()

    class TitleDB:
        def get_session_title(self, key):
            return {"stored-b": "Session B"}.get(key, "")

    monkeypatch.setattr(server, "_get_db", lambda: TitleDB())
    try:
        yield registry
    finally:
        for session_id in list(server._sessions):
            popped = server._pop_session_by_id(session_id)
            server._teardown_popped_session(popped, end_reason="test_cleanup")
        server._sessions.update(previous_sessions)


def test_subscribe_rpc_registers_authorized_target_and_returns_sanitized_metadata(
    gateway_subscription_state,
):
    assert gateway_subscription_state is not None, (
        "Gateway subscription registry is not installed"
    )
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(
            owner_b,
            key="stored-b",
            model="gpt-5.6-sol",
            running=True,
            last_active=123.0,
        ),
    })

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    assert response["result"] == {
        "rejected": [],
        "subscriptions": [
            {
                "last_active": 123.0,
                "model": "gpt-5.6-sol",
                "session_id": "sid-b",
                "status": "working",
                "stored_session_id": "stored-b",
                "title": "Session B",
            }
        ],
    }
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset({
        owner_a
    })
    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset({
        "sid-b"
    })


def test_active_list_reports_requester_relative_owned_and_watchable(
    gateway_subscription_state, tmp_path
):
    owner = RecordingTransport()
    other = RecordingTransport()
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    server._sessions.update({
        "sid-a": _live_session(owner, key="stored-a", profile_home=profile_a),
        "sid-b": _live_session(other, key="stored-b", profile_home=profile_a),
        "sid-hidden": _live_session(
            other, key="stored-hidden", profile_home=profile_a, hidden=True
        ),
        "sid-other-profile": _live_session(
            other, key="stored-other", profile_home=profile_b
        ),
    })

    response = _rpc(
        owner,
        "session.active_list",
        {"current_session_id": "sid-a"},
    )
    rows = {row["id"]: row for row in response["result"]["sessions"]}

    assert (rows["sid-a"]["owned"], rows["sid-a"]["watchable"]) == (True, False)
    assert (rows["sid-b"]["owned"], rows["sid-b"]["watchable"]) == (False, True)
    assert rows["sid-hidden"]["watchable"] is False
    assert rows["sid-other-profile"]["watchable"] is False


def test_subscribe_rpc_is_idempotent_for_duplicate_targets_and_calls(
    gateway_subscription_state,
):
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(owner_b, key="stored-b"),
    })
    params = {
        "subscriber_session_id": "sid-a",
        "session_ids": ["sid-b", "sid-b"],
    }

    first = _rpc(owner_a, "session.output_subscribe", params)
    second = _rpc(owner_a, "session.output_subscribe", params)

    assert [row["session_id"] for row in first["result"]["subscriptions"]] == ["sid-b"]
    assert [row["session_id"] for row in second["result"]["subscriptions"]] == ["sid-b"]
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset({
        owner_a
    })


def test_subscribe_rpc_rejects_requester_that_does_not_own_anchor(
    gateway_subscription_state,
):
    owner_a = RecordingTransport()
    attacker = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(RecordingTransport(), key="stored-b"),
    })

    response = _rpc(
        attacker,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    assert response["error"]["data"] == {"reason": "not_owner"}
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset()


def test_subscribe_rpc_rejects_cross_profile_target(
    gateway_subscription_state, tmp_path
):
    owner_a = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a", profile_home=tmp_path / "p1"),
        "sid-b": _live_session(
            RecordingTransport(), key="stored-b", profile_home=tmp_path / "p2"
        ),
    })

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    assert response["result"] == {
        "subscriptions": [],
        "rejected": [{"session_id": "sid-b", "reason": "profile_mismatch"}],
    }
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset()


def test_subscribe_rpc_accepts_canonical_aliases_of_the_same_profile(
    gateway_subscription_state, tmp_path
):
    owner_a = RecordingTransport()
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    profile_alias = tmp_path / "profile-alias"
    profile_alias.symlink_to(profile_home, target_is_directory=True)
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a", profile_home=profile_home),
        "sid-b": _live_session(
            RecordingTransport(), key="stored-b", profile_home=profile_alias
        ),
    })

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    assert response["result"]["rejected"] == []
    assert [row["session_id"] for row in response["result"]["subscriptions"]] == [
        "sid-b"
    ]


def test_cross_profile_rejection_does_not_disclose_target_eligibility(
    gateway_subscription_state, tmp_path
):
    owner_a = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a", profile_home=tmp_path / "p1"),
        "sid-b": _live_session(
            RecordingTransport(),
            key="stored-b",
            profile_home=tmp_path / "p2",
            hidden=True,
            source="internal",
        ),
    })

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    assert response["result"]["rejected"] == [
        {"session_id": "sid-b", "reason": "profile_mismatch"}
    ]


@pytest.mark.parametrize(
    "target_patch",
    [
        {"source": "tool"},
        {"source": "tool-sidecar"},
        {"source": "kanban"},
        {"source": "internal"},
        {"pending_hidden": True},
        {"hidden": True},
        {"observer_eligible": False},
    ],
)
def test_subscribe_rpc_rejects_non_observer_eligible_target(
    gateway_subscription_state, target_patch
):
    owner_a = RecordingTransport()
    target = _live_session(RecordingTransport(), key="stored-b")
    target.update(target_patch)
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": target,
    })

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    assert response["result"]["rejected"] == [
        {"session_id": "sid-b", "reason": "not_observer_eligible"}
    ]
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset()


def test_set_hidden_revokes_incoming_subscriptions_and_unhide_allows_resubscribe(
    gateway_subscription_state, monkeypatch
):
    """The real hide RPC must fail closed in live authorization state."""

    class HiddenDB:
        def __init__(self):
            self.values = []

        def set_session_hidden(self, session_key, hidden):
            self.values.append((session_key, hidden))
            return True

        def get_session_title(self, _key):
            return ""

    db = HiddenDB()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(owner_b, key="stored-b"),
        "sid-c": _live_session(RecordingTransport(), key="stored-c"),
    })
    params = {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]}
    assert _rpc(owner_a, "session.output_subscribe", params)["result"][
        "rejected"
    ] == []
    assert _rpc(
        owner_b,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-b", "session_ids": ["sid-c"]},
    )["result"]["rejected"] == []

    hidden = _rpc(
        owner_b,
        "session.set_hidden",
        {"session_id": "sid-b", "hidden": True},
    )

    assert hidden["result"]["hidden"] is True
    assert server._sessions["sid-b"]["hidden"] is True
    assert gateway_subscription_state.subscribers_for_session(
        "sid-b",
        target_session_incarnation=server._sessions["sid-b"][
            "_output_incarnation"
        ],
    ) == frozenset()
    assert gateway_subscription_state.session_ids_for_transport(
        owner_b
    ) == frozenset({"sid-c"})
    assert gateway_subscription_state.subscribers_for_session("sid-c") == frozenset({
        owner_b
    })
    assert _rpc(owner_a, "session.output_subscribe", params)["result"][
        "rejected"
    ] == [{"session_id": "sid-b", "reason": "not_observer_eligible"}]

    visible = _rpc(
        owner_b,
        "session.set_hidden",
        {"session_id": "sid-b", "hidden": False},
    )

    assert visible["result"]["hidden"] is False
    assert server._sessions["sid-b"]["hidden"] is False
    assert _rpc(owner_a, "session.output_subscribe", params)["result"][
        "rejected"
    ] == []
    assert db.values == [("stored-b", True), ("stored-b", False)]


def test_set_hidden_db_failure_restores_live_flag_but_not_revoked_authorization(
    gateway_subscription_state, monkeypatch
):
    """A failed durable hide cannot resurrect the already-revoked watch grant."""

    class FailingHiddenDB:
        def set_session_hidden(self, _session_key, _hidden):
            raise RuntimeError("database is locked")

        def get_session_title(self, _key):
            return ""

    monkeypatch.setattr(server, "_get_db", lambda: FailingHiddenDB())
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    target = _live_session(owner_b, key="stored-b", hidden=False)
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": target,
    })
    _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    response = _rpc(
        owner_b,
        "session.set_hidden",
        {"session_id": "sid-b", "hidden": True},
    )

    assert response["error"]["code"] == 5007
    assert target["hidden"] is False
    assert gateway_subscription_state.subscribers_for_session(
        "sid-b", target_session_incarnation=target["_output_incarnation"]
    ) == frozenset()


def test_persisted_hidden_resume_is_not_observer_eligible_at_publication(
    gateway_subscription_state, monkeypatch, tmp_path
):
    """A hidden durable row must never become briefly watchable while resuming."""

    class HiddenResumeDB:
        def get_session(self, target):
            return {
                "id": target,
                "cwd": str(tmp_path),
                "hidden": 1,
                "created_at": 10.0,
            }

        def get_session_by_title(self, _target):
            return None

        def resolve_resume_session_id(self, target):
            return target

        def assert_resume_safe(self, _target):
            return None

        def reopen_session(self, _target):
            return None

        def get_resume_conversations(self, _target):
            return ([], [])

        def get_ancestor_display_prefix(self, _target):
            return []

        def get_session_title(self, _target):
            return "Hidden Session"

    db = HiddenResumeDB()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _row: {})
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(
        server, "_maybe_schedule_auto_continue", lambda _sid, _record, _target: None
    )
    observer = RecordingTransport()
    owner = RecordingTransport()
    server._sessions["sid-a"] = _live_session(observer, key="stored-a")

    resumed = _rpc(owner, "session.resume", {"session_id": "stored-hidden"})
    target_sid = resumed["result"]["session_id"]
    response = _rpc(
        observer,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": [target_sid]},
    )

    assert server._sessions[target_sid]["hidden"] is True
    assert response["result"] == {
        "subscriptions": [],
        "rejected": [
            {"session_id": target_sid, "reason": "not_observer_eligible"}
        ],
    }


def test_session_create_publishes_authoritative_hidden_and_incarnation(
    gateway_subscription_state, monkeypatch
):
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    owner = RecordingTransport()

    response = _rpc(owner, "session.create", {"hidden": True})
    sid = response["result"]["session_id"]
    session = server._sessions[sid]

    assert session["hidden"] is True
    assert session["pending_hidden"] is True
    assert session["_output_incarnation"] is not None


def test_init_session_publishes_hidden_and_incarnation_before_callback(
    gateway_subscription_state, monkeypatch
):
    from tools import approval

    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(
        server, "_start_notification_poller", lambda _sid, _session: threading.Event()
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *_args: None)
    monkeypatch.setattr(approval, "register_gateway_notify", lambda *_args: None)
    monkeypatch.setattr(approval, "load_permanent_allowlist", lambda: None)
    published = {}

    server._init_session(
        "sid-hidden",
        "stored-hidden",
        CloseTrackingAgent(),
        [],
        hidden=True,
        on_published=lambda session: published.update({
            "hidden": session.get("hidden"),
            "incarnation": session.get("_output_incarnation"),
        }),
    )

    assert published["hidden"] is True
    assert published["incarnation"] is server._sessions["sid-hidden"][
        "_output_incarnation"
    ]


def test_subscribe_rpc_rejects_missing_closing_and_finalized_targets(
    gateway_subscription_state,
):
    owner_a = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-closing": _live_session(
            RecordingTransport(), key="stored-closing", _closing=True
        ),
        "sid-finalized": _live_session(
            RecordingTransport(), key="stored-finalized", _finalized=True
        ),
    })

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {
            "subscriber_session_id": "sid-a",
            "session_ids": ["sid-missing", "sid-closing", "sid-finalized"],
        },
    )

    assert response["result"] == {
        "subscriptions": [],
        "rejected": [
            {"session_id": "sid-missing", "reason": "not_found"},
            {"session_id": "sid-closing", "reason": "closing"},
            {"session_id": "sid-finalized", "reason": "closing"},
        ],
    }


def test_subscribe_rpc_enforces_sixteen_target_limit(gateway_subscription_state):
    owner_a = RecordingTransport()
    server._sessions["sid-a"] = _live_session(owner_a, key="stored-a")
    targets = [f"sid-{index}" for index in range(17)]
    for target in targets:
        server._sessions[target] = _live_session(
            RecordingTransport(), key=f"stored-{target}"
        )

    response = _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": targets},
    )

    assert len(response["result"]["subscriptions"]) == 16
    assert response["result"]["rejected"] == [
        {"session_id": "sid-16", "reason": "subscription_limit"}
    ]
    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset(
        targets[:16]
    )


def test_unsubscribe_rpc_is_idempotent_and_does_not_change_target_owner(
    gateway_subscription_state,
):
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    owner_c = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(owner_b, key="stored-b"),
        "sid-c": _live_session(owner_c, key="stored-c"),
    })
    _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b", "sid-c"]},
    )

    first = _rpc(
        owner_a,
        "session.output_unsubscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    second = _rpc(
        owner_a,
        "session.output_unsubscribe",
        {
            "subscriber_session_id": "sid-a",
            "session_ids": ["sid-b", "sid-missing"],
        },
    )

    assert first["result"] == {"not_subscribed": [], "unsubscribed": ["sid-b"]}
    assert second["result"] == {
        "not_subscribed": ["sid-b", "sid-missing"],
        "unsubscribed": [],
    }
    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset({
        "sid-c"
    })
    assert server._sessions["sid-b"]["transport"] is owner_b


def test_unregister_transport_removes_its_output_subscriptions(
    gateway_subscription_state,
):
    owner_a = RecordingTransport()
    server._sessions["sid-a"] = _live_session(owner_a, key="stored-a")
    for target in ("sid-b", "sid-c", "sid-d"):
        server._sessions[target] = _live_session(
            RecordingTransport(), key=f"stored-{target}"
        )
    _rpc(
        owner_a,
        "session.output_subscribe",
        {
            "subscriber_session_id": "sid-a",
            "session_ids": ["sid-b", "sid-c", "sid-d"],
        },
    )

    server.unregister_live_transport(owner_a)

    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset()
    for target in ("sid-b", "sid-c", "sid-d"):
        assert owner_a not in gateway_subscription_state.subscribers_for_session(target)


def test_session_close_removes_target_from_every_subscriber(
    gateway_subscription_state, monkeypatch
):
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    owner_c = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(owner_b, key="stored-b"),
        "sid-c": _live_session(owner_c, key="stored-c"),
    })
    _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    _rpc(
        owner_c,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-c", "session_ids": ["sid-b"]},
    )
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda session, **_kwargs: session is not None,
    )

    response = _rpc(owner_b, "session.close", {"session_id": "sid-b"})

    assert response["result"] == {"closed": True}
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset()
    assert "sid-b" not in gateway_subscription_state.session_ids_for_transport(owner_a)
    assert "sid-b" not in gateway_subscription_state.session_ids_for_transport(owner_c)


def test_anchor_session_close_removes_its_outgoing_subscriptions(
    gateway_subscription_state, monkeypatch
):
    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(owner_b, key="stored-b"),
    })
    _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda session, **_kwargs: session is not None,
    )

    response = _rpc(owner_a, "session.close", {"session_id": "sid-a"})

    assert response["result"] == {"closed": True}
    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset()
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset()


def test_failed_resume_hydration_removes_target_subscriptions(
    gateway_subscription_state, monkeypatch
):
    observer = RecordingTransport()
    owner = RecordingTransport()
    target = _live_session(
        owner,
        key="stored-b",
        agent_ready=threading.Event(),
        resume_history_ready=threading.Event(),
        resume_hydrating=True,
    )
    server._sessions.update({
        "sid-a": _live_session(observer, key="stored-a"),
        "sid-b": target,
    })
    _rpc(
        observer,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    class ImmediateThread:
        def __init__(self, *, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    class FailingDB:
        def reopen_session(self, _stored_id):
            raise RuntimeError("hydrate failed")

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)

    server._schedule_resume_hydration("sid-b", "stored-b", FailingDB())

    assert "sid-b" not in server._sessions
    assert gateway_subscription_state.subscribers_for_session("sid-b") == frozenset()
    assert "sid-b" not in gateway_subscription_state.session_ids_for_transport(observer)


def test_eager_named_profile_resume_is_scoped_before_concurrent_subscribe(
    gateway_subscription_state, monkeypatch, tmp_path
):
    """A published eager resume must already carry its named-profile identity."""

    launch_owner = RecordingTransport()
    resume_owner = RecordingTransport()
    named_home = tmp_path / "named-profile"
    named_home.mkdir()
    published = threading.Event()
    release_resume = threading.Event()
    captured: dict = {}

    class ResumeDB:
        def get_session(self, target):
            return {"id": target, "cwd": str(tmp_path)}

        def get_session_by_title(self, _target):
            return None

        def resolve_resume_session_id(self, target):
            return target

        def assert_resume_safe(self, _target):
            return None

        def reopen_session(self, _target):
            return None

        def get_resume_conversations(self, _target):
            return ([], [])

        def get_ancestor_display_prefix(self, _target):
            return []

        def close(self):
            return None

    resume_db = ResumeDB()
    original_init_session = server._init_session

    def blocking_init_session(*args, **kwargs):
        original_init_session(*args, **kwargs)
        captured["sid"] = args[0]
        published.set()
        assert release_resume.wait(timeout=5.0)

    monkeypatch.setattr(
        server, "_profile_home", lambda profile: named_home if profile else None
    )
    monkeypatch.setattr("hermes_state.SessionDB", lambda **_kwargs: resume_db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: types.SimpleNamespace(model="test")
    )
    monkeypatch.setattr(server, "_init_session", blocking_init_session)
    monkeypatch.setattr(server, "_set_session_context", lambda _target: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _found: {})
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *_args: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {"model": "test"})
    server._sessions["sid-a"] = _live_session(launch_owner, key="stored-a")

    resume_result: dict = {}

    def resume_named_profile():
        resume_result["response"] = _rpc(
            resume_owner,
            "session.resume",
            {"session_id": "stored-b", "profile": "named", "eager_build": True},
        )

    resume_thread = threading.Thread(target=resume_named_profile)
    resume_thread.start()
    try:
        assert published.wait(timeout=5.0), resume_result
        target_sid = captured["sid"]
        subscribe_response = _rpc(
            launch_owner,
            "session.output_subscribe",
            {"subscriber_session_id": "sid-a", "session_ids": [target_sid]},
        )
    finally:
        release_resume.set()
        resume_thread.join(timeout=5.0)

    assert not resume_thread.is_alive()
    assert "result" in resume_result["response"]
    assert subscribe_response["result"] == {
        "subscriptions": [],
        "rejected": [{"session_id": target_sid, "reason": "profile_mismatch"}],
    }
    assert gateway_subscription_state.subscribers_for_session(target_sid) == frozenset()
    assert target_sid not in gateway_subscription_state.session_ids_for_transport(
        launch_owner
    )


def test_eager_resume_failure_cleans_concurrent_subscription_indexes(
    gateway_subscription_state, monkeypatch, tmp_path
):
    """A synchronous resume failure must remove both subscription indexes."""

    observer = RecordingTransport()
    resume_owner = RecordingTransport()
    named_home = tmp_path / "named-profile"
    named_home.mkdir()
    published = threading.Event()
    release_resume = threading.Event()
    captured: dict = {}
    poller_stop, registered, unregistered = _install_session_side_machinery_spies(
        monkeypatch
    )

    class ResumeDB:
        def get_session(self, target):
            return {"id": target, "cwd": str(tmp_path)}

        def get_session_by_title(self, _target):
            return None

        def resolve_resume_session_id(self, target):
            return target

        def assert_resume_safe(self, _target):
            return None

        def reopen_session(self, _target):
            return None

        def get_resume_conversations(self, _target):
            return ([], [])

        def get_ancestor_display_prefix(self, _target):
            return []

        def get_session_title(self, _target):
            return "Session B"

        def close(self):
            return None

    resume_db = ResumeDB()
    original_init_session = server._init_session

    def failing_init_session(*args, **kwargs):
        original_init_session(*args, **kwargs)
        captured["sid"] = args[0]
        captured["session"] = server._sessions[args[0]]
        published.set()
        assert release_resume.wait(timeout=5.0)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        server, "_profile_home", lambda profile: named_home if profile else None
    )
    monkeypatch.setattr("hermes_state.SessionDB", lambda **_kwargs: resume_db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    agent = CloseTrackingAgent()
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: agent)
    monkeypatch.setattr(server, "_init_session", failing_init_session)
    monkeypatch.setattr(server, "_set_session_context", lambda _target: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _found: {})
    server._sessions["sid-a"] = _live_session(
        observer,
        key="stored-a",
        profile_home=named_home,
    )

    resume_result: dict = {}

    def resume_named_profile():
        resume_result["response"] = _rpc(
            resume_owner,
            "session.resume",
            {"session_id": "stored-b", "profile": "named", "eager_build": True},
        )

    resume_thread = threading.Thread(target=resume_named_profile)
    resume_thread.start()
    target_sid = ""
    try:
        assert published.wait(timeout=5.0), resume_result
        target_sid = captured["sid"]
        subscribe_response = _rpc(
            observer,
            "session.output_subscribe",
            {"subscriber_session_id": "sid-a", "session_ids": [target_sid]},
        )
        assert [
            row["session_id"] for row in subscribe_response["result"]["subscriptions"]
        ] == [target_sid]
    finally:
        release_resume.set()
        resume_thread.join(timeout=5.0)

    try:
        assert not resume_thread.is_alive()
        assert resume_result["response"]["error"]["code"] == 5000
        assert target_sid not in server._sessions
        assert (
            gateway_subscription_state.subscribers_for_session(target_sid)
            == frozenset()
        )
        assert target_sid not in gateway_subscription_state.session_ids_for_transport(
            observer
        )
        assert captured["session"].get("_finalized") is True
        assert poller_stop.is_set()
        assert registered == ["stored-b"]
        assert unregistered == ["stored-b"]
        assert agent.close_calls == 1
    finally:
        server._teardown_session(
            captured.get("session"), end_reason="test_cleanup"
        )
        gateway_subscription_state.remove_session(target_sid)
        gateway_subscription_state.remove_transport(observer)


def test_default_profile_eager_resume_failure_cleans_concurrent_subscriptions(
    gateway_subscription_state, monkeypatch, tmp_path
):
    """A failed launch-profile resume cleans subscriptions without owning its DB."""

    observer = RecordingTransport()
    resume_owner = RecordingTransport()
    published = threading.Event()
    release_resume = threading.Event()
    captured: dict = {}
    poller_stop, registered, unregistered = _install_session_side_machinery_spies(
        monkeypatch
    )

    class SharedResumeDB:
        def __init__(self):
            self.close_calls = 0

        def get_session(self, target):
            return {"id": target, "cwd": str(tmp_path)}

        def get_session_by_title(self, _target):
            return None

        def resolve_resume_session_id(self, target):
            return target

        def assert_resume_safe(self, _target):
            return None

        def reopen_session(self, _target):
            return None

        def get_resume_conversations(self, _target):
            return ([], [])

        def get_ancestor_display_prefix(self, _target):
            return []

        def get_session_title(self, _target):
            return "Session B"

        def close(self):
            self.close_calls += 1

    shared_db = SharedResumeDB()
    original_init_session = server._init_session

    def failing_init_session(*args, **kwargs):
        original_init_session(*args, **kwargs)
        captured["sid"] = args[0]
        captured["session"] = server._sessions[args[0]]
        published.set()
        assert release_resume.wait(timeout=5.0)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_get_db", lambda: shared_db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    agent = CloseTrackingAgent()
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: agent)
    monkeypatch.setattr(server, "_init_session", failing_init_session)
    monkeypatch.setattr(server, "_set_session_context", lambda _target: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _found: {})
    server._sessions["sid-a"] = _live_session(observer, key="stored-a")

    resume_result: dict = {}

    def resume_default_profile():
        resume_result["response"] = _rpc(
            resume_owner,
            "session.resume",
            {"session_id": "stored-b", "eager_build": True},
        )

    resume_thread = threading.Thread(target=resume_default_profile)
    resume_thread.start()
    target_sid = ""
    try:
        assert published.wait(timeout=5.0), resume_result
        target_sid = captured["sid"]
        assert server._sessions[target_sid]["profile_home"] is None
        subscribe_response = _rpc(
            observer,
            "session.output_subscribe",
            {"subscriber_session_id": "sid-a", "session_ids": [target_sid]},
        )
        assert [
            row["session_id"] for row in subscribe_response["result"]["subscriptions"]
        ] == [target_sid]
    finally:
        release_resume.set()
        resume_thread.join(timeout=5.0)

    try:
        assert not resume_thread.is_alive()
        assert resume_result["response"]["error"]["code"] == 5000
        assert shared_db.close_calls == 0
        assert target_sid not in server._sessions
        assert (
            gateway_subscription_state.subscribers_for_session(target_sid)
            == frozenset()
        )
        assert target_sid not in gateway_subscription_state.session_ids_for_transport(
            observer
        )
        assert gateway_subscription_state.subscription_for(observer, target_sid) is None
        assert captured["session"].get("_finalized") is True
        assert poller_stop.is_set()
        assert registered == ["stored-b"]
        assert unregistered == ["stored-b"]
        assert agent.close_calls == 1
    finally:
        server._teardown_session(
            captured.get("session"), end_reason="test_cleanup"
        )
        gateway_subscription_state.remove_session(target_sid)
        gateway_subscription_state.remove_transport(observer)


def test_failed_resume_cleans_exact_attempt_without_deleting_same_sid_replacement(
    gateway_subscription_state, monkeypatch, tmp_path
):
    """Failure cleanup tears down S1 while preserving replacement S2 by identity."""

    observer_s1 = RecordingTransport()
    observer_s2 = RecordingTransport()
    resume_owner = RecordingTransport()
    published = threading.Event()
    release_resume = threading.Event()
    captured: dict = {}
    poller_stop, _registered, unregistered = _install_session_side_machinery_spies(
        monkeypatch
    )

    class SharedResumeDB:
        def get_session(self, target):
            return {"id": target, "cwd": str(tmp_path)}

        def get_session_by_title(self, _target):
            return None

        def resolve_resume_session_id(self, target):
            return target

        def assert_resume_safe(self, _target):
            return None

        def reopen_session(self, _target):
            return None

        def get_resume_conversations(self, _target):
            return ([], [])

        def get_ancestor_display_prefix(self, _target):
            return []

        def get_session_title(self, target):
            return "Replacement" if target == "stored-replacement" else "Session B"

    shared_db = SharedResumeDB()
    original_init_session = server._init_session
    failed_agent = CloseTrackingAgent()

    def failing_init_session(*args, **kwargs):
        original_init_session(*args, **kwargs)
        captured["sid"] = args[0]
        captured["session"] = server._sessions[args[0]]
        published.set()
        assert release_resume.wait(timeout=5.0)
        raise RuntimeError("late initialization failure")

    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_get_db", lambda: shared_db)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: failed_agent)
    monkeypatch.setattr(server, "_init_session", failing_init_session)
    monkeypatch.setattr(server, "_set_session_context", lambda _target: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _found: {})
    server._sessions.update({
        "sid-a": _live_session(observer_s1, key="stored-a"),
        "sid-c": _live_session(observer_s2, key="stored-c"),
        "sid-z": _live_session(RecordingTransport(), key="stored-z"),
    })

    resume_result: dict = {}

    def resume_default_profile():
        resume_result["response"] = _rpc(
            resume_owner,
            "session.resume",
            {"session_id": "stored-b", "eager_build": True},
        )

    resume_thread = threading.Thread(target=resume_default_profile)
    resume_thread.start()
    target_sid = ""
    replacement = None
    try:
        assert published.wait(timeout=5.0), resume_result
        target_sid = captured["sid"]
        failed_session = captured["session"]
        first_subscribe = _rpc(
            observer_s1,
            "session.output_subscribe",
            {"subscriber_session_id": "sid-a", "session_ids": [target_sid]},
        )
        assert [
            row["session_id"] for row in first_subscribe["result"]["subscriptions"]
        ] == [target_sid]
        failed_outgoing = _rpc(
            resume_owner,
            "session.output_subscribe",
            {
                "subscriber_session_id": target_sid,
                "session_ids": ["sid-z"],
            },
        )
        assert failed_outgoing["result"]["rejected"] == []

        replacement = _live_session(
            resume_owner,
            key="stored-b",
        )
        with server._sessions_lock:
            assert server._sessions[target_sid] is failed_session
            server._sessions[target_sid] = replacement

        second_subscribe = _rpc(
            observer_s2,
            "session.output_subscribe",
            {"subscriber_session_id": "sid-c", "session_ids": [target_sid]},
        )
        assert [
            row["session_id"] for row in second_subscribe["result"]["subscriptions"]
        ] == [target_sid]
        replacement_outgoing = _rpc(
            resume_owner,
            "session.output_subscribe",
            {
                "subscriber_session_id": target_sid,
                "session_ids": ["sid-z"],
            },
        )
        assert replacement_outgoing["result"]["rejected"] == []
    finally:
        release_resume.set()
        resume_thread.join(timeout=5.0)

    try:
        assert not resume_thread.is_alive()
        assert resume_result["response"]["error"]["code"] == 5000
        assert replacement is not None
        assert server._sessions[target_sid] is replacement
        assert target_sid not in gateway_subscription_state.session_ids_for_transport(
            observer_s1
        )
        assert gateway_subscription_state.session_ids_for_transport(
            observer_s2
        ) == frozenset({target_sid})
        assert gateway_subscription_state.subscribers_for_session(
            target_sid,
            target_session_incarnation=replacement["_output_incarnation"],
        ) == frozenset({observer_s2})
        assert gateway_subscription_state.subscription_for(
            resume_owner,
            "sid-z",
            subscriber_session_incarnation=replacement["_output_incarnation"],
        ) is not None
        assert gateway_subscription_state.subscription_for(
            resume_owner,
            "sid-z",
            subscriber_session_incarnation=captured["session"][
                "_output_incarnation"
            ],
        ) is None
        assert captured["session"].get("_finalized") is True
        assert poller_stop.is_set()
        assert unregistered == ["stored-b"]
        assert failed_agent.close_calls == 1
        assert replacement.get("_finalized") is not True
    finally:
        server._teardown_session(
            captured.get("session"), end_reason="test_cleanup"
        )
        gateway_subscription_state.remove_transport(observer_s1)
        gateway_subscription_state.remove_transport(observer_s2)


def test_owner_transfer_clears_subscriptions_authorized_by_old_anchor(
    gateway_subscription_state,
):
    owner_a = RecordingTransport()
    next_owner = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a"),
        "sid-b": _live_session(RecordingTransport(), key="stored-b"),
    })
    _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    response = _rpc(next_owner, "session.activate", {"session_id": "sid-a"})

    assert response["result"]["session_id"] == "sid-a"
    assert server._sessions["sid-a"]["transport"] is next_owner
    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset()


def test_prompt_submit_owner_rebind_clears_old_anchor_subscriptions(
    gateway_subscription_state, monkeypatch
):
    owner_a = RecordingTransport()
    next_owner = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(owner_a, key="stored-a", running=True),
        "sid-b": _live_session(RecordingTransport(), key="stored-b"),
    })
    _rpc(
        owner_a,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(
        server,
        "_handle_busy_submit",
        lambda rid, *_args, **_kwargs: server._ok(rid, {"status": "queued"}),
    )

    response = _rpc(
        next_owner,
        "prompt.submit",
        {"session_id": "sid-a", "text": "take ownership"},
    )

    assert response["result"] == {"status": "queued"}
    assert server._sessions["sid-a"]["transport"] is next_owner
    assert gateway_subscription_state.session_ids_for_transport(owner_a) == frozenset()


def test_queued_prompt_owner_rebind_clears_old_anchor_subscriptions(
    gateway_subscription_state, monkeypatch
):
    queued_owner = RecordingTransport()
    current_owner = RecordingTransport()
    session = _live_session(
        current_owner,
        key="stored-a",
        queued_prompt={"text": "queued handoff", "transport": queued_owner},
    )
    server._sessions.update({
        "sid-a": session,
        "sid-b": _live_session(RecordingTransport(), key="stored-b"),
    })
    _rpc(
        current_owner,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_args, **_kwargs: None)

    assert server._drain_queued_prompt("phase-3b1", "sid-a", session) is True

    assert server._sessions["sid-a"]["transport"] is queued_owner
    assert (
        gateway_subscription_state.session_ids_for_transport(current_owner)
        == frozenset()
    )


@pytest.mark.parametrize("event", ["secret.request", "approval.request"])
def test_subscribe_contract_keeps_control_event_routing_owner_only(
    gateway_subscription_state, event
):
    observer = RecordingTransport()
    owner = RecordingTransport()
    server._sessions.update({
        "sid-a": _live_session(observer, key="stored-a"),
        "sid-b": _live_session(owner, key="stored-b"),
    })
    _rpc(
        observer,
        "session.output_subscribe",
        {"subscriber_session_id": "sid-a", "session_ids": ["sid-b"]},
    )

    server._emit(event, "sid-b", {"text": "owner only"})

    assert [frame["params"]["type"] for frame in owner.frames] == [event]
    assert observer.frames == []

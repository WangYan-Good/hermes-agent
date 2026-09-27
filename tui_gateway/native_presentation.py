"""Opt-in Dashboard Native ownership; no prompt or conversation transfer.

The opaque generation is a reconnect capability, never a stored session ID.
Only the owning transport can prepare/release. Receipts survive a lost ACK.
"""
from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager

from .attachments import owner_principal, store


class NativeOwnershipError(PermissionError):
    pass


class Authority:
    def __init__(self, generation, transport, profile):
        self.generation = generation
        self.transport = transport
        self.principal = owner_principal(transport)
        self.profile = profile
        self.lock = threading.RLock()
        self.accepted = False
        self.epoch = 0
        self.active = 0
        self.frozen = False
        self.releasing = False
        self.released = False
        self.ticket = None
        self.ticket_session = None
        self.session = None
        self.sid = None
        self.stored_id = None
        self.completed = 0.0
        self.retirement_pending = False


_registry = {}
_registry_lock = threading.Lock()
_START = {'session.create', 'session.resume', 'session.activate'}
# Reads and explicit resolution of already-pending work remain available.
_SETTLE = {'session.info', 'session.history', 'session.history_page',
           'approval.received', 'approval.respond', 'clarify.respond',
           'secret.respond', 'sudo.respond', 'mcp.setup.respond', 'reload.mcp', 'session.interrupt', 'approval.pending',
           'attachment.cancel', 'attachment.snapshot', 'attachment.connection'}


def attach(transport, generation, profile):
    if not isinstance(generation, str) or not 32 <= len(generation) <= 128:
        raise NativeOwnershipError('Invalid Native generation')
    with _registry_lock:
        for key, old in list(_registry.items()):
            if old.completed and time.monotonic() - old.completed > 900:
                del _registry[key]
        state = _registry.get(generation)
        if state is None:
            if len(_registry) >= 2048:
                raise NativeOwnershipError('Too many Native presentations')
            state = Authority(generation, transport, profile)
            _registry[generation] = state
        elif state.principal != owner_principal(transport) or state.profile != profile:
            raise NativeOwnershipError('Native scope mismatch')
        elif state.transport is not transport and not getattr(state.transport, '_closed', False):
            raise NativeOwnershipError('Native presentation already connected')
        state.transport = transport
        transport.native_presentation = state
    return state


def _retire_if_unreferenced(server, state):
    # Called with the authority lock, only after teardown or a lifecycle RPC.
    # Do not infer release from disconnect or from a session merely being popped.
    if state.active or state.releasing:
        return
    with server._sessions_lock:
        if any(s.get('native_presentation') is state for s in server._sessions.values()):
            return
    state.completed = time.monotonic()
    state.retirement_pending = False
    state.session = state.ticket_session = None
    state.ticket = None


def session_finalized(server, session):
    """Retain a bounded reconnect grace after the normal server teardown finishes.

    Retirement does not manufacture a release receipt. A transient disconnect
    alone never reaches this hook, and another live session keeps its authority.
    """
    state = session.get('native_presentation')
    if state is None:
        return
    with state.lock:
        state.retirement_pending = True
        _retire_if_unreferenced(server, state)


def check_rebind(session, transport):
    state = session.get('native_presentation')
    incoming = getattr(transport, 'native_presentation', None)
    if state is not None and (incoming is not state or state.released or state.releasing):
        raise NativeOwnershipError('Active session belongs to another presentation')
    if incoming is not None:
        if incoming.released or incoming.releasing:
            raise NativeOwnershipError('Native presentation released')
        if state is None:
            old = session.get('transport')
            if old is not transport and not getattr(old, '_closed', False):
                raise NativeOwnershipError('Session already has an input owner')
            session['native_presentation'] = incoming


@contextmanager
def admit(server, method, params):
    transport = server.current_transport()
    state = getattr(transport, 'native_presentation', None)
    generation = params.get('presentation_generation')
    if generation and method in _START:
        state = attach(transport, generation, params.get('profile') or '')
    if state is None:
        yield
        return
    with state.lock:
        if state.transport is not transport or state.released or state.releasing:
            raise NativeOwnershipError('Native presentation released or rebound')
        if params.get('profile', state.profile) != state.profile:
            raise NativeOwnershipError('Native profile mismatch')
        if state.frozen and method not in _SETTLE and method not in {'session.resume', 'session.activate'}:
            raise NativeOwnershipError('Native input frozen for switching')
        if method in _START and state.frozen and state.stored_id and params.get('session_id') not in {state.stored_id, state.sid}:
            raise NativeOwnershipError('Cannot change prepared session')
        if method in _START:
            state.completed = 0.0
        if method == 'prompt.submit':
            state.accepted = True
        state.active += 1
    try:
        yield
    finally:
        with state.lock:
            state.active -= 1
            if state.retirement_pending or method in _START:
                _retire_if_unreferenced(server, state)


def blocked(server, sid, session):
    reasons = []
    if session.get('running') or session.get('inflight_turn'):
        reasons.append('turn')
    if server._queued_prompt_snapshot(session):
        reasons.append('queue')
    if server._session_pending_kind(sid):
        reasons.append('interaction')
    ready = session.get('agent_ready')
    if session.get('resume_hydrating') or session.get('agent_error') or (ready is not None and not ready.is_set()):
        reasons.append('initializing')
    if server._session_has_active_delegations(sid, session):
        reasons.append('delegation')
    if session.get('attached_images'):
        reasons.append('attachments')
    with store.lock:
        if any(d.runtime_id == sid and any(a.state not in {'submitted', 'cancelled'} for a in d.items.values()) for d in store.drafts.values()):
            reasons.append('attachments')
    return reasons


def ensure_durable(server, session):
    server._ensure_session_db_row(session)
    stored = server._session_lookup_key(session)
    from hermes_state import SessionDB
    from pathlib import Path
    with SessionDB(Path(session.get('profile_home') or server._hermes_home) / 'state.db') as db:
        if not stored or db.get_session(stored) is None:
            raise NativeOwnershipError('Durable session unavailable')
    return stored


def handle(server, rid, params):
    transport = server.current_transport()
    generation = params.get('generation')
    state = getattr(transport, 'native_presentation', None)
    # Reconnect may query a receipt before trying to resume the released source.
    if state is None and generation:
        state = attach(transport, generation, params.get('profile') or '')
    if state is None or state.generation != generation or state.transport is not transport:
        raise NativeOwnershipError('Native owner required')
    action = params.get('action')
    if action not in {'status', 'prepare', 'cancel', 'release'}:
        return server._err(rid, 4000, 'Invalid Native lifecycle action')
    with state.lock:
        if state.released:
            return server._ok(rid, {'confirmed': True, 'released': True, 'stored_id': state.stored_id})
        if state.releasing:
            return server._ok(rid, {'confirmed': True, 'ready': False, 'blocked': ['releasing']})
        sid = params.get('session_id')
        with server._sessions_lock:
            session = server._sessions.get(sid)
            if session is None and params.get('cleanup'):
                records = [(key, value) for key, value in server._sessions.items() if value.get('native_presentation') is state]
                if len(records) == 1:
                    sid, session = records[0]
                elif not records and not state.active and not state.accepted:
                    state.released = True
                    state.completed = time.monotonic()
                    return server._ok(rid, {'confirmed': True, 'released': True})
            if session is None or session.get('transport') is not transport or session.get('native_presentation') is not state:
                raise NativeOwnershipError('Native session owner required')
        state.session, state.sid = session, sid
        state.stored_id = server._session_lookup_key(session)
        if action == 'cancel':
            state.epoch += 1
            state.frozen = False
            state.ticket = None
            state.ticket_session = None
            return server._ok(rid, {'cancelled': True})
        if action in {'prepare', 'release'}:
            state.epoch += 1
            state.frozen = True
        epoch = state.epoch
        reasons = blocked(server, sid, session)
        if params.get('cleanup') and not state.accepted:
            reasons = [r for r in reasons if r != 'initializing']
        if state.active:
            reasons.append('rpc-in-flight')
        result = {'confirmed': True, 'ready': not reasons, 'blocked': reasons, 'stored_id': state.stored_id}
        if action == 'status' or reasons:
            return server._ok(rid, result)
        if action == 'release':
            if not state.ticket or params.get('ticket') != state.ticket or state.ticket_session is not session:
                raise NativeOwnershipError('Authoritative prepare required')
            state.releasing = True
    if action == 'prepare':
        # Persistence may block. Admission remains frozen, no global lock held.
        stored = ensure_durable(server, session)
        with state.lock:
            if state.epoch != epoch or state.transport is not transport or state.releasing or state.released or not state.frozen:
                raise NativeOwnershipError('Native prepare invalidated')
            with server._sessions_lock:
                if server._sessions.get(sid) is not session:
                    raise NativeOwnershipError('Native session changed')
            reasons = blocked(server, sid, session)
            if params.get('cleanup') and not state.accepted:
                reasons = [r for r in reasons if r != 'initializing']
            if state.active:
                reasons.append('rpc-in-flight')
            if reasons:
                return server._ok(rid, {**result, 'ready': False, 'blocked': reasons})
            state.ticket = state.ticket or secrets.token_urlsafe(32)
            state.stored_id = stored
            state.ticket_session = session
            return server._ok(rid, {**result, 'stored_id': stored, 'ticket': state.ticket})
    with server._session_resume_lock:
        with server._sessions_lock:
            popped = server._pop_session_by_id(sid, expected_session=session)
    if popped is None:
        raise NativeOwnershipError('Native cleanup unconfirmed')
    server._teardown_popped_session(popped)
    with state.lock:
        state.released = True
        state.completed = time.monotonic()
    return server._ok(rid, {'confirmed': True, 'released': True, 'stored_id': state.stored_id})

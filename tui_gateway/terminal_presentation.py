"""Terminal-only ownership adapter. No Native handoff RPC or durable schema.

A capability validated by the Dashboard binds a real gateway transport to one
PTY instance. Caller-provided session IDs alone never confer authority.
"""
from __future__ import annotations

import secrets
import threading
from contextlib import contextmanager


class TerminalOwnershipError(PermissionError):
    """Only Terminal ownership failures map to the Terminal RPC error code."""


class Authority:
    def __init__(self, pty):
        self.pty = pty
        self.lock = threading.RLock()
        self.active = 0
        self.frozen = False
        self.ticket = None
        self.ticket_scope = None
        self.released = False
        self.transport = None
        self.access = None


def owner_of(transport):
    return getattr(transport, 'terminal_owner', None)


def check_rebind(session, transport):
    old = session.get('terminal_owner')
    new = owner_of(transport)
    if (old is not None or new is not None) and old is not new:
        raise TerminalOwnershipError('Active session belongs to another presentation')
    if old is not None and (old.closing or old.gateway.released):
        raise TerminalOwnershipError('Terminal presentation is closing')


@contextmanager
def admit(server, method, params):
    transport = server.current_transport()
    owner = owner_of(transport)
    if owner is None:
        yield params
        return
    with server._sessions_lock:
        session = server._sessions.get(params.get('session_id'))
        if session is not None and method != 'session.resume':
            check_rebind(session, transport)
    state = owner.gateway
    if state is None:
        raise TerminalOwnershipError('Terminal authority unavailable')
    state.access = server
    # Terminal launch context is fixed for this transport. It cannot select a
    # second profile by sending different JSON-RPC parameters.
    params = dict(params)
    if params.get('profile') not in (None, '', owner.profile):
        raise TerminalOwnershipError('Terminal profile mismatch')
    params['profile'] = owner.profile
    if method == 'session.resume':
        params['allow_auto_continue'] = False
    with state.lock:
        if state.released or owner.closing or state.frozen:
            raise TerminalOwnershipError('Terminal presentation input is frozen')
        state.active += 1
    try:
        yield params
    finally:
        with state.lock:
            state.active -= 1


def _snapshot(server, owner):
    with server._sessions_lock:
        records = [(sid, s) for sid, s in server._sessions.items() if s.get('terminal_owner') is owner]
    blocked = []
    for sid, session in records:
        if session.get('running') or session.get('inflight_turn'):
            blocked.append('turn')
        if server._queued_prompt_snapshot(session):
            blocked.append('queue')
        if server._session_pending_kind(sid):
            blocked.append('interaction')
        if session.get('resume_hydrating') or session.get('agent_error'):
            blocked.append('initializing')
        ready = session.get('agent_ready')
        if ready is not None and not ready.is_set():
            blocked.append('initializing')
        if server._session_has_active_delegations(sid, session):
            blocked.append('delegation')
        if session.get('attached_images'):
            blocked.append('attachments')
    if not records:
        blocked.append('no-session')
    return records, sorted(set(blocked))


def handle(server, rid, params):
    owner = owner_of(server.current_transport())
    if owner is None or owner.gateway is None:
        return server._err(rid, 4030, 'Terminal owner required')
    state = owner.gateway
    state.access = server
    action = params.get('action')
    if action not in {'status', 'prepare', 'cancel', 'release'}:
        return server._err(rid, 4000, 'Invalid Terminal lifecycle action')
    popped = []
    with state.lock:
        if state.released or owner.closing:
            return server._err(rid, 4090, 'Terminal owner released')
        if action == 'cancel':
            state.frozen = False
            state.ticket = None
            return server._ok(rid, {'cancelled': True})
        if action in {'prepare', 'release'}:
            state.frozen = True
        records, blocked = _snapshot(server, owner)
        if state.active:
            blocked.append('rpc-in-flight')
        result = {'confirmed': True, 'ready': not blocked, 'blocked': blocked,
                  'accepted_input': owner.accepted_input,
                  'stored_id': server._session_lookup_key(records[0][1]) if len(records) == 1 else None}
        if action == 'prepare' and not blocked:
            scope = (owner.owner, owner.generation)
            if state.ticket_scope != scope:
                state.ticket = None
            state.ticket_scope = scope
            state.ticket = state.ticket or secrets.token_urlsafe(32)
            result['ticket'] = state.ticket
        if action == 'release':
            if blocked or not state.ticket or params.get('ticket') != state.ticket or state.ticket_scope != (owner.owner, owner.generation):
                return server._err(rid, 4090, 'Authoritative prepare required')
            # Claim under the established lock order, finalize OUTSIDE all locks.
            with server._session_resume_lock:
                with server._sessions_lock:
                    for sid, session in records:
                        popped.append(server._pop_session_by_id(sid, expected_session=session))
            state.released = True
    if action == 'release':
        for session in popped:
            server._teardown_popped_session(session)
        result['released'] = True
    return server._ok(rid, result)


def close_owner(owner):
    state = owner.gateway
    if state is None:
        return
    with state.lock:
        state.frozen = True
        state.released = True
    server = state.access
    if server is None:
        return  # No gateway RPC has created a session for this target.
    with server._session_resume_lock:
        with server._sessions_lock:
            records = [(sid, s) for sid, s in server._sessions.items() if s.get('terminal_owner') is owner]
            popped = [server._pop_session_by_id(sid, expected_session=s) for sid, s in records]
    for session in popped:
        server._teardown_popped_session(session)

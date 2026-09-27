"""Exercise the real gateway admission and release paths without a model call."""
import threading
import uuid
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway import native_presentation as native


@pytest.fixture
def owner(monkeypatch, tmp_path):
    transport = SimpleNamespace(_closed=False)
    monkeypatch.setattr(server, 'current_transport', lambda: transport)
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_hermes_home', tmp_path)
    monkeypatch.setattr(server, '_queued_prompt_snapshot', lambda s: s.get('queued'))
    monkeypatch.setattr(server, '_session_pending_kind', lambda sid: server._sessions[sid].get('pending'))
    monkeypatch.setattr(server, '_session_has_active_delegations', lambda sid, s: False)
    monkeypatch.setattr(server, '_teardown_popped_session', lambda s, **kw: True)
    native._registry.clear()
    state = native.attach(transport, uuid.uuid4().hex, '')
    ready = threading.Event()
    ready.set()
    session = server._prepare_output_session_record({'transport': transport, 'session_key': 'durable', 'agent_ready': ready})
    server._sessions['runtime'] = session
    def persist(s):
        from hermes_state import SessionDB
        with SessionDB(tmp_path / 'state.db') as db:
            db.create_session(s['session_key'], source='webui')
    monkeypatch.setattr(server, '_ensure_session_db_row', persist)
    return transport, state, session


def call(owner, action, **extra):
    return server.handle_request({'id': 1, 'method': 'native.presentation', 'params': {
        'session_id': 'runtime', 'generation': owner[1].generation, 'action': action, **extra,
    }})


def test_empty_prepare_persists_without_prompt_and_release_receipt(owner):
    result = call(owner, 'prepare')['result']
    assert result['ready'] and result['stored_id'] == 'durable'
    assert call(owner, 'release', ticket=result['ticket'])['result']['released']
    assert not server._sessions
    assert call(owner, 'status')['result']['released']
    assert call(owner, 'release', ticket=result['ticket'])['result']['released']
    with pytest.raises(native.NativeOwnershipError):
        with native.admit(server, 'prompt.submit', {}):
            pytest.fail('released source admitted prompt')


@pytest.mark.parametrize('reason,field,value', [('turn', 'running', True), ('queue', 'queued', ['text']), ('interaction', 'pending', 'approval'), ('initializing', 'resume_hydrating', True), ('attachments', 'attached_images', ['image'])])
def test_busy_defer_and_cancel(owner, reason, field, value):
    owner[2][field] = value
    assert reason in call(owner, 'prepare')['result']['blocked']
    with pytest.raises(native.NativeOwnershipError):
        with native.admit(server, 'prompt.submit', {}):
            pytest.fail('frozen input admitted')
    with native.admit(server, 'approval.respond', {}):
        pass
    assert call(owner, 'cancel')['result']['cancelled']
    with native.admit(server, 'prompt.submit', {}):
        pass


def test_ticket_and_transport_required(owner, monkeypatch):
    assert 'error' in call(owner, 'release', ticket='fake')
    impostor = SimpleNamespace(_closed=False)
    monkeypatch.setattr(server, 'current_transport', lambda: impostor)
    assert 'error' in call(owner, 'prepare')
    with pytest.raises(native.NativeOwnershipError):
        native.check_rebind(owner[2], impostor)


def test_reconnect_can_query_release_receipt_but_cannot_reopen(owner, monkeypatch):
    ticket = call(owner, 'prepare')['result']['ticket']
    call(owner, 'release', ticket=ticket)
    owner[0]._closed = True
    other = SimpleNamespace(_closed=False)
    monkeypatch.setattr(server, 'current_transport', lambda: other)
    assert call(owner, 'status')['result']['released']
    with pytest.raises(native.NativeOwnershipError):
        with native.admit(server, 'session.resume', {'presentation_generation': owner[1].generation}):
            pytest.fail('old generation reopened')


def test_prepare_does_not_wait_for_persistence_under_global_locks(owner, monkeypatch):
    original = server._ensure_session_db_row
    def persist(session):
        assert server._session_resume_lock.acquire(blocking=False)
        server._session_resume_lock.release()
        acquired = []
        def probe():
            with server._sessions_lock:
                acquired.append(True)
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(timeout=2)
        assert acquired
        original(session)
    monkeypatch.setattr(server, '_ensure_session_db_row', persist)
    assert call(owner, 'prepare')['result']['ready']


def test_inflight_request_blocks_prepare_and_generation_is_required(owner):
    with native.admit(server, 'prompt.submit', {'session_id': 'runtime'}):
        assert 'rpc-in-flight' in call(owner, 'prepare')['result']['blocked']
    assert 'error' in call(owner, 'prepare', generation='stale')


def test_attachment_ledger_blocks_even_if_browser_reports_empty(owner, tmp_path):
    from tui_gateway.attachments import store
    draft = store.create('runtime', str(tmp_path), tmp_path, owner[0])
    try:
        item = store.prepare(draft, occurrence='one', request_id='request', name='file.txt', size=1, mime='text/plain')
        assert 'attachments' in call(owner, 'prepare')['result']['blocked']
        store.cancel(draft, item.id)
        assert call(owner, 'prepare')['result']['ready']
    finally:
        store.drafts.pop(draft.id, None)


def test_stale_profile_generation_cannot_claim(owner, monkeypatch):
    owner[0]._closed = True
    other = SimpleNamespace(_closed=False)
    with pytest.raises(native.NativeOwnershipError):
        native.attach(other, owner[1].generation, 'other-profile')
    assert server._sessions['runtime'] is owner[2]


def test_cleanup_failure_never_produces_release_receipt(owner, monkeypatch):
    ticket = call(owner, 'prepare')['result']['ticket']
    def fail(*args, **kwargs):
        raise RuntimeError('cleanup failed')
    monkeypatch.setattr(server, '_teardown_popped_session', fail)
    with pytest.raises(RuntimeError):
        call(owner, 'release', ticket=ticket)
    assert call(owner, 'status')['result'].get('released') is not True
    assert owner[1].releasing


def test_other_transport_cannot_close_native_session(owner, monkeypatch):
    monkeypatch.setattr(server, 'current_transport', lambda: SimpleNamespace(_closed=False))
    result = server.handle_request({'id': 1, 'method': 'session.close', 'params': {'session_id': 'runtime'}})
    assert 'error' in result
    assert server._sessions['runtime'] is owner[2]


def test_ticket_is_bound_to_the_prepared_session_incarnation(owner):
    ticket = call(owner, 'prepare')['result']['ticket']
    replacement = dict(owner[2])
    server._sessions['runtime'] = replacement
    assert 'error' in call(owner, 'release', ticket=ticket)
    assert server._sessions['runtime'] is replacement


def test_busy_orphan_finalization_retires_authority_after_receipt_grace(owner, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(native.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(server, '_finalize_session', lambda s, **kw: s.update(_finalized=True))
    monkeypatch.setattr(server, '_announce_session_reclaimed', lambda *a: None)
    for _ in range(6):
        transport = SimpleNamespace(_closed=False)
        state = native.attach(transport, uuid.uuid4().hex, '')
        monkeypatch.setattr(server, 'current_transport', lambda: transport)
        session = server._prepare_output_session_record({'transport': transport, 'session_key': 'durable', 'running': True})
        server._sessions['runtime'] = session
        assert 'turn' in call((transport, state, session), 'prepare')['result']['blocked']
        transport._closed = True
        # A disconnect itself cannot retire a still-running authority.
        assert not state.completed and native.attach(SimpleNamespace(_closed=False), state.generation, '') is state
        state.transport._closed = True
        session['running'] = False
        server._sessions.pop('runtime')
        server._teardown_session(session, end_reason='ws_orphan_reap')
        assert state.completed and not state.released  # Retirement is not a release receipt.
        assert state.generation in native._registry
        clock[0] += 901
        # Normal subsequent admission prunes only authorities past their grace.
        native.attach(owner[0], owner[1].generation, '')
        assert state.generation not in native._registry
        assert len(native._registry) <= 2


def test_finalizing_old_session_does_not_retire_another_live_session(owner, monkeypatch):
    monkeypatch.setattr(server, '_finalize_session', lambda s, **kw: s.update(_finalized=True))
    monkeypatch.setattr(server, '_announce_session_reclaimed', lambda *a: None)
    other = dict(owner[2]); server._sessions['other'] = other
    server._sessions.pop('runtime')
    server._teardown_session(owner[2])
    assert not owner[1].completed and not owner[1].released
    native.check_rebind(other, owner[0])

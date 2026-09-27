"""Terminal lifecycle uses the existing session owner, persistence and locks."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.pty_session import PtySession
from tui_gateway import server
from tui_gateway.terminal_presentation import Authority, admit, check_rebind, handle


@pytest.fixture
def owned(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    owner = PtySession('tab', 'alice', '')
    owner.gateway = Authority(owner)
    transport = SimpleNamespace(terminal_owner=owner)
    ready = threading.Event()
    ready.set()
    session = {'terminal_owner': owner, 'transport': transport, 'agent_ready': ready,
               'running': False, 'session_key': 'durable', 'history': [],
               'history_lock': threading.Lock()}
    monkeypatch.setattr(server, '_sessions', {'runtime': session})
    monkeypatch.setattr(server, 'current_transport', lambda: transport)
    monkeypatch.setattr(server, '_session_has_active_delegations', lambda *a: False)
    monkeypatch.setattr(server, '_session_pending_kind', lambda *a: '')
    monkeypatch.setattr(server, '_teardown_popped_session', Mock())
    return owner, transport, session


def call(action, **params):
    return handle(server, 'id', {'action': action, 'session_id': 'runtime', **params})


def test_actual_owner_prepare_cancel_release_and_lock_order(owned, monkeypatch):
    owner, transport, session = owned
    assert call('status')['result']['ready']
    ticket = call('prepare')['result']['ticket']
    with pytest.raises(PermissionError):
        with admit(server, 'prompt.submit', {'session_id': 'runtime'}):
            pytest.fail('new input admitted after prepare')
    assert call('cancel')['result']['cancelled']
    with admit(server, 'session.resume', {'session_id': 'durable', 'allow_auto_continue': True}) as params:
        assert params['allow_auto_continue'] is False
    assert 'error' in call('release', ticket=ticket)
    ticket = call('prepare')['result']['ticket']
    def finalize(record):
        # Another thread must be able to take the global locks during slow I/O.
        acquired = threading.Event()
        def probe():
            with server._session_resume_lock, server._sessions_lock:
                acquired.set()
        thread = threading.Thread(target=probe)
        thread.start()
        assert acquired.wait(2)
        thread.join()
        assert record is session
    monkeypatch.setattr(server, '_teardown_popped_session', finalize)
    assert call('release', ticket=ticket)['result']['released']
    assert not server._sessions
    assert owner.gateway.released


@pytest.mark.parametrize('field,value', [
    ('running', True), ('inflight_turn', {'user': 'pending'}),
    ('queued_prompt', {'text': 'queued'}), ('resume_hydrating', True),
    ('agent_error', 'failed'), ('attached_images', ['attachment']),
])
def test_real_session_work_blocks_prepare(owned, field, value):
    owner, transport, session = owned
    session[field] = value
    result = call('prepare')['result']
    assert not result['ready'] and result['blocked']
    assert 'ticket' not in result
    assert 'error' in call('release', ticket='invented')
    assert server._sessions['runtime'] is session


@pytest.mark.parametrize('kind', ['approval', 'clarify', 'secret', 'sudo', 'mcp'])
def test_pending_interaction_blocks_prepare(owned, monkeypatch, kind):
    monkeypatch.setattr(server, '_session_pending_kind', lambda *a: kind)
    assert not call('prepare')['result']['ready']


def test_inflight_admission_cannot_race_prepare(owned):
    owner, _, _ = owned
    with admit(server, 'prompt.submit', {'session_id': 'runtime'}):
        result = call('prepare')['result']
        assert not result['ready']
        assert 'rpc-in-flight' in result['blocked']
    assert owner.gateway.frozen
    assert call('cancel')['result']['cancelled']


def test_native_cannot_acquire_terminal_or_call_terminal_lifecycle(owned, monkeypatch):
    owner, _, session = owned
    native = SimpleNamespace()
    with pytest.raises(PermissionError):
        check_rebind(session, native)
    with pytest.raises(PermissionError):
        check_rebind({'transport': native}, SimpleNamespace(terminal_owner=owner))
    monkeypatch.setattr(server, 'current_transport', lambda: native)
    assert 'error' in call('status')
    # Ordinary transports bypass the Terminal wrapper; the real session input
    # handler must still reject a foreign Terminal target.
    rejected = server.handle_request({'id': 1, 'method': 'prompt.submit', 'params': {'session_id': 'runtime', 'text': 'intruder'}})
    assert rejected['error']['code'] == 4030
    assert server.handle_request({'id': 1, 'method': 'session.handoff', 'params': {}})['error']['code'] == -32601


def test_profile_cannot_be_changed_by_terminal_rpc(owned):
    with pytest.raises(PermissionError):
        with admit(server, 'session.create', {'profile': 'other'}):
            pytest.fail('cross-profile create')


@pytest.mark.parametrize('changed', ['owner', 'generation'])
def test_prepare_ticket_cannot_cross_owner_incarnation_or_viewer(owned, changed):
    owner, _, _ = owned
    ticket = call('prepare')['result']['ticket']
    setattr(owner, changed, object())
    assert call('release', ticket=ticket)['error']['code'] == 4090
    assert 'runtime' in server._sessions


def test_abort_cleanup_of_real_owner_does_not_reinitialize_closed_authority(owned):
    from tui_gateway.terminal_presentation import close_owner
    owner, transport, session = owned
    owner.gateway.access = server
    owner.closing = True
    close_owner(owner)
    assert not server._sessions
    assert owner.gateway.released

"""Review regressions for optional Terminal integration with the current gateway."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.pty_session import PtySession
from tui_gateway import server, ws as ws_mod
from tui_gateway.terminal_presentation import Authority, admit


def test_non_terminal_admission_never_reads_sessions_or_locks(monkeypatch):
    with monkeypatch.context() as scoped:
        _non_terminal_admission(scoped)


def _non_terminal_admission(monkeypatch):
    class Forbidden:
        def __getattribute__(self, name):
            raise AssertionError(f'Terminal layer accessed non-Terminal state: {name}')
        def __enter__(self):
            raise AssertionError('Terminal layer acquired a non-Terminal session lock')
    monkeypatch.setattr(server, 'current_transport', lambda: SimpleNamespace())
    monkeypatch.setattr(server, '_sessions', Forbidden())
    monkeypatch.setattr(server, '_sessions_lock', Forbidden())
    params = {'session_id': 'opaque', 'profile': 'unchanged'}
    with admit(server, 'session.delete', params) as scoped:
        assert scoped is params


@pytest.mark.parametrize('registry', ['missing', 'replaced'])
@pytest.mark.parametrize('terminal', [False, True])
def test_callable_server_does_not_require_module_registry(monkeypatch, registry, terminal):
    with monkeypatch.context() as scoped:
        _callable_server(scoped, registry, terminal)


def _callable_server(monkeypatch, registry, terminal):
    owner = PtySession('tab', 'alice', '')
    owner.gateway = Authority(owner)
    transport = SimpleNamespace(terminal_owner=owner) if terminal else SimpleNamespace()
    monkeypatch.setattr(server, 'current_transport', lambda: transport)
    monkeypatch.setattr(server, '_sessions', {})
    # A retained module object and its functions remain authoritative even if
    # test isolation unloads/replaces its module-registry entry.
    if registry == 'missing':
        monkeypatch.delitem(sys.modules, server.__name__, raising=False)
    else:
        monkeypatch.setitem(sys.modules, server.__name__, SimpleNamespace())
    if terminal:
        # Replace dynamic state AFTER registry removal, rather than freezing it
        # in the presentation adapter at import/initialization time.
        monkeypatch.setattr(server, '_sessions', {'live': {'terminal_owner': owner, 'session_key': 'durable'}})
        monkeypatch.setattr(server, '_session_pending_kind', lambda sid: '')
        monkeypatch.setattr(server, '_session_has_active_delegations', lambda *args: False)
        reply = server.handle_request({'id': 'id', 'method': 'terminal.presentation', 'params': {'action': 'status'}})
        assert reply['result']['ready']
        assert reply['result']['stored_id'] == 'durable'
    else:
        params = {'profile': 'original'}
        monkeypatch.setitem(server._methods, 'test.echo', lambda rid, supplied: server._ok(rid, supplied))
        assert server.handle_request({'id': 'id', 'method': 'test.echo', 'params': params})['result'] is params


@pytest.mark.parametrize('terminal', [False, True])
def test_ws_metadata_is_optional_and_valid_owner_still_binds(monkeypatch, terminal):
    owner = PtySession('tab', 'alice', '')
    owner.gateway = Authority(owner)
    seen = []
    monkeypatch.setattr(server, 'resolve_skin', lambda: {})
    monkeypatch.setattr(server, '_WS_ORPHAN_REAP_GRACE_S', 0)
    class Socket:
        async def accept(self):
            pass
        async def send_text(self, text):
            seen.append(text)
        async def receive_text(self):
            if terminal:
                assert owner.gateway.transport.terminal_owner is owner
            raise ws_mod._WebSocketDisconnect()
        async def close(self, **kwargs):
            pass
    socket = Socket()
    if terminal:
        socket.scope = {'terminal_owner': owner}
    else:
        assert not hasattr(socket, 'scope')
    asyncio.run(ws_mod.handle_ws(socket))
    assert any('gateway.ready' in frame for frame in seen)
    assert not server._live_transports


def test_non_terminal_handler_keeps_its_original_exception(monkeypatch):
    monkeypatch.setattr(server, 'current_transport', lambda: SimpleNamespace())
    original = PermissionError('ordinary handler failure')
    def handler(rid, params):
        raise original
    monkeypatch.setitem(server._methods, 'test.denied', handler)
    with pytest.raises(PermissionError) as raised:
        server.handle_request({'id': 'id', 'method': 'test.denied'})
    assert raised.value is original

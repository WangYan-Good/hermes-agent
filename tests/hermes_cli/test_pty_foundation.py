"""PTY boundary/race contracts. Real PTY coverage lives in the E2E harness."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli.pty_control import PROTOCOL, control
from hermes_cli.pty_session import PtyConflict, PtySession, PtySessionRegistry


class Socket:
    def __init__(self):
        self.frames = []
        self.closed = False

    async def send_json(self, frame):
        self.frames.append(frame)

    async def send_bytes(self, data):
        self.frames.append(data)

    async def close(self, **_kwargs):
        self.closed = True


class Bridge:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.reading = threading.Event()

    def read(self, timeout):
        self.reading.wait(timeout)
        return None if self.closed else b''

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True
        self.reading.set()


def frame(session, action, **extra):
    return {'id': 'request', 'action': action, 'generation': session.generation,
            'instance': session.instance, **extra}


@pytest.mark.asyncio
async def test_attach_reconnect_reap_and_input_are_single_owner():
    now = [0]
    reg = PtySessionRegistry(clock=lambda: now[0], ttl=5)
    bridge = Bridge()
    session = await reg.acquire('tab', 'alice', 'a', lambda _: bridge)
    first, second = Socket(), Socket()
    session.owner = Socket()
    await session.attach(first, 'g1')
    await session.input(first, 'g1', b'{"action":"release"}')
    assert bridge.writes == [b'{"action":"release"}']
    assert not session.closing
    with pytest.raises(PtyConflict):
        await session.attach(second, 'g2', session.instance)
    with pytest.raises(PtyConflict):
        await session.input(second, 'g2', b'intruder')
    session.detach(first, 0)
    same = await reg.acquire('tab', 'alice', 'a', lambda _: pytest.fail('double spawn'), instance=session.instance)
    assert same is session
    await same.attach(second, 'g2', session.instance)
    session.detach(first, 0)  # late old-socket finally must not detach the new owner
    now[0] = 10
    await reg.reap()
    assert reg.sessions['tab'] is session
    session.detach(second, 10)
    now[0] = 15
    await reg.reap()
    assert not reg.sessions and bridge.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('principal,profile', [('mallory', 'a'), ('alice', 'b')])
async def test_cross_scope_attachment_rejected(principal, profile):
    reg = PtySessionRegistry()
    session = await reg.acquire('tab', 'alice', 'a', lambda _: Bridge())
    try:
        with pytest.raises(PtyConflict):
            await reg.acquire('tab', principal, profile, lambda _: pytest.fail('spawn'), instance=session.instance)
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_spawn_reservation_prevents_double_process_and_cleans_late_startup():
    reg = PtySessionRegistry()
    entered, finish = threading.Event(), threading.Event()
    bridge = Bridge()

    def spawn(_):
        entered.set()
        assert finish.wait(5)
        return bridge

    first = asyncio.create_task(reg.acquire('tab', 'a', '', spawn))
    await asyncio.to_thread(entered.wait, 5)
    with pytest.raises(PtyConflict):
        await reg.acquire('tab', 'a', '', lambda _: pytest.fail('double process'))
    first.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert bridge.closed and not reg.sessions


@pytest.mark.asyncio
async def test_shutdown_racing_startup_and_reconnect():
    reg = PtySessionRegistry()
    entered, finish = threading.Event(), threading.Event()
    bridge = Bridge()
    def spawn(_):
        entered.set()
        assert finish.wait(5)
        return bridge
    start = asyncio.create_task(reg.acquire('tab', 'a', '', spawn))
    await asyncio.to_thread(entered.wait, 5)
    stop = asyncio.create_task(reg.shutdown())
    await asyncio.sleep(0)
    with pytest.raises(PtyConflict):
        await reg.acquire('tab2', 'a', '', spawn)
    finish.set()
    with pytest.raises(PtyConflict):
        await start
    await stop
    assert bridge.closed and not reg.sessions


@pytest.mark.asyncio
async def test_failed_startup_and_full_registry_do_not_leave_resources():
    reg = PtySessionRegistry(max_sessions=1)
    def fail(_):
        raise OSError('spawn failed')
    with pytest.raises(OSError):
        await reg.acquire('failed', 'a', '', fail)
    assert not reg.sessions
    first = await reg.acquire('tab', 'a', '', lambda _: Bridge())
    with pytest.raises(PtyConflict):
        await reg.acquire('next', 'a', '', lambda _: pytest.fail('capacity bypass'))
    assert not first.closing
    await reg.shutdown()


@pytest.mark.asyncio
async def test_prepare_freezes_before_owner_response_and_cancel_requires_ack(monkeypatch):
    from hermes_cli import pty_control
    session = PtySession('key', 'a', '', bridge=Bridge())
    ws = Socket()
    session.owner = Socket()
    await session.attach(ws, 'generation')
    entered, respond = asyncio.Event(), asyncio.Event()
    async def owner(_session, action, **_kwargs):
        entered.set()
        await respond.wait()
        return {'ready': True, 'ticket': 'authority'} if action == 'prepare' else {'cancelled': True}
    monkeypatch.setattr(pty_control, 'request_owner', owner)
    prepare = asyncio.create_task(control(session, PtySessionRegistry(), ws, frame(session, 'prepare')))
    await entered.wait()
    with pytest.raises(PtyConflict):
        await session.input(ws, 'generation', b'new turn')
    respond.set()
    await prepare
    assert session.frozen
    await control(session, PtySessionRegistry(), ws, frame(session, 'cancel'))
    await session.input(ws, 'generation', b'allowed')
    assert session.bridge.writes == [b'allowed']


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [None, {}, {'released': False}])
async def test_release_without_ack_cannot_cleanup(monkeypatch, answer):
    from hermes_cli import pty_control
    async def owner(*args, **kwargs):
        return answer
    monkeypatch.setattr(pty_control, 'request_owner', owner)
    reg = PtySessionRegistry()
    session = await reg.acquire('tab', 'a', '', lambda _: Bridge())
    ws = Socket()
    await session.attach(ws, 'generation')
    try:
        with pytest.raises(PtyConflict):
            await control(session, reg, ws, frame(session, 'release', ticket='ticket'))
        assert reg.sessions['tab'] is session and not session.bridge.closed
        assert session.frozen
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_abort_vs_first_input_and_release_ack_order(monkeypatch):
    from hermes_cli import pty_control
    reg = PtySessionRegistry()
    session = await reg.acquire('tab', 'a', '', lambda _: Bridge())
    ws = Socket()
    session.owner = Socket()
    await session.attach(ws, 'generation')
    await session.input(ws, 'generation', b'x')
    with pytest.raises(PtyConflict):
        await control(session, reg, ws, frame(session, 'abort'))
    assert not session.bridge.closed
    async def owner(*args, **kwargs):
        return {'released': True}
    monkeypatch.setattr(pty_control, 'request_owner', owner)
    await control(session, reg, ws, frame(session, 'release', ticket='ticket'))
    assert session.bridge.closed and not reg.sessions
    assert ws.frames[-1]['result']['released']


@pytest.mark.asyncio
async def test_abort_claim_prevents_late_input():
    reg = PtySessionRegistry()
    session = await reg.acquire('tab', 'a', '', lambda _: Bridge())
    ws = Socket()
    session.owner = Socket()
    await session.attach(ws, 'generation')
    await control(session, reg, ws, frame(session, 'abort'))
    with pytest.raises(PtyConflict):
        await session.input(ws, 'generation', b'x')
    assert session.bridge.closed and not reg.sessions


@pytest.fixture
def dashboard(monkeypatch):
    from hermes_cli import web_server as web
    from hermes_cli import dashboard_tui_env
    monkeypatch.setattr(web.app.state, 'bound_host', '127.0.0.1', raising=False)
    monkeypatch.setattr(web.app.state, 'auth_required', False, raising=False)
    monkeypatch.setattr(web.app.state, 'pty_registry', PtySessionRegistry(), raising=False)
    spawn = Mock(side_effect=AssertionError('Rejected handshake spawned'))
    monkeypatch.setattr(dashboard_tui_env, 'launch', spawn)
    client = TestClient(web.app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234))
    return web, client, spawn


@pytest.mark.parametrize('invalid', ['missing-auth', 'bad-auth', 'host', 'origin', 'profile', 'protocol', 'no-protocol', 'owner', 'peer'])
def test_invalid_handshake_never_spawns(dashboard, invalid, monkeypatch):
    web, client, spawn = dashboard
    params = f'token={web._SESSION_TOKEN}&attach=abcdefghijklmnop&generation=abcdefghijklmnop'
    headers = {'host': '127.0.0.1'}
    protocols = [PROTOCOL]
    if invalid == 'missing-auth': params = params.replace(f'token={web._SESSION_TOKEN}', 'unused=1')
    if invalid == 'bad-auth': params = params.replace(web._SESSION_TOKEN, 'bad')
    if invalid == 'host': headers['host'] = 'evil.example'
    if invalid == 'origin': headers['origin'] = 'https://evil.example'
    if invalid == 'profile': params += '&profile=../escape'
    if invalid == 'protocol': protocols = ['hermes.pty-control.v0']
    if invalid == 'no-protocol': protocols = []
    if invalid == 'owner': params += '&role=owner&instance=bad&capability=bad'
    if invalid == 'peer': client = TestClient(web.app, base_url='http://127.0.0.1', client=('192.0.2.1', 1234))
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect('/api/pty?' + params, headers=headers, subprotocols=protocols):
            pytest.fail('Rejected handshake accepted')
    expected = 4401 if invalid in {'missing-auth', 'bad-auth'} else (4400 if invalid in {'profile', 'protocol', 'no-protocol'} else 4403)
    assert rejected.value.code == expected
    spawn.assert_not_called()
    assert not web.app.state.pty_registry.sessions


def test_replayed_and_expired_tickets_never_spawn(dashboard, monkeypatch):
    from hermes_cli.dashboard_auth import ws_tickets
    web, client, spawn = dashboard
    monkeypatch.setattr(web.app.state, 'auth_required', True)
    ticket = ws_tickets.mint_ticket(user_id='alice', provider='test')
    ws_tickets.consume_ticket(ticket)
    expired = ws_tickets.mint_ticket(user_id='alice', provider='test')
    expiry, identity = ws_tickets._tickets[expired]
    ws_tickets._tickets[expired] = (0, identity)
    for value in (ticket, expired, 'invalid'):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f'/api/pty?ticket={value}', subprotocols=[PROTOCOL]):
                pytest.fail('Invalid ticket accepted')
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_release_disconnect_late_ack_cannot_clean_up_new_viewer(monkeypatch):
    from hermes_cli import pty_control
    reg = PtySessionRegistry(ttl=0)
    session = await reg.acquire('tab', 'a', '', lambda _: Bridge())
    old, new = Socket(), Socket()
    await session.attach(old, 'old')
    entered, ack = asyncio.Event(), asyncio.Event()
    async def owner(*args, **kwargs):
        entered.set()
        await ack.wait()
        return {'released': True}
    monkeypatch.setattr(pty_control, 'request_owner', owner)
    releasing = asyncio.create_task(control(session, reg, old, frame(session, 'release')))
    await entered.wait()
    session.detach(old, reg.clock())
    await session.attach(new, 'new', session.instance)
    ack.set()
    with pytest.raises(PtyConflict):
        await releasing
    assert session.viewer is new and not session.bridge.closed and session.frozen
    session.detach(new, reg.clock())
    await reg.reap()
    assert not reg.sessions and session.bridge.closed


@pytest.mark.asyncio
async def test_attach_cannot_resurrect_reaper_claim():
    reg = PtySessionRegistry(ttl=0)
    session = await reg.acquire('tab', 'a', '', lambda _: Bridge())
    old = Socket()
    await session.attach(old, 'old')
    session.detach(old, reg.clock())
    reaping = asyncio.create_task(reg.reap())
    # Wait for the claim, not a wall-clock idle guess.
    while not session.closing:
        await asyncio.sleep(0)
    with pytest.raises(PtyConflict):
        await session.attach(Socket(), 'new', session.instance)
    await reaping
    assert not reg.sessions


@pytest.mark.asyncio
async def test_cancel_is_not_undone_by_stale_ready_notification(monkeypatch):
    from hermes_cli.pty_control import owner_connection, request_owner
    session = PtySession('key', 'a', '', bridge=Bridge())
    viewer = Socket()
    await session.attach(viewer, 'new-generation')
    class Owner(Socket):
        def __init__(self):
            super().__init__()
            self.queue = asyncio.Queue()
        async def accept(self, **kwargs):
            pass
        async def receive_json(self):
            return await self.queue.get()
    from tui_gateway.terminal_presentation import Authority
    session.gateway = Authority(session)
    session.gateway.ticket = 'old-owner-ticket'
    owner = Owner()
    connection = asyncio.create_task(owner_connection(session, owner))
    await asyncio.sleep(0)
    pending = asyncio.create_task(request_owner(session, 'cancel'))
    await asyncio.sleep(0)
    request = owner.frames[-1]
    await owner.queue.put({'id': request['id'], 'instance': session.instance,
                           'generation': 'old-generation', 'result': {'ready': True}})
    await asyncio.sleep(0)
    assert not pending.done()
    await owner.queue.put({'id': request['id'], 'instance': session.instance,
                           'generation': 'new-generation', 'result': {'cancelled': True}})
    assert await pending == {'cancelled': True}
    connection.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connection
    assert session.gateway.ticket is None
    assert session.gateway.frozen


@pytest.mark.parametrize('gated', [False, True])
def test_authenticated_handshake_reaches_spawn_and_abort_cleans_registry(dashboard, monkeypatch, gated):
    from hermes_cli.dashboard_auth.ws_tickets import mint_ticket
    web, client, spawn = dashboard
    bridge = Bridge()
    spawn.side_effect = lambda *a: bridge
    auth = f'token={web._SESSION_TOKEN}'
    if gated:
        monkeypatch.setattr(web.app.state, 'auth_required', True)
        auth = 'ticket=' + mint_ticket(user_id='alice', provider='test')
    query = auth + '&attach=abcdefghijklmnop&generation=abcdefghijklmnop'
    with client.websocket_connect('/api/pty?' + query, headers={'host': '127.0.0.1'}, subprotocols=[PROTOCOL]) as ws:
        attached = ws.receive_json()
        assert attached['type'] == 'attached'
        ws.send_json({'type': 'control', 'id': 'abort', 'action': 'abort',
                      'instance': attached['instance'], 'generation': 'abcdefghijklmnop'})
        assert ws.receive_json()['result']['released']
    spawn.assert_called_once()
    assert bridge.closed and not web.app.state.pty_registry.sessions


@pytest.mark.asyncio
async def test_acknowledged_pre_input_disconnect_retains_same_process(monkeypatch):
    from hermes_cli import web_server as web, dashboard_tui_env
    from hermes_cli.pty_transport import endpoint
    reg = PtySessionRegistry()
    bridge = Bridge()
    spawn = Mock(return_value=bridge)
    monkeypatch.setattr(web.app.state, 'pty_registry', reg, raising=False)
    monkeypatch.setattr(web, '_ws_auth_ok', lambda ws: True)
    monkeypatch.setattr(web, '_ws_request_is_allowed', lambda ws: True)
    monkeypatch.setattr(dashboard_tui_env, 'launch', spawn)
    class Viewer(Socket):
        app = web.app
        headers = {'sec-websocket-protocol': PROTOCOL}
        scope = {}
        def __init__(self, instance=None, abort=False):
            super().__init__()
            self.query_params = {'attach': 'abcdefghijklmnop', 'generation': 'newgeneration1234'}
            if instance:
                self.query_params['instance'] = instance
            self.abort = abort
        async def accept(self, **kwargs):
            pass
        async def receive(self):
            if self.abort:
                self.abort = False
                session = reg.sessions['abcdefghijklmnop']
                return {'type': 'websocket.receive', 'text': __import__('json').dumps({'type': 'control', **frame(session, 'abort')})}
            return {'type': 'websocket.disconnect', 'code': 1006}
    first = Viewer()
    try:
        await endpoint(first)
        attached = first.frames[0]
        assert attached['type'] == 'attached'
        session = reg.sessions['abcdefghijklmnop']
        assert not session.accepted_input and not bridge.writes
        assert session.viewer is None and not bridge.closed
        second = Viewer(attached['instance'])
        await endpoint(second)
        assert reg.sessions['abcdefghijklmnop'] is session
        assert session.bridge is bridge and not session.accepted_input
        assert spawn.call_count == 1
        assert bridge.writes == [b'\x0c']  # repaint only, no input/prompt replay
        await endpoint(Viewer(attached['instance'], abort=True))
        assert bridge.closed and not reg.sessions
    finally:
        await reg.shutdown()

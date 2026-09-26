"""Old Dashboard terminal clients cannot spawn a process or submit a turn."""

from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.mark.parametrize('query', ['', '&attach=old-tab&resume=durable', '&chat_mode=terminal'])
def test_old_pty_websocket_is_unavailable_without_spawning(query, monkeypatch):
    from hermes_cli import web_server
    from tui_gateway import server
    import subprocess

    spawn = Mock(side_effect=AssertionError('Retired endpoint spawned a process'))
    submit = Mock(side_effect=AssertionError('Retired endpoint submitted a prompt'))
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(server, '_run_prompt_submit', submit)
    before = set(server._sessions)
    client = TestClient(web_server.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f'/api/pty?token={web_server._SESSION_TOKEN}{query}', subprotocols=['hermes.pty-control.v1']):
            pytest.fail('Retired PTY endpoint accepted a connection')
    assert not spawn.called
    assert not submit.called
    assert set(server._sessions) == before
    assert all(getattr(route, 'path', None) != '/api/pty' for route in web_server.app.routes)


def test_retired_handoff_rpc_cannot_release_session():
    from tui_gateway import server

    before = dict(server._sessions)
    response = server.handle_request({'id': 'retired', 'method': 'session.handoff', 'params': {'session_id': 'old', 'action': 'release'}})
    assert 'error' in response
    assert server._sessions == before


def test_private_presentation_publisher_is_rejected():
    from hermes_cli import web_server

    with pytest.raises(WebSocketDisconnect):
        with TestClient(web_server.app).websocket_connect(f'/api/pub?token={web_server._SESSION_TOKEN}&channel=chat&presentation=old'):
            pytest.fail('Retired private publisher accepted a connection')


def test_ordinary_event_publisher_still_broadcasts():
    from hermes_cli import web_server

    client = TestClient(web_server.app)
    token = web_server._SESSION_TOKEN
    with client.websocket_connect(f'/api/events?token={token}&channel=p7') as events:
        with client.websocket_connect(f'/api/pub?token={token}&channel=p7') as publisher:
            publisher.send_text('{"type":"tool.complete","payload":{"name":"terminal"}}')
            assert events.receive_json()['payload']['name'] == 'terminal'

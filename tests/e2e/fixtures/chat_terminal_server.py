"""Real UI-C1 PTY fixture; only the provider HTTP boundary is deterministic.

Run inside an isolated container alongside Vite and terminal-foundation.cjs.
The production /chat route remains Native-only.
"""
import os
import runpy
from pathlib import Path

fixture = runpy.run_path(str(Path(__file__).with_name('chat_native_server.py')))
w = fixture['w']
profile = fixture['home'] / 'profiles' / 'work'
profile.mkdir(parents=True, exist_ok=True)
(profile / 'config.yaml').write_text((fixture['home'] / 'config.yaml').read_text())
from hermes_cli.pty_transport import registry
from tui_gateway import server
from hermes_state import SessionDB


@w.app.get('/c1-evidence')
async def evidence():
    sessions = registry(w.app).sessions
    with SessionDB(fixture['home'] / 'state.db') as db:
        durable = db.list_sessions_rich()
    profile_rows = []
    if (profile / 'state.db').exists():
        with SessionDB(profile / 'state.db') as profile_db:
            profile_rows = profile_db.list_sessions_rich()
    return {
        'pty': [{'instance': s.instance, 'pid': s.bridge.pid if s.bridge else None,
                 'input_bytes': s.input_bytes, 'accepted': s.accepted_input,
                 'frozen': s.frozen, 'closing': s.closing, 'profile': s.profile,
                 'viewer': s.viewer is not None, 'owner': s.owner is not None} for s in sessions.values()],
        'live': [{'runtime': sid, 'stored': server._session_lookup_key(s),
                  'terminal': s.get('terminal_owner') is not None,
                  'running': s.get('running')} for sid, s in server._sessions.items()],
        'durable': [{'id': s['id'], 'message_count': s['message_count']} for s in durable],
        'submissions': fixture['submissions'],
        'profile_durable': [{'id': s['id'], 'message_count': s['message_count']} for s in profile_rows],
    }


@w.app.post('/c1-drop-viewer')
async def drop_viewer():
    for session in registry(w.app).sessions.values():
        if session.viewer:
            await session.viewer.close(code=1012)
    return {'ok': True}


@w.app.get('/c1-process/{pid}')
async def process(pid: int):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {'alive': False}
    return {'alive': True}


w.app.router.routes.sort(key=lambda route: 0 if getattr(route, 'path', '').startswith(('/c1-', '/p7-', '/v1/')) else 1)
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(w.app, host='127.0.0.1', port=fixture['port'], log_level='warning')

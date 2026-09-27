"""UI-C2 real infrastructure, with deterministic provider and failure injection."""
import asyncio
import runpy
from pathlib import Path

foundation = runpy.run_path(str(Path(__file__).with_name('chat_terminal_server.py')))
w = foundation['w']
server = foundation['server']
fixture = foundation['fixture']
faults = {'native_init': False}
resume = server._methods['session.resume']


def resume_with_fault(rid, params):
    if faults['native_init'] and params.get('presentation_generation'):
        faults['native_init'] = False
        return server._err(rid, 5000, 'Injected Native startup failure')
    return resume(rid, params)


server._methods['session.resume'] = resume_with_fault


@w.app.post('/c2-fail-native')
async def fail_native():
    faults['native_init'] = True
    return {'ok': True}


@w.app.post('/c2-reset')
async def reset():
    reg = foundation['registry'](w.app)
    for session in list(reg.sessions.values()):
        await reg.close(session)
    for sid in list(server._sessions):
        await asyncio.to_thread(server._close_session_by_id, sid)
    return {'ok': True}


w.app.router.routes.sort(key=lambda route: 0 if getattr(route, 'path', '').startswith(('/c1-', '/c2-', '/p7-', '/v1/')) else 1)
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(w.app, host='127.0.0.1', port=fixture['port'], log_level='warning')

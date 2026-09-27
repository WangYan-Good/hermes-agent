"""Profile-bound launch of the existing TUI against the current gateway."""
import os
from pathlib import Path
from urllib.parse import urlencode, urlunsplit


def internal_url(web, path, query):
    host = getattr(web.app.state, 'bound_host', '127.0.0.1')
    if host in {'0.0.0.0', '::'}:
        host = '127.0.0.1' if host == '0.0.0.0' else '[::1]'
    elif ':' in host and not host.startswith('['):
        host = f'[{host}]'
    port = getattr(web.app.state, 'bound_port', None)
    if not port:
        raise RuntimeError('Dashboard bound port unavailable')
    if getattr(web.app.state, 'auth_required', False):
        from hermes_cli.dashboard_auth.ws_tickets import internal_ws_credential
        query = {**query, 'internal': internal_ws_credential()}
    else:
        query = {**query, 'token': web._SESSION_TOKEN}
    return urlunsplit(('ws', f'{host}:{port}', path, urlencode(query), ''))


def launch(web, session, resume=None):
    from hermes_cli.main import PROJECT_ROOT, _apply_tui_python_env, _make_tui_argv
    from tools.environments.local import build_subprocess_env
    from tui_gateway.terminal_presentation import Authority
    session.gateway = Authority(session)
    directory = Path(os.environ.get('HERMES_TUI_DIR') or PROJECT_ROOT / 'ui-tui')
    argv, cwd = _make_tui_argv(directory, False)
    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
    _apply_tui_python_env(env)
    env.update(HERMES_TUI_DASHBOARD='1', HERMES_TUI_INLINE='0',
               HERMES_TUI_MOUSE_TRACKING='wheel', COLORTERM='truecolor')
    env.pop('HERMES_TUI_DISABLE_MOUSE', None)
    # Never inherit a previous terminal's routing, resume or publisher context.
    for key in ('HERMES_TUI_RESUME', 'HERMES_TUI_SIDECAR_URL', 'HERMES_TUI_ACTIVE_SESSION_FILE'):
        env.pop(key, None)
    query = {'instance': session.instance, 'capability': session.capability}
    env['HERMES_TUI_GATEWAY_URL'] = internal_url(web, '/api/ws', {'pty_instance': session.instance, 'capability': session.capability})
    env['HERMES_TUI_PRESENTATION_URL'] = internal_url(web, '/api/pty', {**query, 'role': 'owner'})
    if resume:
        env['HERMES_TUI_RESUME'] = resume
    # Gateway forces the authenticated instance's profile on every RPC. Do not
    # switch the server process environment or spawn a second profile gateway.
    if os.name == 'nt':
        from .win_pty_bridge import WinPtyBridge as Bridge
    else:
        from .pty_bridge import PtyBridge as Bridge
    return Bridge.spawn(argv, cwd=str(cwd), env=env)

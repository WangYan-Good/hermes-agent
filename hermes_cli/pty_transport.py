"""Authenticated Dashboard Terminal Chat transport and cleanup receipts."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
from contextlib import suppress

from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

from .pty_control import PROTOCOL, control, owner_connection
from .pty_session import PtyConflict, PtySessionRegistry

_ID = re.compile(r'^[A-Za-z0-9_-]{16,128}$')


def registry(app):
    if not hasattr(app.state, 'pty_registry'):
        app.state.pty_registry = PtySessionRegistry()
    return app.state.pty_registry


def resolve_owner(app, instance, capability):
    for session in registry(app).sessions.values():
        if session.instance == instance and not session.closing:
            if hmac.compare_digest(session.capability, capability or ''):
                return session
    raise PtyConflict('Invalid Terminal owner capability')


async def endpoint(ws):
    from hermes_cli import web_server as web
    if not web._DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4404)
        return
    if not web._ws_auth_ok(ws):
        await ws.close(code=4401)
        return
    if not web._ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return
    protocols = {p.strip() for p in ws.headers.get('sec-websocket-protocol', '').split(',')}
    if PROTOCOL not in protocols:
        await ws.close(code=4400, reason='hermes.pty-control.v1 required')
        return
    reg = registry(ws.app)
    if ws.query_params.get('role') == 'owner':
        try:
            session = resolve_owner(ws.app, ws.query_params.get('instance'), ws.query_params.get('capability'))
            await owner_connection(session, ws)
        except (PtyConflict, WebSocketDisconnect, ValueError, RuntimeError):
            with suppress(RuntimeError):
                await ws.close(code=4403)
        return
    if ws.query_params.get('role'):
        await ws.close(code=4400)
        return
    key, generation = ws.query_params.get('attach', ''), ws.query_params.get('generation', '')
    if not _ID.fullmatch(key) or not _ID.fullmatch(generation):
        await ws.close(code=4400, reason='attach and generation required')
        return
    profile = ws.query_params.get('profile', '').strip()
    try:
        if profile and profile != 'current':
            home = web._resolve_profile_dir(profile).resolve()
        else:
            home = web.get_hermes_home().resolve()
        # Canonical profile name is used by the existing profile-scoped RPCs.
        from hermes_cli.profiles import normalize_profile_name
        profile = '' if home == web.get_hermes_home().resolve() else normalize_profile_name(profile)
    except (HTTPException, ValueError):
        await ws.close(code=4400, reason='invalid profile')
        return
    principal = ws.scope.get('attachment_principal', ('loopback', 'dashboard'))
    resume = ws.query_params.get('resume') or None
    instance = ws.query_params.get('instance')
    if ws.query_params.get('receipt') == '1':
        try:
            result = reg.release_receipt(key, principal, profile, instance, generation)
            await ws.accept(subprotocol=PROTOCOL)
            await ws.send_json({'type': 'receipt', 'result': result})
            await ws.close(code=1000)
        except PtyConflict:
            await ws.close(code=4409, reason='Terminal cleanup unconfirmed')
        return
    session = None
    attached = False
    from .dashboard_tui_env import launch
    try:
        # Validation is complete; no rejected handshake above can spawn.
        await ws.accept(subprotocol=PROTOCOL)
        session = await reg.acquire(key, principal, profile, lambda s: launch(web, s, resume), instance=instance)
        await session.attach(ws, generation, instance)
        attached = True
        while True:
            message = await ws.receive()
            if message['type'] == 'websocket.disconnect':
                break
            raw = message.get('bytes')
            if raw is not None:
                if raw:
                    await session.input(ws, generation, raw)
                continue
            try:
                frame = json.loads(message.get('text', ''))
                if not isinstance(frame, dict):
                    raise PtyConflict('Expected control object')
                session.check_viewer(ws, generation)
                if frame.get('type') == 'resize':
                    await asyncio.to_thread(session.bridge.resize, int(frame['cols']), int(frame['rows']))
                elif frame.get('type') == 'control':
                    try:
                        await control(session, reg, ws, frame)
                    except (PtyConflict, TimeoutError) as exc:
                        await session.send({'type': 'control', 'id': frame.get('id'), 'error': str(exc)})
                    if session.closing:
                        break
                else:
                    raise PtyConflict('Unknown control frame')
            except (ValueError, KeyError, TypeError) as exc:
                raise PtyConflict('Invalid control frame') from exc
    except (PtyConflict, OSError, RuntimeError, SystemExit) as exc:
        with suppress(RuntimeError, WebSocketDisconnect):
            await ws.close(code=4409, reason=str(exc)[:100])
    except WebSocketDisconnect:
        pass
    finally:
        if session is not None:
            if attached:
                # An attached instance is reconnectable even before first input.
                # Only an explicit abort or incomplete startup is a failed target.
                session.detach(ws, reg.clock())
            elif not instance:
                await reg.close(session)

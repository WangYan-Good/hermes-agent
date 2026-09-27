"""Versioned Terminal lifecycle, separate from terminal bytes and /api/pub."""
from __future__ import annotations

import asyncio
import secrets

from .pty_session import PtyConflict

PROTOCOL = 'hermes.pty-control.v1'
ACTIONS = {'status', 'prepare', 'cancel', 'release', 'abort'}


async def request_owner(session, action, *, ticket=None, timeout=15):
    if session.owner is None:
        raise PtyConflict('Terminal control owner unavailable')
    request_id = secrets.token_urlsafe(18)
    future = asyncio.get_running_loop().create_future()
    session.pending[request_id] = future
    try:
        await session.owner.send_json({
            'type': 'control', 'id': request_id, 'instance': session.instance,
            'generation': session.generation, 'action': action,
            'input_bytes': session.input_bytes, 'ticket': ticket,
        })
        return await asyncio.wait_for(future, timeout)
    finally:
        session.pending.pop(request_id, None)


async def control(session, registry, ws, frame):
    action = frame.get('action')
    generation = frame.get('generation')
    if action not in ACTIONS or frame.get('instance') != session.instance:
        raise PtyConflict('Invalid lifecycle frame')
    async with session.admission:
        session.check_viewer(ws, generation)
        if action in {'prepare', 'release', 'abort'}:
            session.frozen = True
        if action == 'abort':
            if session.accepted_input:
                raise PtyConflict('Cannot abort after accepted input')
            # Claim cleanup before releasing the input admission lock.
            session.closing = True
    if action == 'abort':
        # Keep the browser socket alive until its cleanup ACK has been sent.
        session.viewer = None
        await registry.close(session)
        registry.remember_release(session, {'released': True})
        await ws.send_json({'type': 'control', 'id': frame.get('id'), 'result': {'released': True}})
        await ws.close(code=1000)
        return
    result = await request_owner(session, action, ticket=frame.get('ticket'))
    session.check_viewer(ws, generation)
    if not isinstance(result, dict):
        raise PtyConflict('Malformed owner response')
    if action == 'cancel':
        if result.get('cancelled') is not True:
            raise PtyConflict('Cancellation not acknowledged')
        session.frozen = False
    if action == 'release':
        if result.get('released') is not True:
            raise PtyConflict('Release not acknowledged')
        session.viewer = None
        await registry.close(session)
        registry.remember_release(session, result)
        await ws.send_json({'type': 'control', 'id': frame.get('id'), 'result': result})
        await ws.close(code=1000)
        return
    await session.send({'type': 'control', 'id': frame.get('id'), 'result': result})


async def owner_connection(session, ws):
    if session.owner is not None or session.closing:
        raise PtyConflict('Terminal already has a control owner')
    session.owner = ws
    try:
        await ws.accept(subprotocol=PROTOCOL)
        await session.send({'type': 'owner-ready', 'instance': session.instance, 'frozen': session.frozen})
        while True:
            frame = await ws.receive_json()
            if not isinstance(frame, dict) or frame.get('instance') != session.instance:
                raise PtyConflict('Invalid owner instance')
            if frame.get('type') == 'changed':
                await session.send({'type': 'changed', 'instance': session.instance})
                continue
            if frame.get('generation') != session.generation:
                continue
            pending = session.pending.get(frame.get('id'))
            if pending is not None and not pending.done():
                if 'error' in frame:
                    pending.set_exception(PtyConflict('Owner could not confirm lifecycle'))
                else:
                    pending.set_result(frame.get('result'))
    finally:
        if session.owner is ws:
            session.owner = None
            # Uncertainty never re-opens input admission.
            session.frozen = True
            if session.gateway is not None:
                with session.gateway.lock:
                    session.gateway.frozen = True
                    session.gateway.ticket = None
            for future in tuple(session.pending.values()):
                if not future.done():
                    future.set_exception(PtyConflict('Terminal control disconnected'))

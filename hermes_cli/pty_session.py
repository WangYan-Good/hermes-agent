"""Single-viewer PTY resources. All registry mutations run on the ASGI loop.

Spawn reservations remain visible until late children have been reaped. No
registry/global lock is held over process I/O; instance locks only serialize
that instance's output or admitted input.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field


class PtyConflict(RuntimeError):
    pass


@dataclass(eq=False)
class PtySession:
    key: str
    principal: object
    profile: str
    instance: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    capability: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    bridge: object = None
    viewer: object = None
    generation: str | None = None
    owner: object = None
    gateway: object = None
    frozen: bool = False
    closing: bool = False
    input_bytes: int = 0
    accepted_input: bool = False
    detached_at: float | None = None
    buffer: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    pending: dict = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    admission: asyncio.Lock = field(default_factory=asyncio.Lock)
    output: asyncio.Lock = field(default_factory=asyncio.Lock)
    drain: asyncio.Task | None = None
    spawn_task: asyncio.Task | None = None
    close_task: asyncio.Task | None = None

    def check_viewer(self, ws, generation):
        if self.closing or self.viewer is not ws or self.generation != generation:
            raise PtyConflict('stale PTY viewer')

    async def input(self, ws, generation, data):
        async with self.admission:
            self.check_viewer(ws, generation)
            if self.frozen or self.owner is None or self.bridge is None:
                raise PtyConflict('PTY input is blocked')
            # Count BEFORE writing: ambiguous/partial writes must never permit abort.
            self.accepted_input = True
            self.input_bytes += len(data)
            await asyncio.to_thread(self.bridge.write, data)

    async def send(self, frame):
        async with self.output:
            if self.viewer is not None:
                await self.viewer.send_json(frame)

    async def attach(self, ws, generation, instance=None):
        async with self.admission:
            if self.closing or self.viewer is not None:
                raise PtyConflict('PTY already has an input owner')
            if instance is not None and instance != self.instance:
                raise PtyConflict('PTY instance expired')
            self.viewer, self.generation = ws, generation
            self.detached_at = None
        async with self.output:
            await ws.send_json({'type': 'attached', 'instance': self.instance, 'generation': generation, 'control_confirmed': self.owner is not None, 'frozen': self.frozen})
            if self.truncated:
                await ws.send_bytes(b'\x1b[2J\x1b[H')
                # Ctrl-L is a redraw, never an input admission or lifecycle command.
                self.input_bytes += 1
                await asyncio.to_thread(self.bridge.write, b'\x0c')
                self.buffer.clear()
            elif self.buffer:
                await ws.send_bytes(bytes(self.buffer))
            if instance and not self.truncated:
                self.input_bytes += 1
                await asyncio.to_thread(self.bridge.write, b'\x0c')

    def detach(self, ws, now):
        if self.viewer is ws:
            self.viewer = None
            self.detached_at = now


class PtySessionRegistry:
    def __init__(self, *, ttl=1800, max_sessions=16, buffer_cap=1024 * 1024, clock=time.monotonic):
        self.ttl, self.max_sessions, self.buffer_cap = ttl, max_sessions, buffer_cap
        self.clock = clock
        self.sessions: dict[str, PtySession] = {}
        self.stopping = False
        self.receipts = {}

    def remember_release(self, session, result):
        now = self.clock()
        self.receipts = {key: value for key, value in self.receipts.items() if now - value[0] < 900}
        if len(self.receipts) >= 2048:
            self.receipts.pop(next(iter(self.receipts)))
        self.receipts[session.key] = (now, session.principal, session.profile, session.instance, session.generation, result)

    def release_receipt(self, key, principal, profile, instance, generation):
        receipt = self.receipts.get(key)
        if receipt is None or self.clock() - receipt[0] >= 900 or receipt[1:5] != (principal, profile, instance, generation):
            raise PtyConflict('Terminal cleanup unconfirmed')
        return receipt[5]

    async def acquire(self, key, principal, profile, spawn, *, instance=None):
        if self.stopping:
            raise PtyConflict('PTY server stopping')
        existing = self.sessions.get(key)
        if existing:
            if (existing.principal, existing.profile) != (principal, profile):
                raise PtyConflict('PTY attachment scope mismatch')
            if not instance or instance != existing.instance or existing.closing:
                raise PtyConflict('PTY instance mismatch or starting')
            await existing.ready.wait()
            if existing.closing or not existing.bridge:
                raise PtyConflict('PTY startup failed')
            return existing
        if instance:
            raise PtyConflict('PTY instance expired; explicit new terminal required')
        if len(self.sessions) >= self.max_sessions:
            raise PtyConflict('PTY registry full')
        session = PtySession(key, principal, profile)
        self.sessions[key] = session  # reservation before the first await

        async def start():
            try:
                session.bridge = await asyncio.to_thread(spawn, session)
                if not session.closing:
                    session.drain = asyncio.create_task(self._drain(session))
            except BaseException:
                session.closing = True
                raise
            finally:
                session.ready.set()

        session.spawn_task = asyncio.create_task(start())
        try:
            await asyncio.shield(session.spawn_task)
            if session.closing or self.stopping:
                raise PtyConflict('PTY startup cancelled')
            return session
        except BaseException:
            await asyncio.shield(self.close(session))
            raise

    async def _drain(self, session):
        try:
            while not session.closing:
                chunk = await asyncio.to_thread(session.bridge.read, 0.2)
                if chunk is None:
                    break
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                async with session.output:
                    session.buffer.extend(chunk)
                    if len(session.buffer) > self.buffer_cap:
                        del session.buffer[:-self.buffer_cap]
                        session.truncated = True
                    if session.viewer is not None:
                        try:
                            await session.viewer.send_bytes(chunk)
                        except Exception:
                            session.detach(session.viewer, self.clock())
        finally:
            if not session.closing:
                asyncio.create_task(self.close(session))

    async def close(self, session):
        if session.close_task is None:
            session.closing = True
            session.frozen = True
            session.close_task = asyncio.create_task(self._close(session))
        await asyncio.shield(session.close_task)

    async def _close(self, session):
        if session.spawn_task:
            with suppress(Exception):
                await session.spawn_task
        if session.drain:
            session.drain.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await session.drain
        # Gateway cleanup uses the normal persistence/lease teardown, off-loop.
        from tui_gateway.terminal_presentation import close_owner
        failure = None
        try:
            await asyncio.to_thread(close_owner, session)
        except Exception as exc:
            failure = exc
        try:
            if session.bridge:
                await asyncio.to_thread(session.bridge.close)
        except Exception as exc:
            failure = failure or exc
        for future in tuple(session.pending.values()):
            if not future.done():
                future.set_exception(PtyConflict('PTY closed'))
        for ws in (session.owner, session.viewer):
            if ws:
                with suppress(Exception):
                    await ws.close(code=4410)
        session.owner = session.viewer = None
        if self.sessions.get(session.key) is session:
            self.sessions.pop(session.key)
        if failure is not None:
            raise failure  # never ACK release after a failed finalization

    async def reap(self):
        now = self.clock()
        doomed = [s for s in self.sessions.values() if s.detached_at is not None and now - s.detached_at >= self.ttl]
        await asyncio.gather(*(self.close(s) for s in doomed))

    async def shutdown(self):
        self.stopping = True
        await asyncio.gather(*(self.close(s) for s in list(self.sessions.values())))

    async def run_reaper(self):
        while True:
            await asyncio.sleep(30)
            await self.reap()

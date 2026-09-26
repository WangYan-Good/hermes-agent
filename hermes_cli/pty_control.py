"""Negotiated PTY lifecycle controls, isolated from all terminal input bytes."""

import asyncio
from contextlib import suppress

from starlette.websockets import WebSocketDisconnect
import json
import secrets

PROTOCOL = "hermes.pty-control.v1"
CONTROLLERS = {}


class PtyControl:
    def __init__(self, profile="", *, abortable=True):
        self.abortable = abortable
        self.profile = profile
        self.viewer_generation = None
        self.input_bytes = 0
        self.instance = secrets.token_hex(24)
        self.publisher = None
        self.viewer = None
        self.pending = {}
        self.frozen = False
        self.released = False
        CONTROLLERS[self.instance] = self

    async def request(self, frame):
        if not self.publisher:
            raise RuntimeError("TUI lifecycle channel unavailable")
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (future, frame.get("generation"))
        try:
            await self.publisher.send_json({
                "handoff": True,
                "id": request_id,
                "action": frame.get("action"),
                "ticket": frame.get("ticket"),
                "profile": self.profile,
                "generation": frame.get("generation"),
                "input_bytes": self.input_bytes,
            })
            return await asyncio.wait_for(future, 15)
        finally:
            self.pending.pop(request_id, None)

    async def handle(self, ws, frame, release):
        if ws is not self.viewer or frame.get("action") not in {
            "status",
            "prepare",
            "cancel",
            "release",
            "abort",
        }:
            return
        generation = frame.get("generation")
        if (
            frame.get("profile", "") != self.profile
            or not isinstance(generation, str)
            or not generation
        ):
            return
        if self.viewer_generation is None:
            self.viewer_generation = generation
        if generation != self.viewer_generation:
            return
        action = frame["action"]
        if action in {"prepare", "release"}:
            self.frozen = True
        try:
            if action == "abort":
                # Failed target initialization never admitted user input. Remove
                # its PTY even if its private control channel never came up.
                if self.input_bytes or not self.abortable:
                    raise RuntimeError("Active input owner cannot be aborted")
                self.frozen = True
                self.released = True
                await release()
                result = {"released": True}
            else:
                result = await self.request(frame)
            if ws is not self.viewer or generation != self.viewer_generation:
                return
            if action == "prepare" and not result.get("ready"):
                self.frozen = False
            if action == "cancel":
                self.frozen = False
            if action == "release" and result.get("released"):
                self.released = True
                # Ack only after process/registry cleanup releases its leases.
                await release()
            await ws.send_json({
                "handoff": True,
                "id": frame.get("id"),
                "result": result,
            })
        except Exception:
            # Never thaw on uncertainty: the TUI may already hold a prepare ticket.
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.send_json({
                    "handoff": True,
                    "id": frame.get("id"),
                    "error": "TUI handoff could not be confirmed",
                })

    async def publish(self, ws):
        if self.publisher is not None:
            await ws.close(code=4409)
            return
        self.publisher = ws
        try:
            await self.changed()
            while True:
                frame = json.loads(await ws.receive_text())
                if frame.get("id") in self.pending:
                    future, generation = self.pending[frame["id"]]
                    if (
                        frame.get("profile") != self.profile
                        or frame.get("generation") != generation
                    ):
                        continue
                    if not future.done():
                        if frame.get("error"):
                            future.set_exception(RuntimeError("TUI control failed"))
                        else:
                            future.set_result(frame.get("result", {}))
                elif frame.get("changed") and self.viewer:
                    await self.changed()
        finally:
            self.publisher = None
            for future, _generation in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("TUI controller disconnected"))

    async def changed(self):
        viewer = self.viewer
        if viewer:
            try:
                await viewer.send_json({"handoff": True, "changed": True})
            except (RuntimeError, WebSocketDisconnect):
                if self.viewer is viewer:
                    self.viewer = None

    def forget(self):
        CONTROLLERS.pop(self.instance, None)

"""Negotiated control authority is isolated from terminal data and stale viewers."""

import asyncio

import pytest

from hermes_cli.pty_control import PtyControl, CONTROLLERS


class Socket:
    def __init__(self):
        self.frames = []

    async def send_json(self, value):
        self.frames.append(value)


@pytest.mark.asyncio
async def test_prepare_error_keeps_input_frozen_until_authoritative_cancel():
    control = PtyControl("work")
    viewer = Socket()
    control.viewer = viewer
    frame = {"id": "one", "generation": "g", "profile": "work", "action": "prepare"}
    try:
        await control.handle(viewer, frame, None)
        assert control.frozen
        assert viewer.frames[-1]["error"]

        async def cancel(_frame):
            return {"cancelled": True}

        control.request = cancel
        await control.handle(viewer, {**frame, "action": "cancel"}, None)
        assert not control.frozen
    finally:
        control.forget()


@pytest.mark.asyncio
async def test_stale_profile_generation_and_viewer_cannot_release():
    control = PtyControl("work")
    viewer = Socket()
    control.viewer = viewer
    requests = []

    async def request(frame):
        requests.append(frame)
        return {"ready": False}

    control.request = request
    frame = {"id": "one", "generation": "g", "profile": "work", "action": "prepare"}
    try:
        await control.handle(viewer, frame, None)
        await control.handle(Socket(), frame, None)
        await control.handle(viewer, {**frame, "profile": "other"}, None)
        await control.handle(viewer, {**frame, "generation": "stale"}, None)
        assert len(requests) == 1
        assert not control.frozen
    finally:
        control.forget()


@pytest.mark.asyncio
async def test_release_ack_follows_runtime_cleanup_and_fences_stdin():
    control = PtyControl()
    viewer = Socket()
    publisher = Socket()
    control.viewer = viewer
    control.publisher = publisher
    control.input_bytes = 17
    frame = {"id": "one", "generation": "g", "profile": "", "action": "release"}
    cleaned = False

    async def release():
        nonlocal cleaned
        assert not viewer.frames
        cleaned = True
        control.forget()

    task = asyncio.create_task(control.handle(viewer, frame, release))
    await asyncio.sleep(0)
    forwarded = publisher.frames[0]
    assert forwarded["input_bytes"] == 17
    assert forwarded["generation"] == "g"
    future, _ = control.pending[forwarded["id"]]
    future.set_result({"released": True})
    await task
    assert cleaned and control.released
    assert viewer.frames[-1]["result"]["released"]
    assert control.instance not in CONTROLLERS


@pytest.mark.asyncio
async def test_closed_viewer_does_not_poison_the_private_publisher():
    from starlette.websockets import WebSocketDisconnect

    control = PtyControl()

    class ClosedViewer:
        async def send_json(self, frame):
            raise RuntimeError("already closed")

    class Publisher:
        async def receive_text(self):
            raise WebSocketDisconnect()

    control.viewer = ClosedViewer()
    try:
        with pytest.raises(WebSocketDisconnect):
            await control.publish(Publisher())
        assert control.publisher is None
        assert control.viewer is None
    finally:
        control.forget()

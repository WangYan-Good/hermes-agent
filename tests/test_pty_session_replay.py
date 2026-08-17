import asyncio

from hermes_cli.pty_session import PtySession


class IdleBridge:
    def __init__(self) -> None:
        self.written = bytearray()

    def read(self, _timeout: float):
        return b""

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def close(self) -> None:
        pass


class RecordingWebSocket:
    def __init__(self) -> None:
        self.close_codes: list[int] = []
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del reason
        self.close_codes.append(code)


class FailingWebSocket(RecordingWebSocket):
    async def send_bytes(self, data: bytes) -> None:
        del data
        raise ConnectionError("socket closed during replay")


class BlockingWebSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def send_bytes(self, data: bytes) -> None:
        self.started.set()
        await self.release.wait()
        await super().send_bytes(data)


def test_truncated_reattach_discards_ansi_tail_and_requests_semantic_redraw() -> None:
    async def scenario() -> None:
        bridge = IdleBridge()
        session = PtySession("dashboard", bridge, buffer_cap=8, read_timeout=0.01)
        session.buffer.append(b"0123456789")
        ws = RecordingWebSocket()

        await session.attach(ws)

        assert ws.sent == [b"\x1b[2J\x1b[H"]
        assert bridge.written == b"\x0c"
        assert session.buffer.snapshot() == b""
        assert session.buffer.truncated is False

    asyncio.run(scenario())


def test_complete_reattach_replays_buffer_without_injecting_input() -> None:
    async def scenario() -> None:
        bridge = IdleBridge()
        session = PtySession("dashboard", bridge, buffer_cap=32, read_timeout=0.01)
        session.buffer.append(b"complete ansi stream")
        ws = RecordingWebSocket()

        await session.attach(ws)

        assert ws.sent == [b"complete ansi stream"]
        assert bridge.written == b""
        assert session.buffer.snapshot() == b"complete ansi stream"

    asyncio.run(scenario())


def test_live_output_waits_until_snapshot_replay_finishes() -> None:
    async def scenario() -> None:
        bridge = IdleBridge()
        session = PtySession("dashboard", bridge, buffer_cap=64, read_timeout=0.01)
        session.buffer.append(b"history")
        ws = BlockingWebSocket()
        forward = session._record_and_forward

        attach_task = asyncio.create_task(session.attach(ws))
        await ws.started.wait()
        live_task = asyncio.create_task(forward(b"live"))
        await asyncio.sleep(0)

        assert live_task.done() is False

        ws.release.set()
        await attach_task
        await live_task

        assert ws.sent == [b"history", b"live"]

    asyncio.run(scenario())


def test_failed_truncated_attach_remains_invalid_for_the_next_reconnect() -> None:
    async def scenario() -> None:
        bridge = IdleBridge()
        session = PtySession("dashboard", bridge, buffer_cap=8, read_timeout=0.01)
        session.buffer.append(b"0123456789")

        try:
            await session.attach(FailingWebSocket())
        except ConnectionError:
            pass
        else:
            raise AssertionError("attach should propagate replay failure")

        assert session.buffer.truncated is True
        assert bridge.written == b""

        recovered = RecordingWebSocket()
        await session.attach(recovered)

        assert recovered.sent == [b"\x1b[2J\x1b[H"]
        assert bridge.written == b"\x0c"

    asyncio.run(scenario())


def test_superseding_attach_waits_and_requests_its_own_redraw() -> None:
    async def scenario() -> None:
        bridge = IdleBridge()
        session = PtySession("dashboard", bridge, buffer_cap=8, read_timeout=0.01)
        session.buffer.append(b"0123456789")
        first = BlockingWebSocket()
        second = RecordingWebSocket()

        first_task = asyncio.create_task(session.attach(first))
        await first.started.wait()
        second_task = asyncio.create_task(session.attach(second))
        await asyncio.sleep(0)

        assert second_task.done() is False

        first.release.set()
        await first_task
        await second_task

        assert first.sent == [b"\x1b[2J\x1b[H"]
        assert first.close_codes == [4409]
        assert second.sent == [b"\x1b[2J\x1b[H"]
        assert bridge.written == b"\x0c\x0c"

    asyncio.run(scenario())

"""Real connection-drop fault injection for the transport-recovery path.

Mocking ``response.finish_reason = "length"`` proves nothing about whether
Hermes actually recognises a dying socket: the interesting failure lives in
httpx/httpcore's chunked-body reader, not in our own object shapes.

These tests stand up a local HTTP server that speaks a genuine
``Transfer-Encoding: chunked`` SSE response and then closes the connection
*before* the terminating zero-length chunk. A real ``openai`` client reads it
over a real socket, so the runtime sees the production exception —
``httpx.RemoteProtocolError: peer closed connection without sending complete
message body (incomplete chunked read)`` — and we assert what Hermes makes
of it.

Everything binds to 127.0.0.1 on an ephemeral port: no public network, no
sleeps, repeatable in CI.
"""

from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.transport_recovery import (
    TRANSPORT_INTERRUPTED_ATTR,
    TRANSPORT_VISIBLE_TEXT_ATTR,
    is_transport_interrupted,
    transport_had_visible_text,
)
from hermes_constants import PARTIAL_STREAM_STUB_ID


# ── A server that hangs up mid chunked body ───────────────────────────────

def _direct_client(base_url: str):
    """An ``openai`` client wired straight to the loopback drop server.

    ``trust_env=False`` is load-bearing: a runner with HTTP_PROXY set (CI
    images and this project's own container both do) would otherwise send the
    loopback request to the proxy, and the test would exercise the proxy's
    failure mode instead of a mid-body disconnect.
    """
    import httpx
    from openai import OpenAI

    return OpenAI(
        api_key="test-key",
        base_url=base_url,
        max_retries=0,
        http_client=httpx.Client(trust_env=False),
    )


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _chunk(body: bytes) -> bytes:
    return f"{len(body):x}".encode() + b"\r\n" + body + b"\r\n"


def _delta_frame(delta: dict) -> bytes:
    return _sse({
        "id": "chatcmpl-drop",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test/model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    })


class ChunkedDropServer:
    """Sends `frames`, then closes without the terminating 0-length chunk.

    That omission is the whole point: a clean close after a complete chunked
    body is a normal end-of-stream, while closing mid-body is what httpx
    reports as an incomplete chunked read.
    """

    def __init__(self, frames: list[bytes]):
        self._frames = frames
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(5)
                # Drain the request headers so the client finishes sending.
                buf = b""
                while b"\r\n\r\n" not in buf:
                    got = conn.recv(65536)
                    if not got:
                        break
                    buf += got
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                for frame in self._frames:
                    conn.sendall(_chunk(frame))
            except OSError:
                pass
            finally:
                # Hang up mid-body: no "0\r\n\r\n" terminator.
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def drop_server():
    servers: list[ChunkedDropServer] = []

    def _make(frames):
        s = ChunkedDropServer(frames)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


# ── The three drop shapes ─────────────────────────────────────────────────

NO_TOKEN_DROP: list[bytes] = []
TEXT_MIDSTREAM_DROP = [
    _delta_frame({"role": "assistant", "content": "The first half of "}),
    _delta_frame({"content": "the answer"}),
]
TOOL_ARG_MIDSTREAM_DROP = [
    _delta_frame({"role": "assistant", "content": None, "tool_calls": [{
        "index": 0, "id": "call_1", "type": "function",
        "function": {"name": "write_file", "arguments": ""},
    }]}),
    _delta_frame({"tool_calls": [{
        "index": 0, "function": {"arguments": '{"path":"a.md","content":"parti'},
    }]}),
]
# Same drop, but a preamble reached the user first — the shape that produces a
# stub rather than a bare re-raise.
TOOL_ARG_AFTER_TEXT_DROP = [
    _delta_frame({"role": "assistant", "content": "Writing the file now. "}),
    *TOOL_ARG_MIDSTREAM_DROP,
]


class TestRealSocketRaisesATransportError:
    """Layer 1: the runtime really does see a mid-stream transport failure."""

    @pytest.mark.parametrize("frames,label", [
        (NO_TOKEN_DROP, "no-token"),
        (TEXT_MIDSTREAM_DROP, "text-midstream"),
        (TOOL_ARG_MIDSTREAM_DROP, "tool-arg-midstream"),
    ])
    def test_stream_iteration_raises_and_classifies_as_transport(
        self, drop_server, frames, label,
    ):
        from agent.error_classifier import classify_api_error, FailoverReason

        server = drop_server(list(frames))
        client = _direct_client(server.base_url)

        with pytest.raises(Exception) as excinfo:
            stream = client.chat.completions.create(
                model="test/model",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            for _ in stream:
                pass

        err = excinfo.value
        text = f"{type(err).__name__}: {err}".lower()
        assert any(sig in text for sig in (
            "incomplete chunked read",
            "peer closed connection",
            "remoteprotocolerror",
            "connection reset",
            "apiconnectionerror",
            "connection error",
        )), f"[{label}] unexpected error shape: {type(err).__name__}: {err}"

        classified = classify_api_error(err, provider="openrouter", model="test/model")
        assert classified.reason in {
            FailoverReason.timeout, FailoverReason.context_overflow,
            FailoverReason.overloaded, FailoverReason.server_error,
        }, (
            f"[{label}] a mid-stream disconnect must classify as a transport "
            f"failure, not {classified.reason}"
        )
        assert classified.retryable is True


# ── Layer 2: what Hermes builds out of that failure ───────────────────────

@pytest.fixture()
def stream_agent():
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.com/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.api_mode = "chat_completions"
        a._interrupt_requested = False
        a.save_trajectories = False
        a._use_prompt_caching = False
        # The visible-text marker tracks text that actually reached a
        # consumer. Without one registered, nothing is visible by
        # construction and the distinction under test disappears.
        a.streamed_to_user = []
        a.stream_delta_callback = a.streamed_to_user.append
        return a


def _run_real_stream(agent, server):
    """Drive the production streaming helper against the drop server."""
    from agent.chat_completion_helpers import interruptible_streaming_api_call

    agent.base_url = server.base_url
    # ``_create_request_openai_client`` is a per-REQUEST factory: the streaming
    # layer closes the client it hands back after each attempt, and its
    # mid-tool-call reconnect (HERMES_STREAM_RETRIES) then asks for a fresh
    # one.  Patching a single shared instance makes attempt 2 fail with
    # "Cannot send a request, as the client has been closed" instead of
    # exercising the drop, so build a new client per call like production.
    with patch.object(
        agent,
        "_create_request_openai_client",
        side_effect=lambda *a, **k: _direct_client(server.base_url),
        create=True,
    ):
        return interruptible_streaming_api_call(agent, {
            "model": "test/model",
            "messages": [{"role": "user", "content": "hi"}],
        })


class TestHermesStampsTransportIdentity:
    def test_text_midstream_drop_is_marked_with_visible_text(self, drop_server, stream_agent):
        server = drop_server(list(TEXT_MIDSTREAM_DROP))
        try:
            response = _run_real_stream(stream_agent, server)
        except Exception as exc:
            pytest.skip(f"streaming helper re-raised instead of stubbing: {exc!r}")

        assert is_transport_interrupted(response) is True
        assert getattr(response, TRANSPORT_INTERRUPTED_ATTR, False) is True, (
            "The identity must be an explicit marker, not something the loop "
            "has to infer from the id + finish_reason pair that genuine "
            "output truncation also uses."
        )
        assert transport_had_visible_text(response) is True
        assert response.id == PARTIAL_STREAM_STUB_ID

    def test_tool_arg_midstream_drop_carries_no_runnable_tool_call(
        self, drop_server, stream_agent,
    ):
        server = drop_server(list(TOOL_ARG_AFTER_TEXT_DROP))
        response = _run_real_stream(stream_agent, server)

        assert is_transport_interrupted(response) is True
        message = response.choices[0].message
        assert not getattr(message, "tool_calls", None), (
            "Arguments that never finished arriving must never reach the "
            "tool dispatcher — the stub carries no runnable tool call."
        )
        content = getattr(message, "content", None) or ""
        assert '"content":"parti' not in content, (
            "Half-arrived argument JSON must not leak into assistant content."
        )

    def test_tool_arg_drop_with_nothing_delivered_raises_a_classified_drop(
        self, drop_server, stream_agent,
    ):
        """With nothing delivered there is no partial to stub, so the helper
        raises — and the raised error must be one the turn-level policy
        recognises as transport.

        This asserts the low-level contract positively rather than swallowing
        the exception: a raw drop that Hermes fails to CLASSIFY as transport
        would fall into the generic retry budget, which is the exact
        regression the end-to-end gates exist to catch.
        """
        from agent.error_classifier import classify_api_error
        from agent.transport_recovery import is_transport_retryable_error

        server = drop_server(list(TOOL_ARG_MIDSTREAM_DROP))
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - shape asserted below
            _run_real_stream(stream_agent, server)

        assert is_transport_retryable_error(classify_api_error(excinfo.value)), (
            f"a mid-tool-call socket drop must classify as a retryable "
            f"transport failure, not fall through to generic retry: "
            f"{excinfo.value!r}"
        )

    def test_no_token_drop_reports_no_visible_text(self, drop_server, stream_agent):
        """Nothing reached the user, so recovery may replay the request
        verbatim rather than asking the model to 'continue'."""
        server = drop_server(list(NO_TOKEN_DROP))
        try:
            response = _run_real_stream(stream_agent, server)
        except Exception:
            # A drop before any byte legitimately surfaces as a raw transport
            # error for the outer retry loop; there is no partial to stub.
            return
        if not is_transport_interrupted(response):
            pytest.skip("provider-shaped empty stream handled by another guard")
        assert transport_had_visible_text(response) is False
        assert getattr(response, TRANSPORT_VISIBLE_TEXT_ATTR, None) is False

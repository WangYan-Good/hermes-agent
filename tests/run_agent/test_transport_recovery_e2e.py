"""End-to-end transport-recovery gates driven through ``run_conversation``.

``test_transport_fault_injection`` proves Hermes *recognises* a dying socket,
but it stops at ``interruptible_streaming_api_call``. The property P1 actually
promises lives one level up: given one dropped connection, how many requests
does the agent send, in which modes, and with how many synthetic continuation
nudges? Only ``run_conversation`` can answer that, because it owns the
recovery budget, the mode choice and the transcript.

So these tests stand up a scripted loopback provider that records every
request it receives and replays a per-request behaviour: a genuine chunked
SSE body that hangs up mid-stream, a valid non-streaming completion, or an
abrupt hangup with no response at all. A real ``openai`` client talks to it
over a real socket, so the runtime raises production transport exceptions
rather than fixtures shaped like them.

The assertions are deliberately about the *request sequence*, not just the
final answer: a recovery that returns the right text after five requests and
three nudges is exactly the regression this phase exists to prevent.

Everything binds to 127.0.0.1 on an ephemeral port: no public network, no
sleeps, repeatable in CI.
"""

from __future__ import annotations

import json
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

from tests.run_agent.test_transport_fault_injection import (
    _chunk,
    _delta_frame,
    _direct_client,
    _sse,
)

# The exact nudge texts the loop appends, imported rather than re-typed so a
# reworded prompt can never silently stop being counted here.
from agent.conversation_loop import (
    _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX,
    _LENGTH_CONTINUATION_NETWORK_STUB,
    _LENGTH_CONTINUATION_OUTPUT_LIMIT,
)

_NUDGE_MARKERS = (
    _LENGTH_CONTINUATION_NETWORK_STUB,
    _LENGTH_CONTINUATION_OUTPUT_LIMIT,
    _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX,
)


# ── Scripted, recording provider ──────────────────────────────────────────

def _final_length_frame() -> bytes:
    """The frame a provider sends when it hits the output cap."""
    return _sse({
        "id": "chatcmpl-len",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test/model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
    })


def _completion_json(content=None, tool_calls=None, finish_reason="stop") -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-ok",
        "object": "chat.completion",
        "created": 1,
        "model": "test/model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def stream_drop(frames):
    """Send `frames` as real chunks, then hang up with no 0-length chunk."""
    return {"kind": "stream_drop", "frames": frames}


def json_ok(**kwargs):
    return {"kind": "json", "payload": _completion_json(**kwargs)}


def stream_tool_call():
    """A complete stream whose only output is one finished tool call."""
    return {"kind": "stream_complete", "frames": [
        _delta_frame({"role": "assistant", "content": None, "tool_calls": [{
            "index": 0, "id": "t1", "type": "function",
            "function": {"name": "write_file", "arguments": ""},
        }]}),
        _delta_frame({"tool_calls": [{
            "index": 0,
            "function": {
                "arguments": '{"path":"report.md","content":"v1"}',
            },
        }]}),
        _sse({
            "id": "chatcmpl-tc", "object": "chat.completion.chunk",
            "created": 1, "model": "test/model",
            "choices": [{
                "index": 0, "delta": {}, "finish_reason": "tool_calls",
            }],
        }),
    ]}


def stream_text(text: str):
    """A complete stream that delivers `text` and stops normally."""
    return {"kind": "stream_complete", "frames": [
        _delta_frame({"role": "assistant", "content": text}),
        _sse({
            "id": "chatcmpl-t", "object": "chat.completion.chunk",
            "created": 1, "model": "test/model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
    ]}


def hangup():
    """Accept, read the request, then close without sending anything."""
    return {"kind": "hangup"}


class RecordedRequest:
    """What a request looked like — shapes and counts only, never content.

    Deliberately records no message text: the point is to distinguish a
    continuation from an original and streaming from non-streaming without
    ever putting prompts or credentials into test output.
    """

    def __init__(self, body: dict):
        self.stream = bool(body.get("stream"))
        messages = body.get("messages") or []
        self.n_messages = len(messages)
        self.nudges = sum(
            1
            for m in messages
            if m.get("role") == "user"
            and any(marker in (m.get("content") or "") for marker in _NUDGE_MARKERS)
        )
        self.max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")

    @property
    def mode(self) -> str:
        return "STREAM" if self.stream else "NONSTREAM"


class ScriptedProviderServer:
    """Replays one scripted behaviour per request and records what arrived.

    Requests beyond the script are recorded as ``OVERRUN`` and hung up on, so
    a runaway retry loop fails an assertion instead of hanging the suite.
    """

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self.requests: list[RecordedRequest] = []
        self.overruns = 0
        self._lock = threading.Lock()
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

    @property
    def modes(self) -> list[str]:
        return [r.mode for r in self.requests]

    @property
    def total_nudges(self) -> int:
        """Nudges visible on the wire, counted from the LAST request.

        Each request carries the whole transcript, so summing across requests
        would multiply-count one nudge. The final request is what the model
        actually had to answer.
        """
        return self.requests[-1].nudges if self.requests else 0

    def _read_request(self, conn):
        """Return ``(path, body)`` for one request, or ``(None, {})``."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            got = conn.recv(65536)
            if not got:
                return None, {}
            buf += got
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        path = ""
        if lines and b" " in lines[0]:
            path = lines[0].split(b" ")[1].decode(errors="replace")
        length = 0
        for line in lines:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        while len(rest) < length:
            got = conn.recv(65536)
            if not got:
                break
            rest += got
        try:
            return path, json.loads(rest.decode() or "{}")
        except ValueError:
            return path, {}

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(5)
                path, body = self._read_request(conn)
                # Only MODEL requests are scripted and counted.  Hermes also
                # probes /v1/models for capability detection; letting those
                # consume script slots would make every sequence assertion
                # measure the probe instead of the turn.
                if path is not None and "/chat/completions" not in path:
                    canned = json.dumps({"object": "list", "data": []}).encode()
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(canned)).encode() + b"\r\n"
                        b"\r\n" + canned
                    )
                    continue
                with self._lock:
                    index = len(self.requests)
                    self.requests.append(RecordedRequest(body))
                    step = (
                        self._script[index] if index < len(self._script) else None
                    )
                    if step is None:
                        self.overruns += 1
                if step is None:
                    continue  # hang up; an overrun is an assertion failure
                if step["kind"] == "hangup":
                    continue
                if step["kind"] == "stream_complete":
                    # A COMPLETE chunked stream terminated properly: the
                    # frames, then [DONE], then the 0-length chunk.
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Cache-Control: no-cache\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"\r\n"
                    )
                    for frame in step["frames"]:
                        conn.sendall(_chunk(frame))
                    conn.sendall(_chunk(b"data: [DONE]\n\n"))
                    conn.sendall(b"0\r\n\r\n")
                    continue
                if step["kind"] == "json_stream_complete":
                    # A COMPLETE chunked stream: frames, a final
                    # finish_reason=length frame, [DONE], and the 0-length
                    # terminator.  This is genuine output truncation, NOT a
                    # transport fault — the distinction under test.
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Cache-Control: no-cache\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"\r\n"
                    )
                    for frame in step["frames"]:
                        conn.sendall(_chunk(frame))
                    conn.sendall(_chunk(_final_length_frame()))
                    conn.sendall(_chunk(b"data: [DONE]\n\n"))
                    conn.sendall(b"0\r\n\r\n")
                    continue
                if step["kind"] == "json":
                    payload = json.dumps(step["payload"]).encode()
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                        b"\r\n" + payload
                    )
                    continue
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Cache-Control: no-cache\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                for frame in step["frames"]:
                    conn.sendall(_chunk(frame))
            except OSError:
                pass
            finally:
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
def provider():
    servers: list[ScriptedProviderServer] = []

    def _make(script):
        s = ScriptedProviderServer(script)
        servers.append(s)
        return s

    yield _make
    for s in servers:
        s.close()


@pytest.fixture()
def e2e_agent():
    """A real ``AIAgent`` whose every model request goes over a real socket.

    Both transports are pointed at the scripted server: ``client`` for the
    non-streaming path and ``_create_request_openai_client`` (a per-REQUEST
    factory — the streaming layer closes each client it is handed) for the
    streaming one. Patching only one of them would let a "NONSTREAM" recovery
    quietly bypass the server and make the mode assertions meaningless.
    """
    from run_agent import AIAgent

    def _build(server):
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url=server.base_url,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        agent.base_url = server.base_url
        agent.client = _direct_client(server.base_url)
        agent._create_request_openai_client = (
            lambda *a, **k: _direct_client(server.base_url)
        )
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._api_max_retries = 3
        # A registered stream consumer is what makes the loop prefer the
        # streaming path; without one the STREAM assertions are vacuous.
        agent.streamed = []
        agent.stream_delta_callback = agent.streamed.append
        return agent

    return _build


def _run(agent, message):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message)


def _stale_nudges(messages) -> list[str]:
    return [
        m.get("content")
        for m in messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and any(marker in (m.get("content") or "") for marker in _NUDGE_MARKERS)
    ]


def _scaffold_markers(messages) -> list[str]:
    return [
        k
        for m in messages
        if isinstance(m, dict)
        for k in ("_length_continuation_fragment", "_length_continuation_nudge")
        if m.get(k)
    ]


# ── A. Drop before any useful output ──────────────────────────────────────

class TestNoTokenDrop:
    def test_one_stream_then_one_nonstream_and_nothing_more(
        self, provider, e2e_agent,
    ):
        """The headline P1 contract, measured at the socket.

        Before this phase a no-delta drop was retried by the streaming layer
        (STREAM ×3) and then handed to the generic retry budget, so the
        provider saw far more than two requests for one incident.
        """
        server = provider([
            stream_drop([]),
            json_ok(content="Here is the answer."),
        ])
        agent = e2e_agent(server)
        result = _run(agent, "explain transformers")

        assert server.modes == ["STREAM", "NONSTREAM"], (
            f"one drop must produce exactly one strategy change; "
            f"got {server.modes}"
        )
        assert len(server.requests) == 2
        assert server.overruns == 0, "a third provider request means the loop looped"
        assert server.total_nudges == 0, (
            "a transport drop is not output truncation — it must never be "
            "answered with a synthetic 'continue' prompt"
        )
        assert result["completed"] is True
        assert result["final_response"] == "Here is the answer."


# ── B. Drop mid tool-call, nothing visible ────────────────────────────────

class TestMidToolCallDrop:
    def test_incomplete_tool_call_never_executes_and_budget_is_unchanged(
        self, provider, e2e_agent,
    ):
        server = provider([
            stream_drop([
                _delta_frame({"role": "assistant", "content": None, "tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "write_file", "arguments": ""},
                }]}),
                _delta_frame({"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": '{"path":"a.md","content":"parti'},
                }]}),
            ]),
            json_ok(content="Recovered without writing anything."),
        ])
        agent = e2e_agent(server)
        agent.valid_tool_names.add("write_file")

        with patch(
            "run_agent.handle_function_call", return_value='{"success":true}',
        ) as hfc:
            result = _run(agent, "write the file")

        assert hfc.call_count == 0, (
            "arguments that never finished arriving must never reach the "
            "tool dispatcher"
        )
        assert server.modes == ["STREAM", "NONSTREAM"]
        assert server.overruns == 0
        assert server.total_nudges == 0
        # A dropped connection is not an output cap: escalating max_tokens
        # here is how a network fault used to masquerade as truncation.
        assert server.requests[1].max_tokens == server.requests[0].max_tokens
        assert result["completed"] is True


# ── C. Drop after visible model text ──────────────────────────────────────

class TestVisiblePartialDrop:
    def test_one_continuation_nonstreaming_without_duplicating_text(
        self, provider, e2e_agent,
    ):
        server = provider([
            stream_drop([
                _delta_frame({"role": "assistant", "content": "The first half "}),
                _delta_frame({"content": "of the answer"}),
            ]),
            json_ok(content=" and the rest."),
        ])
        agent = e2e_agent(server)
        result = _run(agent, "explain transformers")

        assert server.modes == ["STREAM", "NONSTREAM"], (
            "the continuation must not re-enter the transport that just died"
        )
        assert server.overruns == 0
        assert server.total_nudges == 1, (
            f"exactly one continuation nudge; got {server.total_nudges}"
        )
        final = result.get("final_response") or ""
        assert final.count("The first half") <= 1, (
            f"visible partial text must not be duplicated: {final!r}"
        )


# ── D. Second failure is raw, not a stub ──────────────────────────────────

class TestRawSecondFailure:
    def test_no_third_request_and_no_stale_scaffold(self, provider, e2e_agent):
        """The boundary the first P1 round missed.

        The recovery request fails at the transport layer with a RAW
        exception (no response body at all). That must terminate the bounded
        incident, not fall through to ``retry_count`` /
        ``_try_recover_primary_transport``, which would reset the budget and
        replay the whole cycle.
        """
        server = provider([
            stream_drop([
                _delta_frame({"role": "assistant", "content": "Partial text "}),
            ]),
            hangup(),
        ])
        agent = e2e_agent(server)
        result = _run(agent, "explain transformers")

        assert server.modes == ["STREAM", "NONSTREAM"]
        assert len(server.requests) == 2, (
            f"a spent transport incident must not issue a third request; "
            f"got {server.modes}"
        )
        assert server.overruns == 0
        assert result.get("completed") is not True
        messages = result["messages"]
        assert _stale_nudges(messages) == [], (
            "a synthetic 'continue' row must never survive into the durable "
            "transcript — it would steer the next turn back into a dead "
            "response"
        )
        assert _scaffold_markers(messages) == []
        roles = [m.get("role") for m in messages if isinstance(m, dict)]
        assert "assistant" in roles


# ── E. Raw second failure with a fallback configured ──────────────────────

class TestRawSecondFailureWithFallback:
    def test_single_fallback_activation_and_clean_scaffold(
        self, provider, e2e_agent,
    ):
        server = provider([
            stream_drop([
                _delta_frame({"role": "assistant", "content": "Partial text "}),
            ]),
            hangup(),
        ])
        agent = e2e_agent(server)

        activations = {"n": 0}
        real_has_pending = agent._has_pending_fallback

        def _activate():
            activations["n"] += 1
            return False  # no real second provider to switch to

        agent._try_activate_fallback = _activate
        agent._has_pending_fallback = lambda: True

        result = _run(agent, "explain transformers")

        assert activations["n"] == 1, (
            f"a spent incident must attempt fallback exactly once; "
            f"got {activations['n']}"
        )
        assert server.modes == ["STREAM", "NONSTREAM"]
        assert server.overruns == 0
        assert _stale_nudges(result["messages"]) == []
        assert _scaffold_markers(result["messages"]) == []
        assert real_has_pending is not None


# ── Mutation gates that need an explicit probe ────────────────────────────

class TestOwnedIncidentBypassesGenericMachinery:
    """A drop this policy owns must never also reach the generic recovery.

    ``_try_recover_primary_transport`` rebuilds the client and resets
    ``retry_count`` to 0, handing the same incident a fresh budget. That is
    precisely the multiplication P1 removes, so it must not run at all here.
    """

    def test_primary_transport_recovery_is_never_invoked(
        self, provider, e2e_agent,
    ):
        server = provider([
            stream_drop([]),
            json_ok(content="Recovered."),
        ])
        agent = e2e_agent(server)

        with patch.object(
            agent, "_try_recover_primary_transport", return_value=True,
        ) as recover:
            result = _run(agent, "explain transformers")

        assert recover.call_count == 0, (
            "the bounded policy owns this incident; the generic transport "
            "reset must not also fire on it"
        )
        assert server.modes == ["STREAM", "NONSTREAM"]
        assert result["completed"] is True

    def test_second_raw_failure_does_not_reset_the_budget(
        self, provider, e2e_agent,
    ):
        server = provider([stream_drop([]), hangup()])
        agent = e2e_agent(server)

        with patch.object(
            agent, "_try_recover_primary_transport", return_value=True,
        ) as recover:
            _run(agent, "explain transformers")

        assert recover.call_count == 0
        assert len(server.requests) == 2, (
            f"a spent incident must stop, not restart a retry cycle; "
            f"got {server.modes}"
        )


class TestSideEffectAtMostOnceOverRealSockets:
    def test_completed_tool_is_not_replayed_when_the_next_stream_drops(
        self, provider, e2e_agent,
    ):
        """The expensive failure mode: recovery re-running a real action.

        Request 1 returns a COMPLETE tool call, which executes. Request 2
        (the follow-up model turn) drops mid-stream. Recovery replays the
        MODEL request only — the tool must not run a second time.
        """
        server = provider([
            stream_tool_call(),
            stream_drop([]),
            json_ok(content="Wrote it once."),
        ])
        agent = e2e_agent(server)
        agent.valid_tool_names.add("write_file")

        with patch(
            "run_agent.handle_function_call", return_value='{"success":true}',
        ) as hfc:
            result = _run(agent, "write the report")

        assert hfc.call_count == 1, (
            f"a side-effecting tool must execute at most once per completed "
            f"tool call; executions={hfc.call_count}, modes={server.modes}"
        )
        assert server.overruns == 0
        assert result["final_response"] == "Wrote it once."


class TestGenuineLengthIsUntouched:
    """Separating the two failures must not disturb the real-truncation path."""

    def test_real_finish_reason_length_still_continues_on_streaming(
        self, provider, e2e_agent,
    ):
        server = provider([
            # A COMPLETE stream (0-length terminator sent) that ends with
            # finish_reason=length — genuine output-cap truncation.
            {"kind": "json_stream_complete", "frames": [
                _delta_frame({"role": "assistant", "content": "First part"}),
            ]},
            json_ok(content=" and the rest."),
        ])
        agent = e2e_agent(server)
        result = _run(agent, "write a long answer")

        assert server.total_nudges == 1, (
            "genuine length exhaustion still earns its continuation nudge"
        )
        assert server.requests[0].mode == "STREAM"
        assert result is not None


class TestInterruptBeatsRecovery:
    """A user cancel must never be answered with another model request.

    An interrupt that closes the socket surfaces as a transport error rather
    than ``InterruptedError``, so without an explicit guard the bounded policy
    would helpfully "recover" from the user's own stop.
    """

    def test_interrupt_during_stream_issues_no_recovery_request(
        self, provider, e2e_agent,
    ):
        server = provider([stream_drop([]), json_ok(content="should not be sent")])
        agent = e2e_agent(server)

        original = agent._create_request_openai_client

        def _interrupt_then_build(*a, **k):
            # Set the flag the way /stop does, before the drop is raised.
            agent._interrupt_requested = True
            return original(*a, **k)

        agent._create_request_openai_client = _interrupt_then_build
        _run(agent, "explain transformers")

        assert len(server.requests) == 1, (
            f"after an interrupt the agent must not send a recovery request; "
            f"got {server.modes}"
        )
        assert server.overruns == 0

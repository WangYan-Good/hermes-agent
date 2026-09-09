"""P1 — transport-interruption recovery converges to ONE bounded attempt.

A streaming model request that dies mid-body (peer closed connection /
incomplete chunked read / SSE stopped before its terminator) used to be
swallowed into a ``finish_reason=length`` stub and handed the genuine
output-truncation budget: up to 4 continuation nudges, or 4 truncated
tool-call retries with a doubling ``max_tokens``. Each of those is a fresh
request that can drop again, so ONE dropped connection multiplied into a
long chain of requests, "continue" prompts, and repeated planning.

These tests pin the replacement contract end-to-end through
``run_conversation``:

    STREAM → (drop) → NONSTREAM → success | fallback | honest terminal

They assert the *shape of the recovery*, not just the final string: how many
model requests went out, in which streaming mode, how many continuation
nudges were appended, and how many times a tool actually executed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID
from agent.transport_recovery import (
    TRANSPORT_INTERRUPTED_ATTR,
    TRANSPORT_VISIBLE_TEXT_ATTR,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────

@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.compression_enabled = False
        a.save_trajectories = False
        # A registered stream consumer is what makes the loop prefer the
        # streaming path; without one it would pick non-streaming for its own
        # reasons and the STREAM→NONSTREAM assertions would be vacuous.
        a.stream_delta_callback = lambda *_args, **_kw: None
        return a


class ApiRecorder:
    """Records the mode of every model request and replays scripted responses.

    Patching at ``_interruptible_streaming_api_call`` /
    ``_interruptible_api_call`` is what makes the streaming *mode* observable:
    the loop chooses between them, and that choice is the thing P1 bounds.
    """

    def __init__(self, agent, responses):
        self.modes: list[str] = []
        self._queue = list(responses)
        self._agent = agent
        agent._interruptible_streaming_api_call = self._stream
        agent._interruptible_api_call = self._nonstream

    def _next(self):
        if not self._queue:
            raise AssertionError(
                f"loop asked for more model requests than scripted; "
                f"modes so far={self.modes}"
            )
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def _stream(self, api_kwargs, on_first_delta=None, **_kwargs):
        self.modes.append("STREAM")
        return self._next()

    def _nonstream(self, api_kwargs):
        self.modes.append("NONSTREAM")
        return self._next()

    @property
    def call_count(self) -> int:
        return len(self.modes)


def _drop(content=None, dropped_tools=None, visible=None, tool_calls=None):
    """A transport-interrupted stub, exactly as the streaming layer builds it.

    ``visible`` defaults to "the stub carries model text"; pass it explicitly
    to model the case where the only content is Hermes' own mid-tool-call
    warning (which the user saw, but the model did not write).
    """
    from tests.run_agent.test_run_agent import _mock_assistant_msg

    if visible is None:
        visible = bool((content or "").strip())
    return SimpleNamespace(
        id=PARTIAL_STREAM_STUB_ID,
        model="test/model",
        choices=[SimpleNamespace(
            index=0,
            message=_mock_assistant_msg(content=content, tool_calls=tool_calls),
            finish_reason=FINISH_REASON_LENGTH,
        )],
        usage=None,
        _dropped_tool_names=dropped_tools,
        **{
            TRANSPORT_INTERRUPTED_ATTR: True,
            TRANSPORT_VISIBLE_TEXT_ATTR: visible,
        },
    )


def _run(agent, message, history=None):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message, conversation_history=history)


def _user_nudge_texts(messages) -> list[str]:
    """Continuation prompts as they appear on the wire (markers stripped)."""
    out = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content") or ""
        if "[System:" in content and (
            "Continue exactly where you left off" in content
            or "network error mid-stream" in content
            or "too large" in content
        ):
            out.append(content)
    return out


# ── A. Drop before any output ─────────────────────────────────────────────

class TestDropBeforeAnyOutput:
    """CASE A: the connection died before a single useful token."""

    def test_exactly_one_nonstreaming_retry_and_no_nudge(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        rec = ApiRecorder(loop_agent, [
            _drop(content=""),
            _mock_response(content="Here is the answer.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "explain transformers")

        assert rec.modes == ["STREAM", "NONSTREAM"], (
            "One drop must produce exactly one strategy change — not a "
            "STREAM/STREAM/CONTINUE ladder."
        )
        assert result["completed"] is True
        assert result["final_response"] == "Here is the answer."
        assert _user_nudge_texts(result["messages"]) == [], (
            "Nothing was delivered to the user, so the original request is "
            "replayed as-is — a synthetic 'continue' prompt would ask the "
            "model to resume text it never wrote."
        )

    def test_no_output_budget_escalation_from_a_network_error(self, loop_agent):
        """Mutation gate: a dropped connection is not an output cap, so the
        max-token budget must not move."""
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent._ephemeral_max_output_tokens = None
        seen: list = []

        rec = ApiRecorder(loop_agent, [
            _drop(content=""),
            _mock_response(content="done", finish_reason="stop"),
        ])
        _orig = rec._nonstream

        def _spy(api_kwargs):
            seen.append(getattr(loop_agent, "_ephemeral_max_output_tokens", None))
            return _orig(api_kwargs)

        loop_agent._interruptible_api_call = _spy
        _run(loop_agent, "explain transformers")

        assert seen == [None], (
            f"Transport recovery must not stamp an ephemeral output cap "
            f"(saw {seen}); boosting max_tokens is the genuine-truncation "
            f"remedy and misapplying it is what made a network drop cost "
            f"like an output-cap truncation."
        )


# ── B. Drop after visible text ────────────────────────────────────────────

class TestDropAfterVisibleText:
    """CASE B: model text already reached the user, so the request cannot be
    replayed from scratch — the user would see it twice."""

    def test_exactly_one_nonstreaming_continuation(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        rec = ApiRecorder(loop_agent, [
            _drop(content="The first half of "),
            _mock_response(content="the answer is forty-two.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "ask me something")

        assert rec.modes == ["STREAM", "NONSTREAM"]
        assert len(_user_nudge_texts(result["messages"])) == 1, (
            "Exactly one continuation — the old path allowed four."
        )
        assert "first half of" in result["final_response"]
        assert "forty-two" in result["final_response"]

    def test_continuation_prompt_names_the_network_not_a_length_limit(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        ApiRecorder(loop_agent, [
            _drop(content="Partial "),
            _mock_response(content="rest.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "go")

        texts = _user_nudge_texts(result["messages"])
        assert len(texts) == 1
        assert "network error mid-stream" in texts[0]
        assert "output length limit" not in texts[0], (
            "Telling the model it hit an output cap when the socket died "
            "makes it answer 'I wasn't truncated, I'm done.'"
        )

    def test_partial_text_is_not_duplicated_in_the_final_response(self, loop_agent):
        """The checkpointed fragment must appear once, not once as a fragment
        and again inside a restarted response."""
        from tests.run_agent.test_run_agent import _mock_response

        ApiRecorder(loop_agent, [
            _drop(content="UNIQUE_MARKER_ALPHA "),
            _mock_response(content="and the ending.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "write something")

        assert result["final_response"].count("UNIQUE_MARKER_ALPHA") == 1


# ── C. Drop mid tool-call ─────────────────────────────────────────────────

class TestDropMidToolCall:
    """CASE C: arguments were still streaming when the connection died."""

    def test_incomplete_tool_call_is_not_executed(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

        loop_agent.valid_tool_names.add("write_file")
        good_tc = _mock_tool_call(
            name="write_file",
            arguments='{"path":"report.md","content":"full content"}',
            call_id="c2",
        )
        rec = ApiRecorder(loop_agent, [
            # Truncated JSON arrived on the wire; the stub must never let it run.
            _drop(
                content="",
                dropped_tools=["write_file"],
                visible=False,
                tool_calls=[_mock_tool_call(
                    name="write_file",
                    arguments='{"path":"report.md","content":"partial',
                    call_id="c1",
                )],
            ),
            _mock_response(content="", finish_reason="stop", tool_calls=[good_tc]),
            _mock_response(content="Done!", finish_reason="stop"),
        ])

        with patch("run_agent.handle_function_call", return_value='{"success":true}') as hfc:
            result = _run(loop_agent, "write the report")

        assert rec.modes == ["STREAM", "NONSTREAM", "STREAM"], (
            "The recovery request goes out non-streaming. Once it succeeds "
            "the incident is over, so the following tool iteration gets the "
            "normal streaming path back."
        )
        assert hfc.call_count == 1, (
            "The incomplete tool call must be discarded, never executed; "
            "only the complete one from the retry runs."
        )
        assert hfc.call_args_list[0].args[0] == "write_file"
        assert "partial" not in str(hfc.call_args_list[0].args[1]), (
            "A tool must never run with argument JSON that never finished "
            "arriving."
        )
        assert result["final_response"] == "Done!"

    def test_no_max_token_escalation_for_a_dropped_tool_call(self, loop_agent):
        """Mutation gate: the old path doubled max_tokens up to 4 times for a
        mid-tool-call stub. A network drop earns no extra budget."""
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent._ephemeral_max_output_tokens = None
        seen: list = []
        rec = ApiRecorder(loop_agent, [
            _drop(content="", dropped_tools=["write_file"], visible=False),
            _mock_response(content="ok", finish_reason="stop"),
        ])
        _orig = rec._nonstream

        def _spy(api_kwargs):
            seen.append(getattr(loop_agent, "_ephemeral_max_output_tokens", None))
            return _orig(api_kwargs)

        loop_agent._interruptible_api_call = _spy
        _run(loop_agent, "write the report")
        assert seen == [None]

    def test_hermes_warning_text_does_not_count_as_model_output(self, loop_agent):
        """The mid-tool-call warning Hermes appends is not model text: it must
        not push a zero-output drop into the continuation path."""
        from tests.run_agent.test_run_agent import _mock_response

        rec = ApiRecorder(loop_agent, [
            _drop(
                content="\n\n⚠ Stream stalled mid tool-call (write_file); "
                        "the action was not executed.",
                dropped_tools=["write_file"],
                visible=False,
            ),
            _mock_response(content="Recovered.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "write the report")

        assert rec.modes == ["STREAM", "NONSTREAM"]
        assert _user_nudge_texts(result["messages"]) == [], (
            "Only MODEL-authored text justifies a continuation; asking the "
            "model to 'continue' our own warning is nonsense."
        )


# ── D. Second drop after the recovery attempt ─────────────────────────────

class TestSecondDropIsTerminal:
    """Mutation gate: the recovery must not recurse. A drop that survives the
    non-streaming retry is not transient."""

    def test_two_drops_stop_instead_of_looping(self, loop_agent):
        rec = ApiRecorder(loop_agent, [_drop(content=""), _drop(content="")])
        result = _run(loop_agent, "explain transformers")

        assert rec.modes == ["STREAM", "NONSTREAM"], (
            f"Recovery must stop after one strategy change; got {rec.modes}"
        )
        assert result["completed"] is False
        assert result["partial"] is True
        assert "dropped again" in (result["error"] or "")

    def test_terminal_exit_leaves_no_continuation_scaffolding(self, loop_agent):
        rec = ApiRecorder(loop_agent, [
            _drop(content="Some visible text. "), _drop(content="more "),
        ])
        result = _run(loop_agent, "write a report")

        assert rec.modes == ["STREAM", "NONSTREAM"]
        assert _user_nudge_texts(result["messages"]) == [], (
            "An unanswered 'continue' nudge steers every later turn back "
            "into this dead response."
        )

    def test_visible_partial_is_surfaced_not_discarded(self, loop_agent):
        """The user already saw this text; returning None would make Hermes
        look like it lost work it had in hand."""
        ApiRecorder(loop_agent, [
            _drop(content="Half the answer. "), _drop(content=""),
        ])
        result = _run(loop_agent, "write a report")
        assert "Half the answer." in (result["final_response"] or "")

    def test_fallback_is_preferred_over_a_terminal_failure(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        loop_agent._fallback_chain = [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.7"},
        ]
        loop_agent._fallback_index = 0
        activations = {"n": 0}

        def _fake_activate(reason=None):
            activations["n"] += 1
            loop_agent._fallback_index = len(loop_agent._fallback_chain)
            return True

        rec = ApiRecorder(loop_agent, [
            _drop(content=""),
            _drop(content=""),
            _mock_response(content="Answered on the fallback.", finish_reason="stop"),
        ])
        with patch.object(loop_agent, "_try_activate_fallback", side_effect=_fake_activate):
            result = _run(loop_agent, "explain transformers")

        assert activations["n"] == 1
        assert result["final_response"] == "Answered on the fallback."
        assert rec.modes == ["STREAM", "NONSTREAM", "STREAM"], (
            "Once the incident is handed to a different provider it is over: "
            "the fallback gets a normal streaming turn, not a pinned "
            "non-streaming one."
        )


# ── E. Genuine output-length truncation is untouched ──────────────────────

class TestGenuineLengthPathUnchanged:
    """The whole point of P1 is that these two failures stop sharing a budget.
    A real output cap keeps its multi-attempt continuation policy."""

    def test_real_finish_reason_length_still_continues_repeatedly(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        rec = ApiRecorder(loop_agent, [
            _mock_response(content="chapter one ", finish_reason="length"),
            _mock_response(content="chapter two ", finish_reason="length"),
            _mock_response(content="chapter three.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "write a long report")

        assert rec.call_count == 3, (
            "Mutation gate: a genuine output-cap truncation must NOT be "
            "capped at the single transport-recovery attempt."
        )
        assert "chapter one" in result["final_response"]
        assert "chapter three" in result["final_response"]

    def test_genuine_length_uses_the_output_limit_prompt(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        ApiRecorder(loop_agent, [
            _mock_response(content="chapter one ", finish_reason="length"),
            _mock_response(content="the end.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "write a long report")

        texts = _user_nudge_texts(result["messages"])
        assert texts and "output length limit" in texts[0], (
            "Mutation gate: relabelling a genuine truncation as a transport "
            "drop would send the network-error prompt here."
        )

    def test_genuine_length_stays_on_the_streaming_path(self, loop_agent):
        """A real output cap says nothing about connection health, so it must
        not pin later requests to non-streaming."""
        from tests.run_agent.test_run_agent import _mock_response

        rec = ApiRecorder(loop_agent, [
            _mock_response(content="part one ", finish_reason="length"),
            _mock_response(content="part two.", finish_reason="stop"),
        ])
        _run(loop_agent, "write a long report")
        assert rec.modes == ["STREAM", "STREAM"]


# ── F. Content-filter stall keeps fallback-first semantics ────────────────

class TestContentFilterStallUnchanged:
    """A provider output-filter kill arrives as the same stub shape. It is
    content-deterministic, so it must still go straight to fallback rather
    than spend the transport budget re-hitting the filter."""

    def test_tagged_stub_still_activates_fallback_first(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        stub = _drop(content="Writing the file...", dropped_tools=["write_file"])
        stub._content_filter_terminated = True

        loop_agent._fallback_chain = [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.7"},
        ]
        loop_agent._fallback_index = 0
        activations = {"n": 0}

        def _fake_activate(reason=None):
            activations["n"] += 1
            loop_agent._fallback_index = len(loop_agent._fallback_chain)
            return True

        rec = ApiRecorder(loop_agent, [
            stub,
            _mock_response(content="Done on the fallback provider.", finish_reason="stop"),
        ])
        with patch.object(loop_agent, "_try_activate_fallback", side_effect=_fake_activate):
            result = _run(loop_agent, "write me a long file")

        assert activations["n"] == 1, (
            "Content-filter classification must be checked BEFORE transport "
            "recovery — otherwise a deterministic filter gets treated as a "
            "flaky socket and burns the recovery attempt."
        )
        assert rec.modes == ["STREAM", "STREAM"], (
            "Fallback is not a transport recovery, so nothing is pinned to "
            "the non-streaming path."
        )
        assert result["final_response"] == "Done on the fallback provider."


# ── G. Rate limiting keeps its own backoff ────────────────────────────────

class TestRateLimitSemanticsUnchanged:
    def test_429_still_retries_with_backoff(self, loop_agent, monkeypatch):
        """Transport recovery must not swallow or shortcut the 429 path."""
        import time as _time
        import run_agent as _ra
        from tests.run_agent.test_run_agent import _mock_response

        monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(_ra, "jittered_backoff", lambda *a, **k: 0.0)

        err = Exception("Rate limit exceeded")
        err.status_code = 429

        rec = ApiRecorder(loop_agent, [
            err,
            _mock_response(content="Recovered after backoff.", finish_reason="stop"),
        ])
        result = _run(loop_agent, "hello")

        assert rec.call_count == 2
        assert result["final_response"] == "Recovered after backoff."
        assert rec.modes == ["STREAM", "STREAM"], (
            "A 429 is not a transport interruption — it must not pin the "
            "retry to the non-streaming path."
        )


# ── H. Context overflow keeps compression ─────────────────────────────────

class TestContextOverflowSemanticsUnchanged:
    """An oversized payload must keep reaching the compression recovery, not
    be mistaken for a flaky socket."""

    def test_413_routes_to_the_overflow_path_not_transport_recovery(
        self, loop_agent, monkeypatch,
    ):
        import time as _time
        import run_agent as _ra

        monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(_ra, "jittered_backoff", lambda *a, **k: 0.0)
        # Compaction off makes the overflow branch terminate with a
        # recognisable message instead of running the real compressor, so the
        # routing assertion stays deterministic.
        loop_agent.compression_enabled = False

        err = Exception("Request entity too large")
        err.status_code = 413

        rec = ApiRecorder(loop_agent, [err])
        result = _run(loop_agent, "hello")

        assert rec.modes == ["STREAM"], (
            "A payload-too-large error is not a mid-stream drop; it must not "
            "consume the transport-recovery attempt or pin the request to "
            "the non-streaming path."
        )
        blob = f"{result.get('final_response')} {result.get('error')}".lower()
        assert "compact" in blob or "compress" in blob or "context" in blob, (
            f"413 must still surface through the context-overflow recovery; "
            f"got {result!r}"
        )


# ── I. A user interrupt is not a retryable failure ────────────────────────

class TestInterruptIsNotRetried:
    def test_interrupt_during_streaming_issues_no_further_request(self, loop_agent):
        """cancel != transport retry. After the user says stop, Hermes must
        not quietly re-send the model call."""
        rec = ApiRecorder(loop_agent, [])

        def _interrupting_stream(api_kwargs, on_first_delta=None, **_kwargs):
            rec.modes.append("STREAM")
            loop_agent._interrupt_requested = True
            raise InterruptedError("Agent interrupted during streaming API call")

        loop_agent._interruptible_streaming_api_call = _interrupting_stream
        result = _run(loop_agent, "explain transformers")

        assert rec.modes == ["STREAM"], (
            f"An interrupt must produce zero automatic retries; "
            f"modes={rec.modes}"
        )
        assert result.get("interrupted") is True

    def test_interrupt_after_a_transport_recovery_is_still_final(self, loop_agent):
        """The recovery attempt itself must stay cancellable."""
        rec = ApiRecorder(loop_agent, [_drop(content="")])

        def _nonstream_then_interrupt(api_kwargs):
            rec.modes.append("NONSTREAM")
            loop_agent._interrupt_requested = True
            raise InterruptedError("Agent interrupted during API call")

        loop_agent._interruptible_api_call = _nonstream_then_interrupt
        result = _run(loop_agent, "explain transformers")

        assert rec.modes == ["STREAM", "NONSTREAM"], (
            "The interrupt ends the turn; it does not start another "
            "recovery round."
        )
        assert result.get("interrupted") is True


# ── J. Synthetic continuation state does not leak into later turns ────────

class TestSyntheticContinuationCleanup:
    def test_next_user_turn_carries_no_stale_nudge_after_a_terminal_drop(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        ApiRecorder(loop_agent, [_drop(content="Half. "), _drop(content="")])
        first = _run(loop_agent, "write a report")
        assert _user_nudge_texts(first["messages"]) == []

        rec2 = ApiRecorder(loop_agent, [
            _mock_response(content="Hi there.", finish_reason="stop"),
        ])
        second = _run(loop_agent, "hi", history=first["messages"])

        assert rec2.modes == ["STREAM"], (
            "A new user turn starts a fresh incident: one request, on the "
            "normal streaming path."
        )
        assert second["completed"] is True
        sent_nudges = _user_nudge_texts(second["messages"])
        assert sent_nudges == [], (
            "A leftover 'continue' prompt makes every later turn resume a "
            "response that already died."
        )

    def test_recovery_state_does_not_pin_the_next_turn_to_nonstreaming(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response

        ApiRecorder(loop_agent, [
            _drop(content=""),
            _mock_response(content="ok", finish_reason="stop"),
        ])
        first = _run(loop_agent, "go")

        rec2 = ApiRecorder(loop_agent, [
            _mock_response(content="second turn", finish_reason="stop"),
        ])
        _run(loop_agent, "again", history=first["messages"])
        assert rec2.modes == ["STREAM"], (
            "Transport recovery is per-incident. A healthy next turn must "
            "get the streaming path (and its stale-stream health checks) back."
        )

    def test_a_later_independent_drop_gets_its_own_bounded_attempt(self, loop_agent):
        """One resolved incident must not spend the budget for a genuinely
        separate drop later in the same long tool loop."""
        from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

        loop_agent.valid_tool_names.add("write_file")
        tc = _mock_tool_call(name="write_file", arguments='{"path":"a.md"}', call_id="t1")

        rec = ApiRecorder(loop_agent, [
            _drop(content=""),                                             # incident 1
            _mock_response(content="", finish_reason="stop", tool_calls=[tc]),
            _drop(content=""),                                             # incident 2
            _mock_response(content="All done.", finish_reason="stop"),
        ])
        with patch("run_agent.handle_function_call", return_value='{"success":true}'):
            result = _run(loop_agent, "write two files")

        assert rec.modes == ["STREAM", "NONSTREAM", "STREAM", "NONSTREAM"], (
            f"Each incident gets exactly one bounded attempt, and the loop "
            f"returns to streaming in between; got {rec.modes}"
        )
        assert result["final_response"] == "All done."


# ── K. Side effects stay at-most-once ─────────────────────────────────────

class TestSideEffectAtMostOnce:
    """The real cost of an unbounded transport retry is not tokens — it is a
    tool running twice."""

    def test_a_completed_tool_is_not_replayed_by_transport_recovery(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

        loop_agent.valid_tool_names.add("write_file")
        tc = _mock_tool_call(
            name="write_file",
            arguments='{"path":"report.md","content":"v1"}',
            call_id="t1",
        )
        rec = ApiRecorder(loop_agent, [
            # The tool call arrives complete and executes once...
            _mock_response(content="", finish_reason="stop", tool_calls=[tc]),
            # ...then the NEXT request dies mid-stream. Recovery re-issues the
            # model request, which must not re-run the already-executed tool.
            _drop(content=""),
            _mock_response(content="Wrote it once.", finish_reason="stop"),
        ])
        with patch("run_agent.handle_function_call", return_value='{"success":true}') as hfc:
            result = _run(loop_agent, "write the report")

        assert hfc.call_count == 1, (
            f"A side-effecting tool must execute at most once per completed "
            f"tool call; transport recovery replays the MODEL request, never "
            f"the tool. Executions={hfc.call_count}, modes={rec.modes}"
        )
        assert rec.modes == ["STREAM", "STREAM", "NONSTREAM"]
        assert result["final_response"] == "Wrote it once."

    def test_incomplete_tool_call_contributes_zero_executions(self, loop_agent):
        rec = ApiRecorder(loop_agent, [
            _drop(content="", dropped_tools=["write_file"], visible=False),
            _drop(content="", dropped_tools=["write_file"], visible=False),
        ])
        with patch("run_agent.handle_function_call") as hfc:
            result = _run(loop_agent, "write the report")

        assert hfc.call_count == 0, (
            "A tool whose arguments never finished arriving must never run — "
            "not on the first drop, and not on the terminal one."
        )
        assert rec.modes == ["STREAM", "NONSTREAM"]
        assert result["completed"] is False


# ── L. Session replay / gateway reuse ─────────────────────────────────────

class TestSessionReplayAfterRecovery:
    """A gateway reuses the returned message list as the next turn's history.
    Nothing the recovery produced may make that replay re-run work."""

    def test_resumed_history_replays_no_tool_and_starts_clean(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response, _mock_tool_call

        loop_agent.valid_tool_names.add("write_file")
        tc = _mock_tool_call(
            name="write_file", arguments='{"path":"a.md","content":"x"}', call_id="t1",
        )
        ApiRecorder(loop_agent, [
            _mock_response(content="", finish_reason="stop", tool_calls=[tc]),
            _drop(content=""),
            _mock_response(content="Done.", finish_reason="stop"),
        ])
        with patch("run_agent.handle_function_call", return_value='{"success":true}') as hfc:
            first = _run(loop_agent, "write a file")
        assert hfc.call_count == 1

        rec2 = ApiRecorder(loop_agent, [
            _mock_response(content="Anything else?", finish_reason="stop"),
        ])
        with patch("run_agent.handle_function_call", return_value='{"success":true}') as hfc2:
            second = _run(loop_agent, "thanks", history=first["messages"])

        assert hfc2.call_count == 0, (
            "Resuming a session must not re-execute a tool that already ran."
        )
        assert rec2.modes == ["STREAM"]
        assert second["completed"] is True

    def test_replayed_history_has_no_transport_stub_artifacts(self, loop_agent):
        """The stub is an internal recovery device; it must not reach the
        provider on the next turn."""
        from tests.run_agent.test_run_agent import _mock_response

        ApiRecorder(loop_agent, [
            _drop(content="", dropped_tools=["write_file"], visible=False),
            _mock_response(content="Recovered.", finish_reason="stop"),
        ])
        first = _run(loop_agent, "write a file")

        for m in first["messages"]:
            assert not isinstance(m, dict) or m.get("id") != PARTIAL_STREAM_STUB_ID
            if isinstance(m, dict) and m.get("role") == "assistant":
                assert m.get("content") or m.get("tool_calls"), (
                    "An empty assistant row poisons replay on strict "
                    "providers (Moonshot/Kimi reject it with HTTP 400)."
                )

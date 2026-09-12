"""Turn-scoped continuation output and pending-answer ownership.

Transport incidents and genuine output caps have different owners/budgets.
This object holds their shared output fragments without conflating policies.
Agent-backed counters read by other owners remain on the agent.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TextCompletion:
    final_response: str
    exit_reason: str


@dataclass
class TurnContinuation:
    length_retries: int = 0
    truncated_tool_retries: int = 0
    codex_ack_retries: int = 0
    parts: list[str] = field(default_factory=list)
    pending_answer: str | None = None
    pending_previewed: bool = False

    def hold_answer(self, agent, answer):
        self.pending_answer = answer
        self.pending_previewed = agent._interim_content_was_streamed(answer or "")

    @staticmethod
    def output_cap(agent, api_kwargs, retries: int):
        base = agent.max_tokens if agent.max_tokens else 4096
        boost = base * (2 ** retries)
        requested = agent._requested_output_cap_from_api_kwargs(api_kwargs)
        if requested is not None:
            boost = max(boost, requested)
        return min(boost, max(32768, requested or 0))



def handle_text_response(agent, *, _ctx, _continuation, _turn_controller, assistant_message, finish_reason, response, api_messages, api_call_count):
    """Resolve a no-tool response into continuation or an existing terminal.

    Returned TurnTransitions have already been committed. Candidate writes
    retain their best-effort contract; they are not tool-dispatch fences.
    """
    from agent import conversation_loop as _loop

    final_response = assistant_message.content or ""

    # Fix: unmute output when entering the no-tool-call branch
    # so the user can see empty-response warnings and recovery
    # status messages.  _mute_post_response was set during a
    # prior housekeeping tool turn and should not silence the
    # final response path.
    agent._mute_post_response = False

    # Check if response only has think block with no actual content after it
    if not agent._has_content_after_think_block(final_response):
        # ── Partial stream recovery ─────────────────────
        # If content was already streamed to the user before
        # the connection died, use it as the final response
        # instead of falling through to prior-turn fallback
        # or wasting API calls on retries.
        _partial_streamed = (
            getattr(agent, "_current_streamed_assistant_text", "") or ""
        )
        if agent._has_content_after_think_block(_partial_streamed):
            _turn_exit_reason = "partial_stream_recovery"
            _recovered = agent._strip_think_blocks(_partial_streamed).strip()
            _loop.logger.info(
                "Partial stream content delivered (%d chars) "
                "— using as final response",
                len(_recovered),
            )
            agent._emit_status(
                "↻ Stream interrupted — using delivered content "
                "as final response"
            )
            final_response = _recovered
            # Streaming delivered a fragment, not a confirmed
            # final preview. Leave response_previewed false so
            # gateway fallback delivery can send the recovered
            # text plus the abnormal-turn explanation.
            agent._response_was_previewed = False
            _turn_controller.move(_loop.TransitionKind.FINALIZE, _loop.TurnReason.PARTIAL_RESPONSE)
            return TextCompletion(final_response, _turn_exit_reason)

        # If the previous turn already delivered real content alongside
        # HOUSEKEEPING tool calls (e.g. "You're welcome!" + memory save),
        # the model has nothing more to say. Use the earlier content
        # immediately instead of wasting API calls on retries.
        # NOTE: Only use this shortcut when ALL tools in that turn were
        # housekeeping (memory, todo, etc.).  When substantive tools
        # were called (terminal, search_files, etc.), the content was
        # likely mid-task narration ("I'll scan the directory...") and
        # the empty follow-up means the model choked — let the
        # post-tool nudge below handle that instead of exiting early.
        fallback = getattr(agent, '_last_content_with_tools', None)
        if fallback and getattr(agent, '_last_content_tools_all_housekeeping', False):
            _turn_exit_reason = "fallback_prior_turn_content"
            _loop.logger.info("Empty follow-up after tool calls — using prior turn content as final response")
            agent._emit_status("↻ Empty response after tool calls — using earlier content as final answer")
            agent._last_content_with_tools = None
            agent._last_content_tools_all_housekeeping = False
            agent._empty_content_retries = 0
            # Do NOT modify the assistant message content — the
            # old code injected "Calling the X tools..." which
            # poisoned the conversation history.  Just use the
            # fallback text as the final response and break.
            final_response = agent._strip_think_blocks(fallback).strip()
            agent._response_was_previewed = True
            _turn_controller.move(_loop.TransitionKind.FINALIZE, _loop.TurnReason.FINAL_RESPONSE)
            return TextCompletion(final_response, _turn_exit_reason)

        # ── Post-tool-call empty response nudge ───────────
        # The model returned empty after executing tool calls.
        # This covers two cases:
        #  (a) No prior-turn content at all — model went silent
        #  (b) Prior turn had content + SUBSTANTIVE tools (the
        #      fallback above was skipped because the content
        #      was mid-task narration, not a final answer)
        # Instead of giving up, nudge the model to continue by
        # appending a user-level hint.  This is the #9400 case:
        # weaker models (mimo-v2-pro, GLM-5, etc.) sometimes
        # return empty after tool results instead of continuing
        # to the next step.  One retry with a nudge usually
        # fixes it.
        _prior_was_tool = any(
            m.get("role") == "tool"
            for m in _ctx.messages[-5:]  # check recent messages
        )
        # Detect Qwen3/Ollama-style in-content thinking blocks.
        # Ollama puts <think> in the content field (not in
        # reasoning_content), so _has_structured below would
        # miss it.  We check here so thinking-only responses
        # after tool calls route to prefill instead of nudge.
        _has_inline_thinking = bool(
            _loop.re.search(
                r'<think>|<thinking>|<reasoning>',
                final_response or "",
                _loop.re.IGNORECASE,
            )
        )
        if (
            _prior_was_tool
            and not getattr(agent, "_post_tool_empty_retried", False)
            and not _has_inline_thinking  # thinking model still working — let prefill handle
        ):
            agent._post_tool_empty_retried = True
            # Clear stale narration so it doesn't resurface
            # on a later empty response after the nudge.
            agent._last_content_with_tools = None
            agent._last_content_tools_all_housekeeping = False
            _loop.logger.info(
                "Empty response after tool calls — nudging model "
                "to continue processing"
            )
            agent._buffer_status(
                "⚠️ Model returned empty after tool calls — "
                "nudging to continue"
            )
            # Append the empty assistant message first so the
            # message sequence stays valid:
            #   tool(result) → assistant("(empty)") → user(nudge)
            # Without this, we'd have tool → user which most
            # APIs reject as an invalid sequence.
            _nudge_msg = agent._build_assistant_message(assistant_message, finish_reason)
            _nudge_msg["content"] = "(empty)"
            _nudge_msg["_empty_recovery_synthetic"] = True
            _loop.append_message(_ctx.messages, _nudge_msg)
            _loop.append_message(_ctx.messages, {
                "role": "user",
                "content": _loop._EMPTY_TOOL_RESPONSE_NUDGE,
                "_empty_recovery_synthetic": True,
            })
            return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.POST_TOOL_EMPTY)

        # ── Thinking-only prefill continuation ──────────
        # The model produced structured reasoning (via API
        # fields) but no visible text content.  Rather than
        # giving up, append the assistant message as-is and
        # continue — the model will see its own reasoning
        # on the next turn and produce the text portion.
        # Inspired by clawdbot's "incomplete-text" recovery.
        # Also covers Qwen3/Ollama in-content <think> blocks
        # (detected above as _has_inline_thinking).
        _has_structured = bool(
            getattr(assistant_message, "reasoning", None)
            or getattr(assistant_message, "reasoning_content", None)
            or getattr(assistant_message, "reasoning_details", None)
            or _has_inline_thinking
        )
        if _has_structured and agent._thinking_prefill_retries < 2:
            agent._thinking_prefill_retries += 1
            _loop.logger.info(
                "Thinking-only response (no visible content) — "
                "prefilling to continue (%d/2)",
                agent._thinking_prefill_retries,
            )
            agent._buffer_status(
                f"↻ Thinking-only response — prefilling to continue "
                f"({agent._thinking_prefill_retries}/2)"
            )
            interim_msg = agent._build_assistant_message(
                assistant_message, "incomplete"
            )
            interim_msg["_thinking_prefill"] = True
            _loop.append_message(_ctx.messages, interim_msg)
            agent._session_messages = _ctx.messages
            return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.THINKING_PREFILL)

        # ── Empty response retry ──────────────────────
        # Model returned nothing usable.  Retry up to 3
        # times before attempting fallback.  This covers
        # both truly empty responses (no content, no
        # reasoning) AND reasoning-only responses after
        # prefill exhaustion — models like mimo-v2-pro
        # always populate reasoning fields via OpenRouter,
        # so the old `not _has_structured` guard blocked
        # retries for every reasoning model after prefill.
        _truly_empty = not agent._strip_think_blocks(
            final_response
        ).strip()
        _prefill_exhausted = (
            _has_structured
            and agent._thinking_prefill_retries >= 2
        )
        _empty_candidate = _truly_empty and (
            not _has_structured or _prefill_exhausted
        )
        if _empty_candidate:
            # NS-503: every empty attempt re-sends the full
            # conversation input at full price. Record the
            # attempt (usage/finish_reason signature) so
            # deterministic empties — e.g. unsignaled
            # provider refusals with zero output tokens —
            # stop burning paid retries reproducing the
            # same empty. Fails open: missing usage or
            # any generated tokens keep the full budget.
            _loop._empty_guard.record_empty_attempt(
                agent,
                finish_reason=finish_reason,
                response=response,
            )
        _empty_retry_budget = (
            _loop._empty_guard.empty_retry_budget(agent, response)
            if _empty_candidate
            else _loop._empty_guard.DEFAULT_EMPTY_RETRY_BUDGET
        )
        _deterministic_empty = _empty_candidate and (
            _loop._empty_guard.deterministic_empty(agent)
        )
        if (
            _empty_candidate
            and agent._empty_content_retries < _empty_retry_budget
            and not _deterministic_empty
        ):
            agent._empty_content_retries += 1
            wait_time = _loop.jittered_backoff(
                agent._empty_content_retries,
                base_delay=5.0,
                max_delay=60.0,
            )
            _loop.logger.warning(
                "Empty response (no content or reasoning) — "
                "retry %d/%d in %.1fs (model=%s)",
                agent._empty_content_retries,
                _empty_retry_budget, wait_time, agent.model,
            )
            _budget_note = (
                " — high-cost request, reduced retry budget"
                if _empty_retry_budget < _loop._empty_guard.DEFAULT_EMPTY_RETRY_BUDGET
                else ""
            )
            agent._buffer_status(
                f"⚠️ Empty response from model — retrying "
                f"({agent._empty_content_retries}/{_empty_retry_budget}) "
                f"in {wait_time:.0f}s{_budget_note}"
            )
            # Sleep in small increments to stay responsive to interrupts
            sleep_end = _loop.time.time() + wait_time
            _backoff_touch_counter = 0
            while _loop.time.time() < sleep_end:
                if agent._interrupt_requested:
                    agent._vprint(f"{agent.log_prefix}⚡ Interrupt detected during empty-response retry wait, aborting.", force=True)
                    _interrupt_text = (
                        f"Operation interrupted: retrying empty response from model "
                        f"(retry {agent._empty_content_retries}/{_empty_retry_budget})."
                    )
                    _loop.close_interrupted_tool_sequence(_ctx.messages, _interrupt_text)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    agent.clear_interrupt()
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _interrupt_text,
                        "messages": _ctx.messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "interrupted": True,
                    })
                _loop.time.sleep(0.2)
                _backoff_touch_counter += 1
                if _backoff_touch_counter % 150 == 0:  # 150 × 0.2s = 30s
                    agent._touch_activity(
                        f"empty response retry backoff ({agent._empty_content_retries}/{_empty_retry_budget}), "
                        f"{int(sleep_end - _loop.time.time())}s remaining"
                    )
            return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.EMPTY_RESPONSE)

        if _truly_empty and _deterministic_empty:
            _loop.logger.warning(
                "Deterministic empty response detected "
                "(consecutive zero-output completions, "
                "model=%s provider=%s finish_reason=%s) — "
                "skipping remaining retries",
                agent.model, agent.provider, finish_reason,
            )
            agent._buffer_status(
                "⚠️ Model is deterministically returning empty "
                "(zero output tokens) — skipping further retries "
                "to avoid repeat charges"
            )

        # ── Exhausted retries — try fallback provider ──
        # Before giving up with "(empty)", attempt to
        # switch to the next provider in the fallback
        # chain.  This covers the case where a model
        # (e.g. GLM-4.5-Air) consistently returns empty
        # due to context degradation or provider issues.
        if _truly_empty and agent._fallback_chain:
            _loop.logger.warning(
                "Empty response after %d retries — "
                "attempting fallback (model=%s, provider=%s)",
                agent._empty_content_retries, agent.model,
                agent.provider,
            )
            agent._buffer_status(
                "⚠️ Model returning empty responses — "
                "switching to fallback provider..."
            )
            if agent._try_activate_fallback():
                _ctx.active_system_prompt = _loop._sync_failover_system_message(
                    agent, api_messages, _ctx.active_system_prompt)
                agent._empty_content_retries = 0
                agent._buffer_status(
                    f"↻ Switched to fallback: {agent.model} "
                    f"({agent.provider})"
                )
                _loop.logger.info(
                    "Fallback activated after empty responses: "
                    "now using %s on %s",
                    agent.model, agent.provider,
                )
                # This site sits directly in the OUTER iteration
                # loop (not the retry loop), so `continue` already
                # restarts the iteration and re-runs the pre-API
                # preflight against the fallback's context window
                # (#84733). A `break` here would exit the outer
                # loop and end the turn without ever calling the
                # fallback. Clear the preflight block so the
                # re-run isn't skipped.
                _ctx.preflight_compression_blocked = False
                return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.PROVIDER_SWITCH)

        # Exhausted retries and fallback chain (or no
        # fallback configured).  Fall through to the
        # "(empty)" terminal.
        # Surface the buffered retry/fallback trace so the
        # user can see what was attempted before "(empty)".
        # NS-503: if we know roughly what the empty streak
        # cost (each attempt re-billed the full input), say
        # so — an unexplained charge for "no answer" is the
        # core of the complaint.
        _streak_cost = _loop._empty_guard.streak_cost_usd(agent)
        if _streak_cost is not None:
            agent._buffer_status(
                f"ℹ️ Estimated cost of these empty attempts: "
                f"~${_streak_cost:.2f} (input tokens are billed "
                f"per attempt even when no answer is produced)"
            )
        agent._flush_status_buffer()
        _turn_exit_reason = "empty_response_exhausted"
        reasoning_text = agent._extract_reasoning(assistant_message)
        agent._drop_trailing_empty_response_scaffolding(_ctx.messages)
        assistant_msg = agent._build_assistant_message(assistant_message, finish_reason)
        assistant_msg["content"] = "(empty)"
        # This is a user-facing failure sentinel for the gateway,
        # not real assistant content. Persisting it makes later
        # "continue" turns replay assistant("(empty)") as if it
        # were a meaningful model response, which can keep long
        # tool-heavy sessions stuck in empty-response loops.
        assistant_msg["_empty_terminal_sentinel"] = True
        _loop.append_message(_ctx.messages, assistant_msg)

        if reasoning_text:
            reasoning_preview = reasoning_text[:500] + "..." if len(reasoning_text) > 500 else reasoning_text
            _loop.logger.warning(
                "Reasoning-only response (no visible content) "
                "after exhausting retries and fallback. "
                "Reasoning: %s", reasoning_preview,
            )
            agent._emit_status(
                "⚠️ Model produced reasoning but no visible "
                "response after all retries. Returning empty."
            )
        else:
            _loop.logger.warning(
                "Empty response (no content or reasoning) "
                "after %d retries. No fallback available. "
                "model=%s provider=%s",
                agent._empty_content_retries, agent.model,
                agent.provider,
            )
            agent._emit_status(
                "❌ Model returned no content after all retries"
                + (" and fallback attempts." if agent._fallback_chain else
                   ". No fallback providers configured.")
            )

        # Deliver a labeled reasoning excerpt instead of a bare
        # "(empty)" when the model DID think but never produced
        # visible text. This is delivery-only: the persisted
        # assistant message above keeps the "(empty)" sentinel
        # (its replay semantics prevent empty-response loops),
        # and raw chain-of-thought is never promoted to a normal
        # answer earlier in the ladder — prefill continuation,
        # empty-content retries, and provider fallback all run
        # first. Only at this terminal, where the alternative is
        # returning nothing, is showing the model's own reasoning
        # (clearly labeled as such) strictly more useful.
        # Idea credit: PR #48795 (@ligl0325).
        if reasoning_text:
            final_response = (
                "⚠️ The model produced only internal reasoning and "
                "no final answer, despite retries"
                + (" and fallback" if agent._fallback_chain else "")
                + ". Its last reasoning, which may contain the "
                "answer:\n\n" + reasoning_preview
            )
        else:
            final_response = "(empty)"
        _turn_controller.move(_loop.TransitionKind.FINALIZE, _loop.TurnReason.EMPTY_RESPONSE)
        return TextCompletion(final_response, _turn_exit_reason)

    # Reset retry counter/signature on successful content
    agent._empty_content_retries = 0
    agent._thinking_prefill_retries = 0
    # Successful content reached — surface the one-shot fallback
    # switch notice (if a fallback activated this turn) before
    # dropping the noisy retry buffer, so a provider/model switch
    # stays visible even when the fallback succeeds.
    agent._emit_pending_fallback_notice()
    agent._clear_status_buffer()

    from agent.agent_runtime_helpers import (
        intent_ack_continuation_mode,
    )

    _ack_mode = intent_ack_continuation_mode(agent)
    if (
        _ack_mode != "off"
        and agent.valid_tool_names
        and _continuation.codex_ack_retries < 2
        and agent._looks_like_codex_intermediate_ack(
            user_message=_ctx.user_message,
            assistant_content=final_response,
            messages=_ctx.messages,
            require_workspace=(_ack_mode == "codex_only"),
        )
    ):
        _continuation.codex_ack_retries += 1
        interim_msg = agent._build_assistant_message(assistant_message, "incomplete")
        _loop.append_message(_ctx.messages, interim_msg)
        agent._emit_interim_assistant_message(interim_msg)

        continue_msg = {
            "role": "user",
            "content": _loop._CODEX_ACK_CONTINUATION_NUDGE,
        }
        _loop.append_message(_ctx.messages, continue_msg)
        agent._session_messages = _ctx.messages
        # An acknowledgment is explicitly non-final. Do not let its
        # text suppress iteration-limit summarization if this
        # continuation consumes the remaining budget.
        final_response = None
        return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.CODEX_ACK)

    _continuation.codex_ack_retries = 0

    if _continuation.parts:
        final_response = _loop._join_truncated_parts([*_continuation.parts, final_response])
        _continuation.parts = []
        _continuation.length_retries = 0
        # The continuation recovered, so the fragments stay in the transcript.
        for _frag in _ctx.messages:
            if isinstance(_frag, dict):
                _frag.pop("_length_continuation_fragment", None)
                _frag.pop("_length_continuation_nudge", None)

    final_response = agent._strip_think_blocks(final_response).strip()

    final_msg = agent._build_assistant_message(assistant_message, finish_reason)

    # ── Dropped tool-call recovery (copilot/Claude) ────────
    # Some providers (observed: claude-opus-4.8 / claude-sonnet-4.5
    # on GitHub Copilot, ~2026-07) return finish_reason="tool_calls"
    # while the parsed tool_calls array is empty — the model
    # signalled it wanted to act but the payload shipped no call.
    # Reaching finalization with that mismatch means the turn is
    # about to end with the task unstarted (the narration, which may
    # be in content or only in the reasoning field, gets treated as
    # the final answer). Re-prompt (bounded to 3 CONSECUTIVE stalls;
    # the budget resets after any successful tool round) to make the
    # model emit the call instead of exiting. finish_reason="stop"
    # text finishes never enter this guard.
    if (
        finish_reason == "tool_calls"
        and not assistant_message.tool_calls
        and getattr(agent, "_dropped_toolcall_retries", 0) < 3
    ):
        agent._dropped_toolcall_retries = getattr(agent, "_dropped_toolcall_retries", 0) + 1
        _loop.logger.warning(
            "finish_reason=tool_calls with empty tool_calls array "
            "(narration only) — re-prompting to emit the call "
            "(retry %d/3, model=%s provider=%s)",
            agent._dropped_toolcall_retries, agent.model, agent.provider,
        )
        agent._emit_status(
            "↻ Model signaled a tool call but sent none — "
            f"re-prompting ({agent._dropped_toolcall_retries}/3)"
        )
        # Both halves of the re-prompt pair are ephemeral recovery
        # scaffolding (mirrors the empty-response nudge pattern):
        # the interim narration-only assistant turn exists solely to
        # keep role alternation valid for the nudge, and the nudge
        # exists solely to drive the retry. Flag both so the
        # persistence layer never writes them to the durable
        # transcript and the finalization pop below can strip an
        # unanswered tail pair. A recovered (answered) pair stays
        # buried mid-list in live memory but is skipped by the
        # flush regardless of position.
        final_msg["_dropped_toolcall_nudge"] = True
        _loop.append_message(_ctx.messages, final_msg)
        _loop.append_message(_ctx.messages, {
            "role": "user",
            "content": _loop._DROPPED_TOOLCALL_NUDGE_CONTENT,
            "_dropped_toolcall_nudge": True,
        })
        agent._session_messages = _ctx.messages
        final_response = None
        return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.DROPPED_TOOL_CALL)

    # Reached finalization without the dropped-tool-call mismatch —
    # a genuine turn end. Clear the consecutive-stall budget so the
    # next turn starts fresh.
    agent._dropped_toolcall_retries = 0

    # Pop thinking-only prefill and empty-response retry
    # scaffolding before appending either a final response or a
    # verification-stop follow-up. These internal turns are only
    # for the next API retry and should not become durable
    # transcript context.
    while (
        _ctx.messages
        and isinstance(_ctx.messages[-1], dict)
        and (
            _ctx.messages[-1].get("_thinking_prefill")
            or _ctx.messages[-1].get("_empty_recovery_synthetic")
            or _ctx.messages[-1].get("_empty_terminal_sentinel")
            or _ctx.messages[-1].get("_dropped_toolcall_nudge")
        )
    ):
        _ctx.messages.pop()

    try:
        from agent.verification_stop import (
            build_verify_on_stop_nudge,
            verify_on_stop_enabled,
        )

        if verify_on_stop_enabled():
            _verify_nudge = build_verify_on_stop_nudge(
                session_id=getattr(agent, "session_id", None),
                changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
                attempts=getattr(agent, "_verification_stop_nudges", 0),
            )
        else:
            _verify_nudge = None
    except Exception:
        _loop.logger.debug("verification stop-loop check failed", exc_info=True)
        _verify_nudge = None

    if _verify_nudge:
        agent._verification_stop_nudges = (
            getattr(agent, "_verification_stop_nudges", 0) + 1
        )
        final_msg["finish_reason"] = "verification_required"
        # The assistant response is real content — persist it and
        # emit to the UI as an interim message so the user sees the
        # attempted final answer before the verification loop runs.
        # Only the nudge is flagged synthetic so it gets stripped
        # from the durable transcript (#65919 §7).
        agent._emit_interim_assistant_message(final_msg)
        _loop.append_message(_ctx.messages, final_msg)
        try:
            agent._flush_messages_to_session_db(_ctx.messages, _ctx.conversation_history)
        except Exception:
            _loop.logger.debug("verify-on-stop interim flush failed", exc_info=True)
        _loop.append_message(_ctx.messages, {
            "role": "user",
            "content": _verify_nudge,
            "_verification_stop_synthetic": True,
        })
        agent._session_messages = _ctx.messages
        # Run the verification-stop loop silently — the nudge is an
        # internal turn that should not add noise to the user's
        # terminal. Keep a debug breadcrumb in agent.log for tracing.
        _loop.logger.debug("verification stop-loop nudge issued (attempt %d)",
                     agent._verification_stop_nudges)
        # Keep the attempted answer only as an explicit fallback for
        # continuation-budget exhaustion.  ``final_response`` itself
        # must be cleared so the finalizer can distinguish this gate
        # from unrelated error/recovery exits. (#61631)
        # Track whether this candidate was already streamed so the
        # finalizer can mark the turn previewed only if the
        # candidate is actually reused as the final response.
        _continuation.hold_answer(agent, final_response)
        final_response = None
        return _turn_controller.move(
            _loop.TransitionKind.NEXT_STEP, _loop.TurnReason.VERIFICATION,
            durability=_loop.DurabilityBoundary.VERIFICATION_CANDIDATE,
        )

    # User verification-loop gate: when the agent edited code this
    # turn, let a registered `pre_verify` hook (plugin/shell) keep it
    # going one more turn. The shipped guidance is folded into the
    # evidence-based verify-on-stop nudge above, so this path has no
    # default continuation cost.
    _verify_nudge2 = None
    _edited = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
    _attempt = getattr(agent, "_pre_verify_nudges", 0)
    try:
        from agent.verify_hooks import max_verify_nudges
        from hermes_cli.lifecycle import has_hook
        from hermes_cli.plugins import get_pre_verify_continue_message

        if _edited and has_hook("pre_verify") and _attempt < max_verify_nudges():
            # Posture is fixed for the session — resolve once + cache.
            coding = getattr(agent, "_resolved_is_coding", None)
            if coding is None:
                from agent.coding_context import is_coding_context
                coding = bool(is_coding_context(platform=getattr(agent, "platform", "") or ""))
                agent._resolved_is_coding = coding
            _verify_nudge2 = get_pre_verify_continue_message(
                session_id=getattr(agent, "session_id", None) or "",
                platform=getattr(agent, "platform", "") or "",
                model=getattr(agent, "model", "") or "",
                coding=coding,
                attempt=_attempt,
                final_response=final_response,
                changed_paths=_edited,
            )
    except Exception:
        _loop.logger.debug("pre_verify hook check failed", exc_info=True)
        _verify_nudge2 = None

    if _verify_nudge2:
        agent._pre_verify_nudges = _attempt + 1
        final_msg["finish_reason"] = "verify_hook_continue"
        # The assistant response is real content — persist it and
        # emit to the UI as an interim message so the user sees the
        # attempted final answer before the pre_verify loop runs.
        # Only the nudge is flagged synthetic so it gets stripped
        # from the durable transcript (#65919 §7).
        agent._emit_interim_assistant_message(final_msg)
        _loop.append_message(_ctx.messages, final_msg)
        try:
            agent._flush_messages_to_session_db(_ctx.messages, _ctx.conversation_history)
        except Exception:
            _loop.logger.debug("pre_verify interim flush failed", exc_info=True)
        _loop.append_message(_ctx.messages, {
            "role": "user",
            "content": _verify_nudge2,
            "_pre_verify_synthetic": True,
        })
        agent._session_messages = _ctx.messages
        _loop.logger.debug("pre_verify nudge issued (attempt %d)",
                     agent._pre_verify_nudges)
        _continuation.hold_answer(agent, final_response)
        final_response = None
        return _turn_controller.move(
            _loop.TransitionKind.NEXT_STEP, _loop.TurnReason.PRE_VERIFY,
            durability=_loop.DurabilityBoundary.VERIFICATION_CANDIDATE,
        )

    # ── Kanban worker terminal-tool stop guard ─────────────
    # Workers must end with kanban_complete / kanban_block.
    # Models sometimes narrate the next step ("Let me write the
    # report") and stop with finish_reason=stop — a clean exit
    # that the dispatcher records as protocol_violation. Nudge
    # once or twice before allowing that exit.
    try:
        from agent.kanban_stop import build_kanban_stop_nudge

        _kanban_nudge = build_kanban_stop_nudge(
            messages=_ctx.messages,
            attempts=getattr(agent, "_kanban_stop_nudges", 0),
        )
    except Exception:
        _loop.logger.debug("kanban stop-loop check failed", exc_info=True)
        _kanban_nudge = None

    if _kanban_nudge:
        agent._kanban_stop_nudges = (
            getattr(agent, "_kanban_stop_nudges", 0) + 1
        )
        final_msg["finish_reason"] = "kanban_terminal_required"
        final_msg["_kanban_stop_synthetic"] = True
        _loop.append_message(_ctx.messages, final_msg)
        _loop.append_message(_ctx.messages, {
            "role": "user",
            "content": _kanban_nudge,
            "_kanban_stop_synthetic": True,
        })
        agent._session_messages = _ctx.messages
        _loop.logger.info(
            "kanban stop-loop nudge issued (attempt %d) task=%s",
            agent._kanban_stop_nudges,
            _loop.os.environ.get("HERMES_KANBAN_TASK", ""),
        )
        agent._emit_status(
            "⚠️ Kanban worker tried to exit without "
            "kanban_complete/kanban_block — nudging to finish"
        )
        # Same finalizer contract as verify-on-stop: clear
        # final_response while continuing so a later budget
        # exhaustion path does not treat the narrated stop as
        # a completed answer.
        _continuation.hold_answer(agent, final_response)
        final_response = None
        return _turn_controller.move(_loop.TransitionKind.NEXT_STEP, _loop.TurnReason.KANBAN_STOP)

    _loop.append_message(_ctx.messages, final_msg)
    # Make the completed answer durable before leaving the loop —
    # a session torn down before finalize_turn's _persist_session
    # otherwise loses a reply the user already saw (#81641). Same
    # contract as the tool-call exit (#49045) and the verify exits
    # above; _DB_PERSISTED_MARKER keeps _persist_session idempotent.
    # Unlike the tool-call exit, failure must NOT abort the turn:
    # no side effect follows and _persist_session retries the write.
    # Full incident narrative: tests/run_agent/test_81641_*.py.
    try:
        agent._flush_messages_to_session_db(_ctx.messages, _ctx.conversation_history)
    except Exception:
        _loop.logger.warning(
            "final text-turn flush failed (session=%s) — reply is "
            "not yet durable; relying on finalize_turn retry",
            getattr(agent, "session_id", None) or "none",
            exc_info=True,
        )

    _turn_exit_reason = f"text_response(finish_reason={finish_reason})"
    if not agent.quiet_mode:
        agent._safe_print(f"🎉 Conversation completed after {api_call_count} OpenAI-compatible API call(s)")
    _turn_controller.move(
        _loop.TransitionKind.FINALIZE, _loop.TurnReason.FINAL_RESPONSE,
        durability=_loop.DurabilityBoundary.FINAL_ANSWER,
    )
    return TextCompletion(final_response, _turn_exit_reason)

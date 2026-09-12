"""Attempt recovery and the single handoff out of a provider request cycle.

A cycle includes all network retries of one prepared logical request. Auth
and payload repair guards survive those retries. Rebuilding the logical
request creates a new cycle, while transport incident state survives it.
"""

from dataclasses import dataclass, field
from typing import Any

from agent.turn_retry_state import TurnRetryState
from agent.transport_recovery import TransportRecoveryState
from agent.turn_state_machine import (
    BudgetEffect, DurabilityBoundary, TransitionKind, TurnReason, TurnTransition,
    resolve_request_exit,
)


@dataclass
class TurnRecovery:
    """Recovery lifetimes that must survive rebuilding a request cycle."""

    transport: TransportRecoveryState = TransportRecoveryState.NONE
    compression_attempts: int = 0
    max_compression_attempts: int = 3
    last_preflight_pressure: int | None = None


@dataclass
class RequestOutcome:
    """Response handoff; direct terminal paths retain their existing dict."""

    response: Any
    api_kwargs: Any
    api_messages: list
    api_duration: float
    interrupted: bool
    final_response: str | None


@dataclass
class RequestCycle:
    max_retries: int
    retry_count: int = 0
    recovery: TurnRetryState = field(default_factory=TurnRetryState)
    pending: TurnTransition | None = None

    def restart(self, controller, reason: TurnReason) -> None:
        if self.pending is not None:
            raise ValueError("A request cycle already selected its next transition")
        if reason in {TurnReason.LENGTH, TurnReason.TRANSPORT_PARTIAL}:
            kind = TransitionKind.NEXT_STEP
            budget = BudgetEffect.KEEP
            boundary = DurabilityBoundary.RECOVERY_SCAFFOLD
        else:
            kind = TransitionKind.REBUILD
            budget = BudgetEffect.REFUND_STEP
            boundary = {
                TurnReason.COMPRESSION: DurabilityBoundary.COMPRESSION,
                TurnReason.REDIRECT: DurabilityBoundary.REDIRECT,
                TurnReason.PROVIDER_SWITCH: DurabilityBoundary.API_ONLY,
            }[reason]
        self.pending = controller.plan(kind, reason, budget=budget, durability=boundary)

    def has_restart(self, reason: TurnReason) -> bool:
        return self.pending is not None and self.pending.reason is reason

    def exit_reason(self, *, interrupted: bool) -> TurnReason:
        return resolve_request_exit(
            self.pending.reason if self.pending is not None else None,
            interrupted=interrupted,
        )

    def apply_restart(self, controller, agent, api_call_count):
        if self.pending is None:
            raise ValueError("No request restart was selected")
        decision = self.pending
        self.pending = None
        controller.apply(decision)
        if decision.budget is BudgetEffect.REFUND_STEP:
            return refund_step(agent, api_call_count)
        return api_call_count


def refund_step(agent, api_call_count: int) -> int:
    """Refund the same logical reservation, without changing display timing.

Some legacy callers synchronize agent._api_call_count immediately, others
only at the next reservation. Keep that observable timing at the caller.
"""
    agent.iteration_budget.refund()
    return api_call_count - 1



def run_request_cycle(agent, *, _ctx, _continuation, _recovery, _turn_controller, _cycle, semantic_progress, system_message, api_call_count, api_messages, tools_for_api, approx_tokens, total_chars, _moa_prepared_request, thinking_spinner, api_start_time, api_request_id):
    """Execute a prepared request until response, rebuild, or direct terminal.

    Existing context and recovery objects carry state across this boundary.
    Local repair/credential attempts never enter the tool executor.
    """
    from agent import conversation_loop as _loop

    finish_reason = "stop"
    response = None
    api_kwargs = None
    api_duration = 0.0
    interrupted = False
    final_response = None
    while _cycle.retry_count < _cycle.max_retries:
        # ── Nous Portal rate limit guard ──────────────────────
        # If another session already recorded that Nous is rate-
        # limited, skip the API call entirely.  Each attempt
        # (including SDK-level retries) counts against RPH and
        # deepens the rate limit hole.
        if agent.provider == "nous":
            try:
                from agent.nous_rate_guard import (
                    nous_rate_limit_remaining,
                    format_remaining as _fmt_nous_remaining,
                )
                _nous_remaining = nous_rate_limit_remaining()
                if _nous_remaining is not None and _nous_remaining > 0:
                    _nous_msg = (
                        f"Nous Portal rate limit active — "
                        f"resets in {_fmt_nous_remaining(_nous_remaining)}."
                    )
                    agent._buffer_vprint(
                        f"⏳ {_nous_msg} Trying fallback..."
                    )
                    agent._buffer_status(f"⏳ {_nous_msg}")
                    if agent._try_activate_fallback():
                        _ctx.active_system_prompt = _loop._sync_failover_system_message(
                            agent, api_messages, _ctx.active_system_prompt)
                        _cycle.retry_count = 0
                        _recovery.compression_attempts = 0
                        _cycle.recovery.primary_recovery_attempted = False
                        _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                        break
                    # No fallback available — surface buffered context
                    # so user sees the rate-limit message that led here.
                    agent._flush_status_buffer()
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": (
                            f"⏳ {_nous_msg}\n\n"
                            "No fallback provider available. "
                            "Try again after the reset, or add a "
                            "fallback provider in config.yaml."
                        ),
                        "messages": _ctx.messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "failed": True,
                        "error": _nous_msg,
                    })
            except ImportError:
                pass
            except Exception:
                pass  # Never let rate guard break the agent loop

        try:
            # Bound before anything that can raise: the transport-recovery
            # gate in the handler below reads it, and a failure earlier in
            # this block (e.g. _build_api_kwargs) would otherwise surface
            # as UnboundLocalError instead of the real error.  False is
            # also the correct default there — nothing was streamed yet.
            _use_streaming = False
            agent._reset_stream_delivery_tracking()
            # api_messages is built once, before this retry loop, while the
            # primary provider is active.  A mid-conversation fallback can
            # switch to a require-side provider (DeepSeek / Kimi / MiMo) that
            # rejects assistant turns lacking reasoning_content.  Re-apply the
            # echo-back pad for the *current* provider here (idempotent no-op
            # unless the active provider needs it) so the fallback request
            # isn't sent with stale, primary-shaped reasoning fields.
            agent._reapply_reasoning_echo_for_provider(api_messages)
            # Same story for prompt-cache decoration (#72626): try_activate_
            # fallback refreshes the policy flags, but the decorated list
            # still carries the primary's breakpoints (or none). Strip and
            # re-render for the current provider before building kwargs.
            api_messages, _moa_prepared_request, tools_for_api = (
                _loop._redecorate_prompt_cache_for_provider(
                    agent,
                    api_messages,
                    system_message=system_message,
                    moa_prepared=_moa_prepared_request,
                    tools_for_api=tools_for_api,
                )
            )
            if tools_for_api == agent.tools:
                api_kwargs = agent._build_api_kwargs(api_messages)
            else:
                api_kwargs = agent._build_api_kwargs(
                    api_messages,
                    tools_for_api=tools_for_api,
                )
            # Outbound-request surrogate chokepoint (#50959): the messages
            # were scrubbed above, but the rest of the request body —
            # tool/function descriptions (session_search's ±-heavy text is
            # the recorded repro), extra_body, system strings routed via
            # kwargs — can still carry invalid code points that providers
            # reject with a non-retryable HTTP 400 ("invalid unicode code
            # point"). One in-place walk here guarantees the entire
            # payload json.dumps()-safe regardless of which leaf produced
            # the string. Fast no-op when the payload is clean.
            _loop._sanitize_structure_surrogates(api_kwargs)
            if agent._force_ascii_payload:
                _loop._sanitize_structure_non_ascii(api_kwargs)
            if agent.api_mode == "codex_responses":
                api_kwargs = agent._get_transport().preflight_kwargs(
                    api_kwargs,
                    allow_stream=False,
                    is_github_responses=agent._is_copilot_url(),
                    sanitize_harmony_tokens=agent._is_codex_backend(),
                )
            # Copilot x-initiator: the first API call of a user turn is
            # marked "user" so Copilot bills a premium request; tool-loop
            # follow-ups keep the default "agent" header (#3040).
            if getattr(agent, "_is_user_initiated_turn", False) and agent._is_copilot_url():
                _xh = dict(api_kwargs.get("extra_headers") or {})
                _xh["x-initiator"] = "user"
                api_kwargs["extra_headers"] = _xh
                agent._is_user_initiated_turn = False
            try:
                from hermes_cli.middleware import apply_llm_request_middleware

                _llm_request_mw = apply_llm_request_middleware(
                    api_kwargs,
                    task_id=_ctx.effective_task_id,
                    turn_id=_ctx.turn_id,
                    api_request_id=api_request_id,
                    session_id=agent.session_id or "",
                    platform=agent.platform or "",
                    model=agent.model,
                    provider=agent.provider,
                    base_url=agent.base_url,
                    api_mode=agent.api_mode,
                    api_call_count=api_call_count,
                )
                api_kwargs = _llm_request_mw.payload
                _original_api_kwargs = _llm_request_mw.original_payload
                _llm_middleware_trace = _llm_request_mw.trace
            except Exception:
                _original_api_kwargs = dict(api_kwargs)
                _llm_middleware_trace = []

            try:
                from hermes_cli.lifecycle import (
                    has_hook,
                    invoke_hook as _invoke_hook,
                )
                if has_hook("pre_api_request"):
                    request_messages = api_kwargs.get("messages")
                    if not isinstance(request_messages, list):
                        request_messages = api_kwargs.get("input")
                    if not isinstance(request_messages, list):
                        request_messages = api_messages
                    # Shallow-copy the outer list so plugins that retain the
                    # reference for async snapshotting don't observe later
                    # mutations of api_messages.  The inner dicts are not
                    # mutated by the agent loop, so a shallow copy is
                    # sufficient; a deepcopy would walk every tool result
                    # and base64 image on every API call.
                    #
                    # The ``request_messages`` and ``conversation_history``
                    # kwargs below are pre-existing raw passthroughs
                    # consumed by the bundled langfuse plugin
                    # (``plugins/observability/langfuse/__init__.py:_coerce_request_messages``).
                    # They predate ``request`` and are intentionally NOT
                    # sanitised — secrets are not expected here because
                    # ``api_kwargs`` is the same object passed to the
                    # provider client.  New consumers should read the
                    # sanitised view from ``request["body"]["messages"]``.
                    _request_payload = agent._api_request_payload_for_hook(api_kwargs)
                    # Anthropic (``system``) and Responses/Codex
                    # (``instructions``) move the system prompt out of
                    # messages; pass it explicitly for observability
                    # plugins (Langfuse).
                    system_prompt_for_hooks = _loop._system_prompt_for_hooks(
                        api_kwargs, request_messages
                    )
                    _invoke_hook(
                        "pre_api_request",
                        task_id=_ctx.effective_task_id,
                        turn_id=_ctx.turn_id,
                        api_request_id=api_request_id,
                        session_id=agent.session_id or "",
                        user_message=_ctx.original_user_message,
                        conversation_history=list(_ctx.messages),
                        platform=agent.platform or "",
                        model=agent.model,
                        provider=agent.provider,
                        base_url=agent.base_url,
                        api_mode=agent.api_mode,
                        api_call_count=api_call_count,
                        retry_count=_cycle.retry_count,
                        request_messages=list(request_messages)
                        if isinstance(request_messages, list)
                        else [],
                        system_prompt=system_prompt_for_hooks,
                        message_count=len(api_messages),
                        tool_count=len(agent.tools or []),
                        approx_input_tokens=approx_tokens,
                        request_char_count=total_chars,
                        max_tokens=agent.max_tokens,
                        started_at=api_start_time,
                        middleware_trace=list(_llm_middleware_trace),
                        request=_request_payload,
                    )
            except Exception:
                pass

            if _loop.env_var_enabled("HERMES_DUMP_REQUESTS"):
                agent._dump_api_request_debug(api_kwargs, reason="preflight")

            # This object is private to the in-process MoA facade.  Add it
            # only after middleware, hooks, and debug dumps so none of them
            # attempts to serialize it as part of the provider payload.
            if _moa_prepared_request is not None and agent.provider == "moa":
                # Re-read the live client instead of trusting the one that
                # prepared the request above. Credential rotation, provider
                # fallback and dead-connection cleanup all rebuild
                # agent.client from _client_kwargs between attempts, and
                # pending_moa_prepared_request carries a prepared request
                # across exactly that boundary. The rebuilt client is a
                # native OpenAI client while provider stays "moa", so this
                # private key would reach the SDK as an unexpected keyword
                # — a non-retryable TypeError that kills every remaining
                # turn on the session.
                if _loop._moa_client_consumes_prepared_request(agent.client):
                    api_kwargs["_moa_prepared_request"] = _moa_prepared_request
                else:
                    _loop.logger.warning(
                        "MoA client replaced mid-turn (client=%s); sending the "
                        "prepared prompt without the MoA handshake",
                        type(agent.client).__name__,
                    )

            # Always prefer the streaming path — even without stream
            # consumers.  Streaming gives us fine-grained health
            # checking (90s stale-stream detection, 60s read timeout)
            # that the non-streaming path lacks.  Without this,
            # subagents and other quiet-mode callers can hang
            # indefinitely when the provider keeps the connection
            # alive with SSE pings but never delivers a response.
            # The streaming path is a no-op for callbacks when no
            # consumers are registered, and falls back to non-
            # streaming automatically if the provider doesn't
            # support it.
            def _stop_spinner():
                nonlocal thinking_spinner
                if thinking_spinner:
                    thinking_spinner.stop("")
                    thinking_spinner = None
                if agent.thinking_callback:
                    agent.thinking_callback("")

            _use_streaming = True
            # Provider signaled "stream not supported" on a previous
            # attempt — switch to non-streaming for the rest of this
            # session instead of re-failing every retry.
            if getattr(agent, "_disable_streaming", False):
                _use_streaming = False
            # CopilotACPClient communicates via subprocess stdio and
            # returns a plain SimpleNamespace — not an iterable
            # stream.  Mirror the ACP exclusion used for Responses
            # API upgrade (lines ~1083-1085).
            elif (
                agent.provider in {"copilot-acp"}
                or str(agent.base_url or "").lower().startswith("acp://copilot")
                or str(agent.base_url or "").lower().startswith("acp+tcp://")
            ):
                _use_streaming = False
            # MoA streams only when a display/TTS consumer is present to
            # receive the deltas. MoAChatCompletions.create() honors
            # stream=True (runs the references, then returns the aggregator's
            # raw token stream) and is reached here because, for provider
            # "moa", _create_request_openai_client returns the MoA facade
            # itself. Without consumers (quiet mode, subagents, health-check
            # probes) we keep the complete-response path: the facade returns a
            # whole response when stream is not requested, preserving the
            # prior behavior for those callers.
            elif agent.provider == "moa" and not agent._has_stream_consumers():
                _use_streaming = False
            elif not agent._has_stream_consumers():
                # No display/TTS consumer. Still prefer streaming for
                # health checking, but skip for Mock clients in tests
                # (mocks return SimpleNamespace, not stream iterators).
                from unittest.mock import Mock
                if isinstance(getattr(agent, "client", None), Mock):
                    _use_streaming = False
            # Transport-recovery override, applied last so it wins over
            # every preference above: the streaming path is what just
            # died mid-body, so the one bounded recovery attempt goes out
            # non-streaming.  Re-entering streaming here is exactly how a
            # single dropped connection used to multiply into a retry ×
            # continuation chain.
            if _recovery.transport is not _loop.TransportRecoveryState.NONE:
                _use_streaming = False

            def _perform_api_call(next_api_kwargs):
                if not agent._interrupt_requested:
                    _semantic_delivery = semantic_progress.mark_request_started()
                    if _semantic_delivery.action == "nudge":
                        _loop.logger.warning(
                            "semantic progress: action=nudge stalled_rounds=%d cycle=%d unique_actions=%d tool_count=%d",
                            _semantic_delivery.stalled_rounds, _semantic_delivery.cycle,
                            _semantic_delivery.unique_actions, _semantic_delivery.tool_count,
                        )
                if agent.api_mode == "codex_responses":
                    next_api_kwargs = agent._get_transport().preflight_kwargs(
                        next_api_kwargs,
                        allow_stream=False,
                        is_github_responses=agent._is_copilot_url(),
                        sanitize_harmony_tokens=agent._is_codex_backend(),
                    )
                if _use_streaming:
                    return agent._interruptible_streaming_api_call(
                        next_api_kwargs, on_first_delta=_stop_spinner,
                        # run_conversation owns the bounded transport
                        # budget for its own model turns, so the streaming
                        # layer surfaces the first drop instead of burning
                        # its local reconnect attempts first.  Callers
                        # outside this turn contract (relay/native probes,
                        # direct helper users) keep the legacy loop.
                        owns_transport_recovery=True,
                    )
                from agent import relay_llm

                return relay_llm.execute(
                    next_api_kwargs,
                    agent._interruptible_api_call,
                    session_id=str(agent.session_id or ""),
                    name=str(agent.provider or "provider"),
                    model_name=str(agent.model or ""),
                    metadata={
                        "api_mode": agent.api_mode,
                        "api_request_id": api_request_id,
                        "call_role": (
                            "delegated"
                            if getattr(agent, "is_subagent", False)
                            else "fallback"
                            if int(getattr(agent, "_fallback_index", 0) or 0) > 0
                            else "primary"
                        ),
                        "retry_count": _cycle.retry_count,
                    },
                    defer_logical_completion=True,
                )

            from hermes_cli.middleware import run_llm_execution_middleware

            _model_request_active = getattr(agent, "_model_request_active", None)
            _redirect_lock = getattr(agent, "_pending_redirect_lock", None)
            if _redirect_lock is not None:
                with _redirect_lock:
                    if _model_request_active is not None:
                        _model_request_active.set()
            elif _model_request_active is not None:
                _model_request_active.set()
            _redirect_crossed_response = False
            try:
                response = run_llm_execution_middleware(
                    api_kwargs,
                    _perform_api_call,
                    original_request=_original_api_kwargs,
                    task_id=_ctx.effective_task_id,
                    turn_id=_ctx.turn_id,
                    api_request_id=api_request_id,
                    session_id=agent.session_id or "",
                    platform=agent.platform or "",
                    model=agent.model,
                    provider=agent.provider,
                    base_url=agent.base_url,
                    api_mode=agent.api_mode,
                    api_call_count=api_call_count,
                    middleware_trace=list(_llm_middleware_trace),
                )
            finally:
                if _redirect_lock is not None:
                    with _redirect_lock:
                        if _model_request_active is not None:
                            _model_request_active.clear()
                        _redirect_crossed_response = bool(
                            agent._pending_redirect
                        )
                else:
                    if _model_request_active is not None:
                        _model_request_active.clear()
                    _redirect_crossed_response = agent._has_pending_redirect()
            if _redirect_crossed_response:
                # The response and redirect can cross on different threads:
                # redirect() observed the request as active just before this
                # call returned. Discard that now-stale response and rebuild
                # from the correction rather than silently losing it.
                if thinking_spinner:
                    thinking_spinner.stop("")
                    thinking_spinner = None
                if agent.thinking_callback:
                    agent.thinking_callback("")
                if agent.clear_interrupt(preserve_redirect=True):
                    _cycle.restart(_turn_controller, _loop.TurnReason.REDIRECT)
                else:
                    interrupted = True
                break

            api_duration = _loop.time.time() - api_start_time

            # Stop thinking spinner silently -- the response box or tool
            # execution messages that follow are more informative.
            if thinking_spinner:
                thinking_spinner.stop("")
                thinking_spinner = None
            if agent.thinking_callback:
                agent.thinking_callback("")

            if not agent.quiet_mode:
                agent._vprint(f"{agent.log_prefix}⏱️  API call completed in {api_duration:.2f}s")

            if agent.verbose_logging:
                # Log response with provider info if available
                resp_model = getattr(response, 'model', 'N/A') if response else 'N/A'
                _loop.logging.debug(f"API Response received - Model: {resp_model}, Usage: {response.usage if hasattr(response, 'usage') else 'N/A'}")

            # Validate response shape before proceeding
            response_invalid = False
            error_details = []
            if agent.api_mode == "codex_responses":
                _ct_v = agent._get_transport()
                if not _ct_v.validate_response(response):
                    if response is None:
                        response_invalid = True
                        error_details.append("response is None")
                    else:
                        # Provider returned a terminal failure (e.g. quota exhaustion).
                        # Treat as invalid so the fallback chain is triggered instead of
                        # letting the error bubble up outside the retry/fallback loop.
                        _codex_resp_status = str(getattr(response, "status", "") or "").strip().lower()
                        if _codex_resp_status in {"failed", "cancelled"}:
                            _codex_error_obj = getattr(response, "error", None)
                            _codex_error_msg = (
                                _codex_error_obj.get("message") if isinstance(_codex_error_obj, dict)
                                else str(_codex_error_obj) if _codex_error_obj
                                else f"Responses API returned status '{_codex_resp_status}'"
                            )
                            _loop.logger.warning(
                                "Codex response status='%s' (error=%s). Routing to fallback. %s",
                                _codex_resp_status, _codex_error_msg,
                                agent._client_log_context(),
                            )
                            response_invalid = True
                            error_details.append(f"response.status={_codex_resp_status}: {_codex_error_msg}")
                        else:
                            # output_text fallback: stream backfill may have failed
                            # but normalize can still recover from output_text
                            _out_text = getattr(response, "output_text", None)
                            _out_text_stripped = _out_text.strip() if isinstance(_out_text, str) else ""
                            if _out_text_stripped:
                                _loop.logger.debug(
                                    "Codex response.output is empty but output_text is present "
                                    "(%d chars); deferring to normalization.",
                                    len(_out_text_stripped),
                                )
                            else:
                                _resp_status = getattr(response, "status", None)
                                _resp_incomplete = getattr(response, "incomplete_details", None)
                                _loop.logger.warning(
                                    "Codex response.output is empty after stream backfill "
                                    "(status=%s, incomplete_details=%s, model=%s). %s",
                                    _resp_status, _resp_incomplete,
                                    getattr(response, "model", None),
                                    f"api_mode={agent.api_mode} provider={agent.provider}",
                                )
                                response_invalid = True
                                error_details.append("response.output is empty")
            elif agent.api_mode == "anthropic_messages":
                _tv = agent._get_transport()
                if not _tv.validate_response(response):
                    response_invalid = True
                    if response is None:
                        error_details.append("response is None")
                    else:
                        error_details.append("response.content invalid (not a non-empty list)")
            elif agent.api_mode == "bedrock_converse":
                _btv = agent._get_transport()
                if not _btv.validate_response(response):
                    response_invalid = True
                    if response is None:
                        error_details.append("response is None")
                    else:
                        error_details.append("Bedrock response invalid (no output or choices)")
            else:
                _ctv = agent._get_transport()
                if not _ctv.validate_response(response):
                    response_invalid = True
                    if response is None:
                        error_details.append("response is None")
                    elif not hasattr(response, 'choices'):
                        error_details.append("response has no 'choices' attribute")
                    elif response.choices is None:
                        error_details.append("response.choices is None")
                    else:
                        error_details.append("response.choices is empty")

            if response_invalid:
                agent._invoke_api_request_error_hook(
                    task_id=_ctx.effective_task_id,
                    turn_id=_ctx.turn_id,
                    api_request_id=api_request_id,
                    api_call_count=api_call_count,
                    api_start_time=api_start_time,
                    api_kwargs=api_kwargs,
                    error_type="InvalidAPIResponse",
                    error_message=", ".join(error_details) or "Invalid API response",
                    status_code=getattr(getattr(response, "error", None), "code", None),
                    retry_count=_cycle.retry_count,
                    max_retries=_cycle.max_retries,
                    retryable=True,
                    reason="invalid_response",
                )
                # Stop spinner silently — retry status is now buffered
                # and only surfaced if every retry+fallback exhausts.
                if thinking_spinner:
                    thinking_spinner.stop("")
                    thinking_spinner = None
                if agent.thinking_callback:
                    agent.thinking_callback("")

                # Invalid response — could be rate limiting, provider timeout,
                # upstream server error, or malformed response.
                _cycle.retry_count += 1

                # Eager fallback: empty/malformed responses are a common
                # rate-limit symptom.  Switch to fallback immediately
                # rather than retrying with extended backoff.
                if agent._fallback_index < len(agent._fallback_chain):
                    agent._buffer_status("⚠️ Empty/malformed response — switching to fallback...")
                if agent._try_activate_fallback():
                    _ctx.active_system_prompt = _loop._sync_failover_system_message(
                        agent, api_messages, _ctx.active_system_prompt)
                    _cycle.retry_count = 0
                    _recovery.compression_attempts = 0
                    _cycle.recovery.primary_recovery_attempted = False
                    _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                    break

                # Check for error field in response (some providers include this)
                error_msg = "Unknown"
                provider_name = "Unknown"
                if response and hasattr(response, 'error') and response.error:
                    error_msg = str(response.error)
                    # Try to extract provider from error metadata
                    if hasattr(response.error, 'metadata') and response.error.metadata:
                        provider_name = response.error.metadata.get('provider_name', 'Unknown')
                elif response and hasattr(response, 'message') and response.message:
                    error_msg = str(response.message)

                # Try to get provider from model field (OpenRouter often returns actual model used)
                if provider_name == "Unknown" and response and hasattr(response, 'model') and response.model:
                    provider_name = f"model={response.model}"

                # Check for x-openrouter-provider or similar metadata
                if provider_name == "Unknown" and response:
                    # Log all response attributes for debugging
                    resp_attrs = {k: str(v)[:100] for k, v in vars(response).items() if not k.startswith('_')}
                    if agent.verbose_logging:
                        _loop.logging.debug(f"Response attributes for invalid response: {resp_attrs}")

                # Extract error code from response for contextual diagnostics
                _resp_error_code = None
                if response and hasattr(response, 'error') and response.error:
                    _code_raw = getattr(response.error, 'code', None)
                    if _code_raw is None and isinstance(response.error, dict):
                        _code_raw = response.error.get('code')
                    if _code_raw is not None:
                        try:
                            _resp_error_code = int(_code_raw)
                        except (TypeError, ValueError):
                            pass

                # Build a human-readable failure hint from the error code
                # and response time, instead of always assuming rate limiting.
                if _resp_error_code == 524:
                    _failure_hint = f"upstream provider timed out (Cloudflare 524, {api_duration:.0f}s)"
                elif _resp_error_code == 504:
                    _failure_hint = f"upstream gateway timeout (504, {api_duration:.0f}s)"
                elif _resp_error_code == 429:
                    _failure_hint = "rate limited by upstream provider (429)"
                elif _resp_error_code in {500, 502}:
                    _failure_hint = f"upstream server error ({_resp_error_code}, {api_duration:.0f}s)"
                elif _resp_error_code in {503, 529}:
                    _failure_hint = f"upstream provider overloaded ({_resp_error_code})"
                elif _resp_error_code is not None:
                    _failure_hint = f"upstream error (code {_resp_error_code}, {api_duration:.0f}s)"
                elif api_duration < 10:
                    _failure_hint = f"fast response ({api_duration:.1f}s) — likely rate limited"
                elif api_duration > 60:
                    _failure_hint = f"slow response ({api_duration:.0f}s) — likely upstream timeout"
                else:
                    _failure_hint = f"response time {api_duration:.1f}s"

                agent._buffer_vprint(f"⚠️  Invalid API response (attempt {_cycle.retry_count}/{_cycle.max_retries}): {', '.join(error_details)}")
                agent._buffer_vprint(f"   🏢 Provider: {provider_name}")
                cleaned_provider_error = agent._clean_error_message(error_msg)
                agent._buffer_vprint(f"   📝 Provider message: {cleaned_provider_error}")
                agent._buffer_vprint(f"   ⏱️  {_failure_hint}")

                if _cycle.retry_count >= _cycle.max_retries:
                    # Try fallback before giving up
                    if agent._has_pending_fallback():
                        agent._buffer_status(f"⚠️ Max retries ({_cycle.max_retries}) for invalid responses — trying fallback...")
                    if agent._try_activate_fallback():
                        _ctx.active_system_prompt = _loop._sync_failover_system_message(
                            agent, api_messages, _ctx.active_system_prompt)
                        _cycle.retry_count = 0
                        _recovery.compression_attempts = 0
                        _cycle.recovery.primary_recovery_attempted = False
                        _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                        break
                    # Terminal — flush buffered retry trace so user sees what happened.
                    agent._flush_status_buffer()
                    agent._emit_status(f"❌ Max retries ({_cycle.max_retries}) exceeded for invalid responses. Giving up.")
                    _loop.logger.error("%sInvalid API response after %d retries.", agent.log_prefix, _cycle.max_retries)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    _final_response = f"Invalid API response after {_cycle.max_retries} retries: {_failure_hint}"
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _final_response,
                        "messages": _ctx.messages,
                        "completed": False,
                        "api_calls": api_call_count,
                        "error": _final_response,
                        "failed": True  # Mark as failure for filtering
                    })

                # Backoff before retry — jittered exponential: 5s base, 120s cap
                wait_time = _loop.jittered_backoff(_cycle.retry_count, base_delay=5.0, max_delay=120.0)
                agent._buffer_vprint(f"⏳ Retrying in {wait_time:.1f}s ({_failure_hint})...")
                _loop.logger.warning("Invalid API response (retry %d/%d): %s | Provider: %s", _cycle.retry_count, _cycle.max_retries, ', '.join(error_details), provider_name)

                # Sleep in small increments to stay responsive to interrupts
                sleep_end = _loop.time.time() + wait_time
                _backoff_touch_counter = 0
                while _loop.time.time() < sleep_end:
                    if agent._interrupt_requested:
                        # A redirect uses the interrupt machinery to cancel
                        # only the live request. Aborting the retry here
                        # with clear_interrupt() would DESTROY the pending
                        # correction and kill the turn with "Operation
                        # interrupted" — the exact mid-stream steer loss
                        # users hit when a redirect lands during provider
                        # backoff. Rebuild from the correction instead,
                        # mirroring the InterruptedError handler.
                        if agent.clear_interrupt(preserve_redirect=True):
                            _cycle.restart(_turn_controller, _loop.TurnReason.REDIRECT)
                            break
                        agent._vprint(f"{agent.log_prefix}⚡ Interrupt detected during retry wait, aborting.", force=True)
                        _interrupt_text = f"Operation interrupted during retry ({_failure_hint}, attempt {_cycle.retry_count}/{_cycle.max_retries})."
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
                    # Touch activity every ~30s so the gateway's inactivity
                    # monitor knows we're alive during backoff waits.
                    _backoff_touch_counter += 1
                    if _backoff_touch_counter % 150 == 0:  # 150 × 0.2s = 30s
                        agent._touch_activity(
                            f"retry backoff ({_cycle.retry_count}/{_cycle.max_retries}), "
                            f"{int(sleep_end - _loop.time.time())}s remaining"
                        )
                if _cycle.has_restart(_loop.TurnReason.REDIRECT):
                    break  # rebuild this iteration from the correction
                _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.INVALID_RESPONSE)
                continue  # Retry the API call

            agent._turn_received_provider_response = True

            # Check finish_reason before proceeding
            if agent.api_mode == "codex_responses":
                status = getattr(response, "status", None)
                if isinstance(status, str):
                    status = status.strip().lower()
                incomplete_details = getattr(response, "incomplete_details", None)
                incomplete_reason = None
                if isinstance(incomplete_details, dict):
                    incomplete_reason = incomplete_details.get("reason")
                else:
                    incomplete_reason = getattr(incomplete_details, "reason", None)
                if incomplete_reason is not None:
                    incomplete_reason = str(incomplete_reason).strip().lower()
                if status == "incomplete" and incomplete_reason in {"max_output_tokens", "length"}:
                    # Responses API max-output exhaustion is a normal
                    # Codex incomplete turn.  Let the Codex-specific
                    # continuation path below append the incomplete
                    # assistant state and retry, instead of routing to
                    # the generic chat-completions length rollback that
                    # emits "Response truncated due to output length
                    # limit" and stops gateway turns.
                    finish_reason = "incomplete"
                elif status == "incomplete" and incomplete_reason == "content_filter":
                    finish_reason = "content_filter"
                else:
                    finish_reason = "stop"
            elif agent.api_mode == "anthropic_messages":
                _tfr = agent._get_transport()
                finish_reason = _tfr.map_finish_reason(response.stop_reason)
            elif agent.api_mode == "bedrock_converse":
                # Bedrock response already normalized at dispatch — use transport
                _bt_fr = agent._get_transport()
                _bedrock_result = _bt_fr.normalize_response(response)
                finish_reason = _bedrock_result.finish_reason
            else:
                _cc_fr = agent._get_transport()
                _finish_result = _cc_fr.normalize_response(response)
                finish_reason = _finish_result.finish_reason
                assistant_message = _finish_result
                if agent._should_treat_stop_as_truncated(
                    finish_reason,
                    assistant_message,
                    _ctx.messages,
                ):
                    agent._vprint(
                        f"{agent.log_prefix}⚠️  Treating suspicious Ollama/GLM stop response as truncated",
                        force=True,
                    )
                    finish_reason = "length"

            # A response that is NOT a swallowed transport failure means
            # the incident this turn was recovering from is over (the
            # non-streaming retry or the single continuation worked).
            # Release the bounded budget so a genuinely separate drop
            # later in a long tool loop still gets its own one attempt —
            # what the state forbids is a second recovery for the SAME
            # incident, not recovery for the rest of the turn.
            if (
                _recovery.transport is not _loop.TransportRecoveryState.NONE
                and not _loop.is_transport_interrupted(response)
            ):
                _recovery.transport = _loop.TransportRecoveryState.NONE

            # ── Content-policy refusal (HTTP 200) ──────────────────
            # The model — or the provider's safety system — returned a
            # *successful* response whose stop/finish reason is a refusal:
            # Anthropic ``stop_reason="refusal"`` → ``content_filter``;
            # OpenAI / portal ``finish_reason="content_filter"`` or a
            # populated ``message.refusal`` (mapped in the chat_completions
            # transport); Bedrock ``guardrail_intervened``. The content is
            # typically empty, so without this branch the response falls
            # through to the empty-response / invalid-response retry loops
            # and is mis-surfaced as "rate limited" / "no content after
            # retries" — burning paid attempts reproducing a deterministic
            # refusal. Surface it clearly and stop. Mirrors the
            # exception-based ``content_policy_blocked`` recovery: try a
            # configured fallback once, otherwise return the refusal.
            if finish_reason == "content_filter":
                _refusal_transport = agent._get_transport()
                if agent.api_mode == "anthropic_messages":
                    _refusal_result = _refusal_transport.normalize_response(
                        response, strip_tool_prefix=agent._is_anthropic_oauth
                    )
                else:
                    _refusal_result = _refusal_transport.normalize_response(response)
                _refusal_text = (getattr(_refusal_result, "content", None) or "").strip()
                # Some refusals carry the explanation only in the reasoning
                # channel; fall back to it so the user sees *something*.
                if not _refusal_text:
                    _refusal_text = (agent._extract_reasoning(_refusal_result) or "").strip()

                agent._invoke_api_request_error_hook(
                    task_id=_ctx.effective_task_id,
                    turn_id=_ctx.turn_id,
                    api_request_id=api_request_id,
                    api_call_count=api_call_count,
                    api_start_time=api_start_time,
                    api_kwargs=api_kwargs,
                    error_type="ContentPolicyBlocked",
                    error_message=_refusal_text or "model declined to respond (content_filter)",
                    status_code=None,
                    retry_count=_cycle.retry_count,
                    max_retries=_cycle.max_retries,
                    retryable=False,
                    reason=_loop.FailoverReason.content_policy_blocked.value,
                )

                if thinking_spinner:
                    thinking_spinner.stop("")
                    thinking_spinner = None
                if agent.thinking_callback:
                    agent.thinking_callback("")

                # Deterministic for the unchanged prompt — never retry.
                # Try a configured fallback once (a different model may not
                # refuse); otherwise surface the refusal terminally.
                if agent._has_pending_fallback():
                    agent._buffer_status(
                        "⚠️ Model declined to respond (safety refusal) — trying fallback..."
                    )
                if agent._try_activate_fallback():
                    _ctx.active_system_prompt = _loop._sync_failover_system_message(
                        agent, api_messages, _ctx.active_system_prompt)
                    _cycle.retry_count = 0
                    _recovery.compression_attempts = 0
                    _cycle.recovery.primary_recovery_attempted = False
                    _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                    break

                agent._flush_status_buffer()
                _refusal_log = (
                    _refusal_text[:500] + "..."
                    if len(_refusal_text) > 500
                    else _refusal_text
                )
                _loop.logger.warning(
                    "%sModel declined to respond (finish_reason=content_filter). "
                    "model=%s provider=%s refusal=%s",
                    agent.log_prefix, agent.model, agent.provider,
                    _refusal_log or "(no text)",
                )
                agent._emit_status(
                    "⚠️ The model declined to respond to this request (safety refusal)."
                )

                _refusal_detail = (
                    f"Model's explanation: {_refusal_text}"
                    if _refusal_text
                    else "The model returned no explanation."
                )
                _refusal_response = (
                    "⚠️  The model declined to respond to this request "
                    "(safety refusal — not a Hermes/gateway failure).\n\n"
                    f"{_refusal_detail}\n\n"
                    f"{_loop._CONTENT_POLICY_RECOVERY_HINT}"
                )

                agent._cleanup_task_resources(_ctx.effective_task_id)
                agent._persist_session(_ctx.messages, _ctx.conversation_history)
                return _loop._complete_direct_turn(_turn_controller, _loop._content_policy_blocked_result(
                    _ctx.messages,
                    api_call_count,
                    final_response=_refusal_response,
                    error_detail=_refusal_text or "model declined (content_filter)",
                ))

            if finish_reason == "length":
                if getattr(response, "id", "") == _loop.PARTIAL_STREAM_STUB_ID:
                    agent._vprint(
                        f"{agent.log_prefix}⚠️  Response truncated — stream "
                        f"ended before completion",
                        force=True,
                    )
                else:
                    agent._vprint(
                        f"{agent.log_prefix}⚠️  Response truncated "
                        f"(finish_reason='length') - model hit max output tokens",
                        force=True,
                    )

                # Normalize the truncated response to a single OpenAI-style
                # message shape so text-continuation and tool-call retry
                # work uniformly across chat_completions, bedrock_converse,
                # and anthropic_messages.  For Anthropic we use the same
                # adapter the agent loop already relies on so the rebuilt
                # interim assistant message is byte-identical to what
                # would have been appended in the non-truncated path.
                _trunc_msg = None
                _trunc_transport = agent._get_transport()
                if agent.api_mode == "anthropic_messages":
                    _trunc_result = _trunc_transport.normalize_response(
                        response, strip_tool_prefix=agent._is_anthropic_oauth
                    )
                else:
                    _trunc_result = _trunc_transport.normalize_response(response)
                _trunc_msg = _trunc_result

                _trunc_content = getattr(_trunc_msg, "content", None) if _trunc_msg else None
                _trunc_has_tool_calls = bool(getattr(_trunc_msg, "tool_calls", None)) if _trunc_msg else False

                # ── Detect thinking-budget exhaustion ──────────────
                # When the model spends ALL output tokens on reasoning
                # and has none left for the response, continuation
                # retries are pointless.  Detect this early and give a
                # targeted error instead of wasting 3 API calls.
                # A response is "thinking exhausted" only when the model
                # actually produced reasoning blocks but no visible text after
                # them.  Models that do not use <think> tags (e.g. GLM-4.7 on
                # NVIDIA Build, minimax) may return content=None or an empty
                # string for unrelated reasons — treat those as normal
                # truncations that deserve continuation retries, not as
                # thinking-budget exhaustion.
                _has_think_tags = bool(
                    _trunc_content and _loop.re.search(
                        r'<(?:think|thinking|reasoning|REASONING_SCRATCHPAD)[^>]*>',
                        _trunc_content,
                        _loop.re.IGNORECASE,
                    )
                )
                _thinking_exhausted = (
                    not _trunc_has_tool_calls
                    and _has_think_tags
                    and (
                        (_trunc_content is not None and not agent._has_content_after_think_block(_trunc_content))
                        or _trunc_content is None
                    )
                )

                if _thinking_exhausted:
                    _exhaust_error = (
                        "Model used all output tokens on reasoning with none left "
                        "for the response. Try lowering reasoning effort or "
                        "increasing max_tokens."
                    )
                    agent._vprint(
                        f"{agent.log_prefix}💭 Reasoning exhausted the output token budget — "
                        f"no visible response was produced.",
                        force=True,
                    )
                    # Return a user-friendly message as the response so
                    # CLI (response box) and gateway (chat message) both
                    # display it naturally instead of a suppressed error.
                    _exhaust_response = (
                        "⚠️ **Thinking Budget Exhausted**\n\n"
                        "The model used all its output tokens on reasoning "
                        "and had none left for the actual response.\n\n"
                        "To fix this:\n"
                        "→ Lower reasoning effort: `/thinkon low` or `/thinkon minimal`\n"
                        "→ Or switch to a larger/non-reasoning model with `/model`"
                    )
                    agent._cleanup_task_resources(_ctx.effective_task_id)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _exhaust_response,
                        "messages": _ctx.messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": _exhaust_error,
                    })

                # ── Detect repetition-dominated truncation (#86581) ──
                # A model in a degenerate repetition loop can spend its
                # ENTIRE output budget echoing one fragment.  The
                # continuation nudge below would then stitch the
                # pathological fragment into the final response — in the
                # #86581 incident one turn produced a 60,698-char
                # response delivered as 31 Discord messages.  Abort with
                # a clear user-facing error instead, mirroring the
                # _thinking_exhausted guard above.  Reasoning blocks are
                # stripped first (repeated scratchpad lines are not
                # evidence of a degenerate visible response).
                _visible_trunc = (
                    agent._strip_think_blocks(_trunc_content)
                    if isinstance(_trunc_content, str)
                    else _trunc_content
                )
                _repetition_dominated = (
                    not _trunc_has_tool_calls
                    and bool(_visible_trunc)
                    and _loop.is_repetition_dominated(_visible_trunc)
                )
                if _repetition_dominated:
                    _rep_error = (
                        "Model output entered a repetition loop and was "
                        "truncated mid-loop; refusing to continue a "
                        "degenerate response."
                    )
                    agent._vprint(
                        f"{agent.log_prefix}🔁 Response dominated by "
                        f"repeated text — stopping instead of "
                        f"continuing a degenerate response.",
                        force=True,
                    )
                    _rep_response = (
                        "⚠️ **Response Stopped — Repetition Detected**\n\n"
                        "The model fell into a repetition loop while "
                        "writing this response, so continuing would only "
                        "produce more repeated text. The partial response "
                        "was discarded.\n\n"
                        "→ Switch to a different model with `/model`\n"
                        "→ Or resend your message (your conversation "
                        "history is preserved)"
                    )
                    agent._cleanup_task_resources(_ctx.effective_task_id)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _rep_response,
                        "messages": _ctx.messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": _rep_error,
                    })

                if agent.api_mode in {"chat_completions", "bedrock_converse", "anthropic_messages"}:
                    assistant_message = _trunc_msg
                    # ── Content-filter stream stall → fallback (#32421) ──
                    # When the provider's output-layer safety filter (e.g.
                    # MiniMax "output new_sensitive (1027)", Azure
                    # content_filter) kills the stream mid-delivery, the
                    # raw error was classified at the swallow point and the
                    # stub tagged ``_content_filter_terminated``.  This
                    # filter is content-deterministic — continuation
                    # retries against the SAME primary just re-hit it and
                    # burn paid attempts (the loop used to give up with
                    # "Response remained truncated after 3 continuation
                    # attempts" and never consult the fallback chain).
                    # Escalate to the configured fallback BEFORE retrying.
                    _cf_terminated = getattr(
                        response, "_content_filter_terminated", False
                    )
                    if (
                        _cf_terminated
                        and agent._fallback_index < len(agent._fallback_chain)
                    ):
                        agent._vprint(
                            f"{agent.log_prefix}🛡️  Content filter terminated "
                            f"stream — activating fallback provider...",
                            force=True,
                        )
                        agent._emit_status(
                            "Content filter terminated stream; switching to fallback..."
                        )
                        if agent._try_activate_fallback():
                            # Roll the partial content (if any was already
                            # appended in a prior continuation pass) back to
                            # the last clean turn so the fallback provider
                            # gets a coherent continuation point.
                            if _continuation.parts:
                                _ctx.messages = agent._get_messages_up_to_last_assistant(_ctx.messages)
                            # Unmark survivors: their text left the stitched partial.
                            for _frag in _ctx.messages:
                                if isinstance(_frag, dict):
                                    _frag.pop("_length_continuation_fragment", None)
                                    _frag.pop("_length_continuation_nudge", None)
                            agent._session_messages = _ctx.messages
                            _continuation.length_retries = 0
                            _continuation.parts = []
                            _cycle.retry_count = 0
                            _recovery.compression_attempts = 0
                            _cycle.recovery.primary_recovery_attempted = False
                            _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                            break
                        # No fallback available — fall through to normal
                        # continuation (best-effort, may loop).
                        agent._vprint(
                            f"{agent.log_prefix}⚠️  No fallback provider "
                            f"configured — retrying with same provider "
                            f"(may re-hit filter)...",
                            force=True,
                        )

                    # ── Transport interruption ≠ output truncation ──
                    # The stream died mid-body (peer closed / incomplete
                    # chunked read / SSE stopped before its terminator).
                    # The provider never reported an output cap, so this
                    # must NOT inherit the genuine-length budget: 4
                    # continuation nudges (or 4 tool-call retries with a
                    # doubling max_tokens), each itself a fresh request
                    # that can drop again.  One drop gets ONE bounded
                    # strategy change — streaming → non-streaming — then
                    # success, fallback, or an honest terminal result.
                    # Content-filter stalls are checked ABOVE and keep
                    # their own fallback-first semantics.
                    if _loop.is_transport_interrupted(response):
                        _tr_visible = _loop.transport_had_visible_text(
                            response, fallback_content=_trunc_content,
                        )
                        _tr_plan = _loop.plan_transport_recovery(
                            state=_recovery.transport,
                            # A continuation needs something to continue
                            # FROM: without a normalized assistant message
                            # there is no checkpoint to append, so fall
                            # back to replaying the request.
                            has_visible_text=(
                                _tr_visible and assistant_message is not None
                            ),
                        )
                        _tr_dropped_tools = getattr(
                            response, "_dropped_tool_names", None
                        )
                        # One structured line per logical interruption —
                        # never a "retrying 1/4 … 4/4" ladder.  Shapes and
                        # counts only; no prompt, credential, or tool-
                        # argument content.
                        _loop.logger.warning(
                            "%stransport interrupted: visible_partial=%s "
                            "partial_tool_call=%s recovery=%s attempt=%s "
                            "provider=%s model=%s",
                            agent.log_prefix,
                            _tr_visible,
                            bool(_tr_dropped_tools or _trunc_has_tool_calls),
                            _tr_plan.action.value,
                            "1/1" if _tr_plan.force_nonstreaming else "spent",
                            agent.provider,
                            agent.model,
                        )

                        if _tr_plan.action is _loop.TransportRecoveryAction.NONSTREAM_RETRY:
                            # CASE A/C: nothing the user has seen, and any
                            # partially-received tool call is discarded
                            # unexecuted (the stub never carries runnable
                            # tool_calls).  Re-issue the SAME logical
                            # request with no synthetic prompt appended,
                            # and no output-budget escalation — a dropped
                            # connection is not an output cap.
                            _recovery.transport = _tr_plan.next_state
                            agent._ephemeral_max_output_tokens = None
                            agent._buffer_vprint(
                                "⚠️  Stream interrupted — retrying once "
                                "without streaming..."
                            )
                            agent._emit_status(
                                "Stream interrupted; retrying without streaming..."
                            )
                            _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.TRANSPORT_RETRY)
                            continue

                        if _tr_plan.action is _loop.TransportRecoveryAction.PARTIAL_CONTINUATION:
                            # CASE B: model text already reached the user.
                            # Replaying the request would show it twice,
                            # so checkpoint what arrived and ask for
                            # exactly ONE non-streaming continuation.
                            _recovery.transport = _tr_plan.next_state
                            interim_msg = agent._build_assistant_message(
                                assistant_message, finish_reason,
                            )
                            interim_msg["_length_continuation_fragment"] = True
                            _loop.append_message(_ctx.messages, interim_msg)
                            if getattr(assistant_message, "content", None):
                                _continuation.parts.append(
                                    assistant_message.content
                                )
                            _loop.append_message(_ctx.messages, {
                                "role": "user",
                                "content": _loop._get_continuation_prompt(
                                    True, _tr_dropped_tools,
                                ),
                                "_length_continuation_nudge": True,
                            })
                            agent._session_messages = _ctx.messages
                            agent._vprint(
                                f"{agent.log_prefix}↻ Stream interrupted "
                                f"after partial output — one continuation "
                                f"(non-streaming)..."
                            )
                            _cycle.restart(_turn_controller, _loop.TurnReason.TRANSPORT_PARTIAL)
                            break

                        # EXHAUSTED — the bounded recovery already ran and
                        # the connection dropped again.  Never loop back
                        # into streaming; hand off or stop honestly.
                        _recovery.transport = _tr_plan.next_state
                        if agent._has_pending_fallback():
                            agent._buffer_status(
                                "⚠️ Stream kept dropping — trying fallback..."
                            )
                        if agent._try_activate_fallback():
                            # Roll the checkpointed partial back to the last
                            # clean turn so the fallback provider starts from
                            # a coherent point, mirroring the content-filter
                            # escalation above.
                            if _continuation.parts:
                                _ctx.messages = agent._get_messages_up_to_last_assistant(_ctx.messages)
                            # Rolling back to the last clean turn drops the
                            # scaffolding rows only when there WAS a partial
                            # to roll back past.  Strip them explicitly too,
                            # so a fallback can never inherit a synthetic
                            # "continue" row and re-answer a dead response.
                            _loop._strip_continuation_scaffold(
                                _ctx.messages, _ctx.current_turn_user_idx,
                            )
                            for _frag in _ctx.messages:
                                if isinstance(_frag, dict):
                                    _frag.pop("_length_continuation_fragment", None)
                                    _frag.pop("_length_continuation_nudge", None)
                            agent._session_messages = _ctx.messages
                            _continuation.parts = []
                            _continuation.length_retries = 0
                            _recovery.transport = _loop.TransportRecoveryState.NONE
                            _ctx.active_system_prompt = _loop._sync_failover_system_message(
                                agent, api_messages, _ctx.active_system_prompt)
                            _cycle.retry_count = 0
                            _recovery.compression_attempts = 0
                            _cycle.recovery.primary_recovery_attempted = False
                            _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                            break

                        agent._vprint(
                            f"{agent.log_prefix}❌ Stream interrupted again "
                            f"after the non-streaming retry — stopping "
                            f"instead of retrying further.",
                            force=True,
                        )
                        return _loop._complete_direct_turn(_turn_controller, _loop._transport_exhausted_result(
                            agent,
                            messages=_ctx.messages,
                            conversation_history=_ctx.conversation_history,
                            truncated_response_parts=_continuation.parts,
                            current_turn_user_idx=_ctx.current_turn_user_idx,
                            api_call_count=api_call_count,
                            effective_task_id=_ctx.effective_task_id,
                            error_text=(
                                "Stream connection dropped again after a "
                                "non-streaming retry"
                            ),
                        ))

                    if assistant_message is not None and not _trunc_has_tool_calls:
                        _continuation.length_retries += 1
                        # An EMPTY partial-stream stub (stream dropped
                        # mid tool-call before any text was delivered)
                        # must not be appended as an interim assistant
                        # message: it would serialize as
                        # {"role": "assistant", "content": ""}, and
                        # strict providers (Moonshot/Kimi via OpenRouter)
                        # reject empty assistant content with HTTP 400
                        # ("message ... with role 'assistant' must not be
                        # empty") on the very next replay — permanently
                        # poisoning the session history.  There is no
                        # partial text to continue from anyway, so only
                        # the continuation user-message is appended.
                        _is_empty_partial_stub = (
                            getattr(response, "id", "") == _loop.PARTIAL_STREAM_STUB_ID
                            and not getattr(assistant_message, "content", None)
                        )
                        if not _is_empty_partial_stub:
                            interim_msg = agent._build_assistant_message(assistant_message, finish_reason)
                            # Marked so the ceiling exit can drop the fragment trail.
                            interim_msg["_length_continuation_fragment"] = True
                            _loop.append_message(_ctx.messages, interim_msg)
                            if assistant_message.content:
                                _continuation.parts.append(assistant_message.content)

                        if _continuation.length_retries < 4:
                            _is_partial_stream_stub = (
                                getattr(response, "id", "") == _loop.PARTIAL_STREAM_STUB_ID
                            )
                            _dropped_tools = getattr(
                                response, "_dropped_tool_names", None
                            )

                            if _is_partial_stream_stub and _dropped_tools:
                                _tool_list = ", ".join(_dropped_tools[:3])
                                agent._vprint(
                                    f"{agent.log_prefix}↻ Stream interrupted mid "
                                    f"tool-call ({_tool_list}) — requesting "
                                    f"chunked retry "
                                    f"({_continuation.length_retries}/4)..."
                                )
                            elif _is_partial_stream_stub:
                                agent._vprint(
                                    f"{agent.log_prefix}↻ Stream interrupted — "
                                    f"requesting continuation "
                                    f"({_continuation.length_retries}/4)..."
                                )
                            else:
                                agent._vprint(
                                    f"{agent.log_prefix}↻ Requesting continuation "
                                    f"({_continuation.length_retries}/4)..."
                                )

                            _continue_content = _loop._get_continuation_prompt(
                                _is_partial_stream_stub, _dropped_tools
                            )
                            continue_msg = {
                                "role": "user",
                                "content": _continue_content,
                                "_length_continuation_nudge": True,
                            }
                            _loop.append_message(_ctx.messages, continue_msg)
                            agent._session_messages = _ctx.messages
                            _cycle.restart(_turn_controller, _loop.TurnReason.LENGTH)
                            break

                        partial_response = agent._strip_think_blocks(_loop._join_truncated_parts(_continuation.parts)).strip()
                        if partial_response:
                            agent._vprint(
                                f"{agent.log_prefix}⚠️  Response still truncated "
                                f"after 4 continuation attempts — keeping the "
                                f"partial response received so far.",
                                force=True,
                            )
                        # Unanswered continue nudges made every later turn re-truncate.
                        _turn_start = (
                            _ctx.current_turn_user_idx + 1
                            if isinstance(_ctx.current_turn_user_idx, int)
                            and _ctx.current_turn_user_idx >= 0
                            else 0
                        )
                        _ctx.messages[_turn_start:] = [
                            m for m in _ctx.messages[_turn_start:]
                            if not (
                                isinstance(m, dict)
                                and (
                                    m.get("_length_continuation_fragment")
                                    or m.get("_length_continuation_nudge")
                                )
                            )
                        ]
                        if partial_response:
                            _loop.append_message(_ctx.messages, {
                                "role": "assistant",
                                "content": partial_response,
                                "finish_reason": "length",
                            })
                        agent._session_messages = _ctx.messages
                        agent._cleanup_task_resources(_ctx.effective_task_id)
                        agent._persist_session(_ctx.messages, _ctx.conversation_history)
                        return _loop._complete_direct_turn(_turn_controller, {
                            "final_response": partial_response or None,
                            "messages": _ctx.messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": "Response remained truncated after 4 continuation attempts",
                        })

                if agent.api_mode in {"chat_completions", "bedrock_converse", "anthropic_messages"}:
                    assistant_message = _trunc_msg
                    if assistant_message is not None and _trunc_has_tool_calls:
                        _is_stub_stall = (
                            getattr(response, "id", "") == _loop.PARTIAL_STREAM_STUB_ID
                        )
                        if _continuation.truncated_tool_retries < 4:
                            _continuation.truncated_tool_retries += 1
                            if _is_stub_stall:
                                # The stream broke mid tool-call (network /
                                # peer-closed connection), not a real output
                                # cap — say so instead of "max output tokens".
                                agent._buffer_vprint(
                                    f"⚠️  Stream interrupted mid tool-call — "
                                    f"retrying ({_continuation.truncated_tool_retries}/4)..."
                                )
                            else:
                                agent._buffer_vprint(
                                    f"⚠️  Truncated tool call detected — "
                                    f"retrying API call "
                                    f"({_continuation.truncated_tool_retries}/4)..."
                                )
                            # Boost max_tokens on each retry so the model has
                            # more room to complete the tool-call JSON. A
                            # network stall doesn't need a bigger budget, but
                            # a genuine output-cap truncation does, and the
                            # boost is harmless for the stall case.
                            agent._ephemeral_max_output_tokens = _continuation.output_cap(
                                agent, api_kwargs, _continuation.truncated_tool_retries,
                            )
                            # Don't append the broken response to messages;
                            # just re-run the same API call from the current
                            # message state, giving the model another chance.
                            _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.TRUNCATED_TOOL_CALL)
                            continue
                        agent._flush_status_buffer()
                        if _is_stub_stall:
                            agent._vprint(
                                f"{agent.log_prefix}⚠️  Stream kept dropping mid tool-call after 4 retries — the action was not executed.",
                                force=True,
                            )
                        else:
                            agent._vprint(
                                f"{agent.log_prefix}⚠️  Truncated tool call response detected again — refusing to execute incomplete tool arguments.",
                                force=True,
                            )
                        agent._cleanup_task_resources(_ctx.effective_task_id)
                        _final_response = (
                            "Stream repeatedly dropped mid tool-call (network); "
                            "the tool was not executed"
                            if _is_stub_stall
                            else "Response truncated due to output length limit"
                        )
                        # Prior successful tool batches (or injected tool
                        # errors) can leave a tool-result tail; this path
                        # never reaches finalize_turn (#48879 class).
                        _loop.close_interrupted_tool_sequence(_ctx.messages, _final_response)
                        agent._persist_session(_ctx.messages, _ctx.conversation_history)
                        return _loop._complete_direct_turn(_turn_controller, {
                            "final_response": _final_response,
                            "messages": _ctx.messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": _final_response,
                        })

                # If we have prior messages, roll back to last complete state
                if len(_ctx.messages) > 1:
                    agent._vprint(f"{agent.log_prefix}   ⏪ Rolling back to last complete assistant turn")
                    rolled_back_messages = agent._get_messages_up_to_last_assistant(_ctx.messages)

                    agent._cleanup_task_resources(_ctx.effective_task_id)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)

                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": "Response truncated due to output length limit",
                        "messages": rolled_back_messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Response truncated due to output length limit"
                    })
                else:
                    # First message was truncated - mark as failed
                    agent._flush_status_buffer()
                    agent._vprint(f"{agent.log_prefix}❌ First response truncated - cannot recover", force=True)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": "First response truncated due to output length limit",
                        "messages": _ctx.messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "failed": True,
                        "error": "First response truncated due to output length limit"
                    })

            # Track actual token usage from response for context management
            if hasattr(response, 'usage') and response.usage:
                canonical_usage = _loop.normalize_usage(
                    response.usage,
                    provider=agent.provider,
                    api_mode=agent.api_mode,
                )
                # Aggregator-only usage is retained for cost pricing: MoA
                # advisor tokens must be priced at each advisor's OWN model
                # rate, not the aggregator's, so they are added as dollars
                # (below) rather than folded into the priced usage.
                aggregator_usage = canonical_usage
                # MoA: fold the reference (advisor) fan-out's token usage
                # into this turn's REPORTED token counts. MoA runs advisors
                # before the aggregator and returns only the aggregator's
                # usage, so without this the entire advisor spend — usually
                # the bulk of a MoA turn — is invisible in token counts.
                _moa_ref_cost = None
                _moa_client = getattr(agent, "client", None)
                if _moa_client is not None and hasattr(_moa_client, "consume_reference_usage"):
                    try:
                        _ref_usage, _moa_ref_cost = _moa_client.consume_reference_usage()
                        if _ref_usage is not None:
                            canonical_usage = canonical_usage + _ref_usage
                    except Exception as _moa_acct_exc:  # pragma: no cover - defensive
                        _loop.logger.debug("MoA reference usage accounting failed: %s", _moa_acct_exc)
                # Flush the full-turn MoA trace (references + aggregator I/O)
                # to disk when moa.save_traces is on. No-op otherwise and
                # for non-MoA clients. Uses the live session_id so traces
                # land in the right per-session file. On the streaming path
                # the aggregator's output wasn't captured inline (its raw
                # token stream went to the live consumer), so pass the
                # resolved streamed acting text as a fallback — makes the
                # trace self-contained instead of only pointing at state.db.
                if _moa_client is not None and hasattr(_moa_client, "consume_and_save_trace"):
                    try:
                        _agg_streamed_text = (
                            getattr(agent, "_current_streamed_assistant_text", "") or ""
                        )
                        _moa_client.consume_and_save_trace(
                            agent.session_id,
                            aggregator_output_fallback=_agg_streamed_text or None,
                        )
                    except Exception as _moa_trace_exc:  # pragma: no cover - defensive
                        _loop.logger.debug("MoA trace flush failed: %s", _moa_trace_exc)
                prompt_tokens = canonical_usage.prompt_tokens
                completion_tokens = canonical_usage.output_tokens
                total_tokens = canonical_usage.total_tokens
                # Forward canonical token + cache buckets so context engines
                # can make decisions on cache hit ratios / reasoning costs,
                # not just legacy aggregate tokens. Legacy keys stay for
                # back-compat with engines that only read prompt/completion/total.
                usage_dict = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "input_tokens": canonical_usage.input_tokens,
                    "output_tokens": canonical_usage.output_tokens,
                    "cache_read_tokens": canonical_usage.cache_read_tokens,
                    "cache_write_tokens": canonical_usage.cache_write_tokens,
                    "reasoning_tokens": canonical_usage.reasoning_tokens,
                }
                # Capture the boundary latch before update_from_response()
                # consumes it. Only a real provider prompt count for the
                # request immediately following a completed compaction can
                # prove that attempt effective and rearm the shared budget.
                _completed_compaction_pending = bool(
                    getattr(
                        agent.context_compressor,
                        "_verify_compaction_cleared_threshold",
                        False,
                    )
                )
                agent.context_compressor.update_from_response(usage_dict)
                _compression_threshold = int(
                    getattr(agent.context_compressor, "threshold_tokens", 0)
                    or 0
                )
                if _loop._should_rearm_compression_budget(
                    _recovery.compression_attempts,
                    completed_compaction_pending=_completed_compaction_pending,
                    prompt_tokens=prompt_tokens,
                    threshold_tokens=_compression_threshold,
                ):
                    _loop.logger.info(
                        "Compression budget rearmed after provider-confirmed "
                        "recovery: prompt=%s < threshold=%s (attempts were %s/%s)",
                        f"{prompt_tokens:,}",
                        f"{_compression_threshold:,}",
                        _recovery.compression_attempts,
                        _recovery.max_compression_attempts,
                    )
                    _recovery.compression_attempts = 0
                    # Provider-confirmed recovery also invalidates the
                    # insufficient-progress preflight state: with the
                    # prompt proven back below the threshold, a prior
                    # "insufficient progress" verdict (and the stale
                    # pressure reading it would be compared against)
                    # describes a request shape that no longer exists.
                    # Left armed, _preflight_compression_blocked keeps the
                    # pre-API gate dark for the rest of the turn even
                    # though the attempt budget was just rearmed, so a
                    # later pressure spike would grow unchecked until the
                    # provider's overflow handler fired.
                    _ctx.preflight_compression_blocked = False
                    _recovery.last_preflight_pressure = None

                # Stash this response's canonical usage so the post-turn
                # on_turn_complete() observation hook can forward it (the
                # same dict shape passed to update_from_response). A turn
                # may make several API calls; the engine's per-turn signal
                # of interest is the cost/size of the latest assembled
                # request, so we keep the most recent call's usage.
                agent._last_turn_usage = dict(usage_dict)
            elif getattr(
                agent.context_compressor,
                "awaiting_real_usage_after_compression",
                False,
            ):
                # A response with no usage cannot adjudicate whether the
                # prior compaction cleared the threshold. Consume the pending
                # verdict now so a much later, unrelated reading is not
                # charged to that old compaction, and so preflight deferral
                # does not remain latched indefinitely.
                agent.context_compressor.update_from_response({})

            if hasattr(response, 'usage') and response.usage:
                # Cache discovered context length after successful call.
                # Only persist limits confirmed by the provider (parsed
                # from the error message), not guessed probe tiers.
                if getattr(agent.context_compressor, "_context_probed", False):
                    ctx = agent.context_compressor.context_length
                    if getattr(agent.context_compressor, "_context_probe_persistable", False):
                        _loop.save_context_length(agent.model, agent.base_url, ctx)
                        agent._safe_print(f"{agent.log_prefix}💾 Cached context length: {ctx:,} tokens for {agent.model}")
                    agent.context_compressor._context_probed = False
                    agent.context_compressor._context_probe_persistable = False

                agent.session_prompt_tokens += prompt_tokens
                agent.session_completion_tokens += completion_tokens
                agent.session_total_tokens += total_tokens
                agent.session_api_calls += 1
                agent.session_input_tokens += canonical_usage.input_tokens
                agent.session_output_tokens += canonical_usage.output_tokens
                agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
                agent.session_cache_write_tokens += canonical_usage.cache_write_tokens
                agent.session_reasoning_tokens += canonical_usage.reasoning_tokens

                # Log API call details for debugging/observability
                _cache_pct = ""
                if canonical_usage.cache_read_tokens and prompt_tokens:
                    _cache_pct = f" cache={canonical_usage.cache_read_tokens}/{prompt_tokens} ({100*canonical_usage.cache_read_tokens/prompt_tokens:.0f}%)"
                _loop.logger.info(
                    "API call #%d: model=%s provider=%s in=%d out=%d total=%d latency=%.1fs%s",
                    agent.session_api_calls, agent.model, agent.provider or "unknown",
                    prompt_tokens, completion_tokens, total_tokens,
                    api_duration, _cache_pct,
                )

                # On the MoA path, agent.model/provider are the virtual
                # preset name ("closed") and "moa", which have no pricing
                # entry — estimating against them returns None and silently
                # drops the aggregator's own spend, leaving the session cost
                # as advisor-fan-out only (a ~50% undercount when the
                # aggregator does the full acting loop). Price the aggregator
                # turn at its REAL model/provider, read from the MoA client's
                # resolved aggregator slot.
                _agg_cost_model = agent.model
                _agg_cost_provider = agent.provider
                _agg_cost_base_url = agent.base_url
                _agg_slot = getattr(_moa_client, "last_aggregator_slot", None) if _moa_client is not None else None
                if _agg_slot and _agg_slot.get("model"):
                    _agg_cost_model = _agg_slot["model"]
                    _agg_cost_provider = _agg_slot.get("provider") or agent.provider
                    _agg_cost_base_url = _agg_slot.get("base_url") or agent.base_url
                cost_result = _loop.estimate_usage_cost(
                    _agg_cost_model,
                    aggregator_usage,
                    provider=_agg_cost_provider,
                    base_url=_agg_cost_base_url,
                    api_key=getattr(agent, "api_key", ""),
                )
                if cost_result.amount_usd is not None:
                    agent.session_estimated_cost_usd += float(cost_result.amount_usd)
                # Add MoA advisor cost (already priced per-advisor at each
                # advisor's own model rate) on top of the aggregator cost.
                if _moa_ref_cost is not None:
                    try:
                        agent.session_estimated_cost_usd += float(_moa_ref_cost)
                    except (TypeError, ValueError):  # pragma: no cover - defensive
                        pass
                agent.session_cost_status = cost_result.status
                agent.session_cost_source = cost_result.source

                # Persist token counts to session DB for /insights.
                # Do this for every platform with a session_id so non-CLI
                # sessions (gateway, cron, delegated runs) cannot lose
                # token/accounting data if a higher-level persistence path
                # is skipped or fails. Gateway/session-store writes use
                # absolute totals, so they safely overwrite these per-call
                # deltas instead of double-counting them.
                if agent._session_db and agent.session_id:
                    try:
                        # Ensure the session row exists before attempting UPDATE.
                        # Under concurrent load (cron/kanban), the initial
                        # _ensure_db_session() may have failed due to SQLite
                        # locking.  Retry here so per-call token deltas are
                        # not silently lost (UPDATE on a non-existent row
                        # affects 0 rows without error).
                        if not agent._session_db_created:
                            agent._ensure_db_session()
                        # Per-call cost delta = aggregator cost + MoA
                        # advisor cost (each priced at its own rate). Folded
                        # here so state.db's estimated_cost_usd includes the
                        # full MoA spend, matching the folded token counts.
                        _cost_delta = None
                        if cost_result.amount_usd is not None:
                            _cost_delta = float(cost_result.amount_usd)
                        if _moa_ref_cost is not None:
                            try:
                                _cost_delta = (_cost_delta or 0.0) + float(_moa_ref_cost)
                            except (TypeError, ValueError):  # pragma: no cover
                                pass
                        # Enqueued, not written: the background writer
                        # applies the delta off the turn thread (a cold
                        # state.db UPDATE here stalled the tool loop for
                        # up to hundreds of ms per API call). Drained at
                        # turn finalize via _persist_session.
                        agent._session_db.queue_token_counts(
                            agent.session_id,
                            input_tokens=canonical_usage.input_tokens,
                            output_tokens=canonical_usage.output_tokens,
                            cache_read_tokens=canonical_usage.cache_read_tokens,
                            cache_write_tokens=canonical_usage.cache_write_tokens,
                            reasoning_tokens=canonical_usage.reasoning_tokens,
                            estimated_cost_usd=_cost_delta,
                            cost_status=cost_result.status,
                            cost_source=cost_result.source,
                            billing_provider=agent.provider,
                            billing_base_url=agent.base_url,
                            billing_mode="subscription_included"
                            if cost_result.status == "included" else None,
                            model=agent.model,
                            api_call_count=1,
                        )
                    except Exception as e:
                        # Log token persistence failures so they're
                        # visible in agent.log — silent loss here is
                        # the root cause of undercounted analytics.
                        _loop.logger.debug(
                            "Token persistence failed (session=%s, tokens=%d): %s",
                            agent.session_id, total_tokens, e,
                        )

                if agent.verbose_logging:
                    _loop.logging.debug(f"Token usage: prompt={usage_dict['prompt_tokens']:,}, completion={usage_dict['completion_tokens']:,}, total={usage_dict['total_tokens']:,}")

                # Surface cache hit stats for any provider that reports
                # them — not just those where we inject cache_control
                # markers.  OpenAI/Kimi/DeepSeek/Qwen all do automatic
                # server-side prefix caching and return
                # ``prompt_tokens_details.cached_tokens``; users
                # previously could not see their cache % because this
                # line was gated on ``_use_prompt_caching``, which is
                # only True for Anthropic-style marker injection.
                # ``canonical_usage`` is already normalised from all
                # three API shapes (Anthropic / Codex / OpenAI-chat)
                # so we can rely on its values directly.
                cached = canonical_usage.cache_read_tokens
                written = canonical_usage.cache_write_tokens
                prompt = usage_dict["prompt_tokens"]
                if (cached or written) and not agent.quiet_mode:
                    hit_pct = (cached / prompt * 100) if prompt > 0 else 0
                    agent._vprint(
                        f"{agent.log_prefix}   💾 Cache: "
                        f"{cached:,}/{prompt:,} tokens "
                        f"({hit_pct:.0f}% hit, {written:,} written)"
                    )

            _cycle.recovery.has_retried_429 = False  # Reset on success
            # Note: don't clear the retry buffer here — an "API call
            # success" only means we got bytes back, not that we got
            # usable content. Empty responses still loop through the
            # empty-retry path below; the buffer is cleared when
            # genuinely successful content is detected later (~L4127).
            # Clear Nous rate limit state on successful request —
            # proves the limit has reset and other sessions can
            # resume hitting Nous.
            if agent.provider == "nous":
                try:
                    from agent.nous_rate_guard import clear_nous_rate_limit
                    clear_nous_rate_limit()
                except Exception:
                    pass
            from agent import relay_llm

            relay_llm.complete_logical_call(
                api_request_id,
                outcome="success",
            )
            agent._touch_activity(f"API call #{api_call_count} completed")
            break  # Success, exit retry loop

        except InterruptedError:
            if thinking_spinner:
                thinking_spinner.stop("")
                thinking_spinner = None
            if agent.thinking_callback:
                agent.thinking_callback("")
            if agent._has_pending_redirect():
                # redirect() deliberately used the interrupt machinery to
                # cancel only this provider request. Keep its correction
                # queued, clear the cancellation bit, and let the outer
                # loop rebuild a clean request tail. Never materialize
                # incomplete signed/encrypted reasoning items.
                if agent.clear_interrupt(preserve_redirect=True):
                    _cycle.restart(_turn_controller, _loop.TurnReason.REDIRECT)
                    break
            api_elapsed = _loop.time.time() - api_start_time
            agent._vprint(f"{agent.log_prefix}⚡ Interrupted during API call.", force=True)
            interrupted = True
            # Preserve any assistant text already streamed to the user
            # before the stop landed. Dropping it leaves history with no
            # record of the half-finished reply on screen, so the next turn
            # the model "forgets" what it just said — exactly what users hit
            # when they stop to redirect mid-response.
            _partial = agent._strip_think_blocks(
                getattr(agent, "_current_streamed_assistant_text", "") or ""
            ).strip()
            if _partial:
                _loop.append_message(_ctx.messages, {"role": "assistant", "content": _partial})
                final_response = _partial
            else:
                final_response = f"{_loop.INTERRUPT_WAITING_FOR_MODEL_PREFIX}{api_elapsed:.1f}s elapsed)."
            agent._persist_session(_ctx.messages, _ctx.conversation_history)
            break

        except Exception as api_error:
            # Stop spinner silently — retry status is buffered and
            # only flushed when every retry+fallback is exhausted.
            if thinking_spinner:
                thinking_spinner.stop("")
                thinking_spinner = None
            if agent.thinking_callback:
                agent.thinking_callback("")

            # -----------------------------------------------------------
            # UnicodeEncodeError recovery.  Two common causes:
            #   1. Lone surrogates (U+D800..U+DFFF) from clipboard paste
            #      (Google Docs, rich-text editors) — sanitize and retry.
            #   2. ASCII codec on systems with LANG=C or non-UTF-8 locale
            #      (e.g. Chromebooks) — any non-ASCII character fails.
            #      Detect via the error message mentioning 'ascii' codec.
            # We sanitize messages in-place and may retry twice:
            # first to strip surrogates, then once more for pure
            # ASCII-only locale sanitization if needed.
            # -----------------------------------------------------------
            if isinstance(api_error, UnicodeEncodeError) and getattr(agent, '_unicode_sanitization_passes', 0) < 2:
                _err_str = str(api_error).lower()
                _is_ascii_codec = "'ascii'" in _err_str or "ascii" in _err_str
                # Detect surrogate errors — utf-8 codec refusing to
                # encode U+D800..U+DFFF.  The error text is:
                #   "'utf-8' codec can't encode characters in position
                #    N-M: surrogates not allowed"
                _is_surrogate_error = (
                    "surrogate" in _err_str
                    or ("'utf-8'" in _err_str and not _is_ascii_codec)
                )
                # Sanitize surrogates from both the canonical `messages`
                # list AND `api_messages` (the API-copy, which may carry
                # `reasoning_content`/`reasoning_details` transformed
                # from `reasoning` — fields the canonical list doesn't
                # have directly).  Also clean `api_kwargs` if built and
                # `prefill_messages` if present.  Mirrors the ASCII
                # codec recovery below.
                _surrogates_found = _loop._sanitize_messages_surrogates(_ctx.messages)
                if isinstance(api_messages, list):
                    if _loop._sanitize_messages_surrogates(api_messages):
                        _surrogates_found = True
                if isinstance(api_kwargs, dict):
                    if _loop._sanitize_structure_surrogates(api_kwargs):
                        _surrogates_found = True
                if isinstance(getattr(agent, "prefill_messages", None), list):
                    if _loop._sanitize_messages_surrogates(agent.prefill_messages):
                        _surrogates_found = True
                # Gate the retry on the error type, not on whether we
                # found anything — _force_ascii_payload / the extended
                # surrogate walker above cover all known paths, but a
                # new transformed field could still slip through.  If
                # the error was a surrogate encode failure, always let
                # the retry run; the proactive sanitizer at line ~8781
                # runs again on the next iteration.  Bounded by
                # _unicode_sanitization_passes < 2 (outer guard).
                if _surrogates_found or _is_surrogate_error:
                    agent._unicode_sanitization_passes += 1
                    if _surrogates_found:
                        agent._buffer_vprint(
                            "⚠️  Stripped invalid surrogate characters from messages. Retrying..."
                        )
                    else:
                        agent._buffer_vprint(
                            "⚠️  Surrogate encoding error — retrying after full-payload sanitization..."
                        )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue
                if _is_ascii_codec:
                    agent._force_ascii_payload = True
                    # ASCII codec: the system encoding can't handle
                    # non-ASCII characters at all. Sanitize all
                    # non-ASCII content from messages/tool schemas and retry.
                    # Sanitize both the canonical `messages` list and
                    # `api_messages` (the API-copy built before the retry
                    # loop, which may contain extra fields like
                    # reasoning_content that are not in `messages`).
                    _messages_sanitized = _loop._sanitize_messages_non_ascii(_ctx.messages)
                    if isinstance(api_messages, list):
                        _loop._sanitize_messages_non_ascii(api_messages)
                    # Also sanitize the last api_kwargs if already built,
                    # so a leftover non-ASCII value in a transformed field
                    # (e.g. extra_body, reasoning_content) doesn't survive
                    # into the next attempt via _build_api_kwargs cache paths.
                    if isinstance(api_kwargs, dict):
                        _loop._sanitize_structure_non_ascii(api_kwargs)
                    _prefill_sanitized = False
                    if isinstance(getattr(agent, "prefill_messages", None), list):
                        _prefill_sanitized = _loop._sanitize_messages_non_ascii(agent.prefill_messages)

                    _tools_sanitized = False
                    if isinstance(getattr(agent, "tools", None), list):
                        _tools_sanitized = _loop._sanitize_tools_non_ascii(agent.tools)

                    _system_sanitized = False
                    if isinstance(_ctx.active_system_prompt, str):
                        _sanitized_system = _loop._strip_non_ascii(_ctx.active_system_prompt)
                        if _sanitized_system != _ctx.active_system_prompt:
                            _ctx.active_system_prompt = _sanitized_system
                            agent._cached_system_prompt = _sanitized_system
                            _system_sanitized = True
                    if isinstance(getattr(agent, "ephemeral_system_prompt", None), str):
                        _sanitized_ephemeral = _loop._strip_non_ascii(agent.ephemeral_system_prompt)
                        if _sanitized_ephemeral != agent.ephemeral_system_prompt:
                            agent.ephemeral_system_prompt = _sanitized_ephemeral
                            _system_sanitized = True

                    _headers_sanitized = False
                    _default_headers = (
                        agent._client_kwargs.get("default_headers")
                        if isinstance(getattr(agent, "_client_kwargs", None), dict)
                        else None
                    )
                    if isinstance(_default_headers, dict):
                        _headers_sanitized = _loop._sanitize_structure_non_ascii(_default_headers)

                    # Sanitize the API key — non-ASCII characters in
                    # credentials (e.g. ʋ instead of v from a bad
                    # copy-paste) cause httpx to fail when encoding
                    # the Authorization header as ASCII.  This is the
                    # most common cause of persistent UnicodeEncodeError
                    # that survives message/tool sanitization (#6843).
                    _credential_sanitized = False
                    _raw_key = getattr(agent, "api_key", None) or ""
                    # Entra ID bearer providers are callables — their
                    # minted JWTs are always ASCII, so no sanitization
                    # is needed (and ``_strip_non_ascii`` would crash
                    # on a callable input).
                    if _raw_key and isinstance(_raw_key, str):
                        _clean_key = _loop._strip_non_ascii(_raw_key)
                        if _clean_key != _raw_key:
                            agent.api_key = _clean_key
                            if isinstance(getattr(agent, "_client_kwargs", None), dict):
                                agent._client_kwargs["api_key"] = _clean_key
                            # Also update the live client — it holds its
                            # own copy of api_key which auth_headers reads
                            # dynamically on every request.
                            if getattr(agent, "client", None) is not None and hasattr(agent.client, "api_key"):
                                agent.client.api_key = _clean_key
                            _credential_sanitized = True
                            agent._vprint(
                                f"{agent.log_prefix}⚠️  API key contained non-ASCII characters "
                                f"(bad copy-paste?) — stripped them. If auth fails, "
                                f"re-copy the key from your provider's dashboard.",
                                force=True,
                            )

                    # Always retry on ASCII codec detection —
                    # _force_ascii_payload guarantees the full
                    # api_kwargs payload is sanitized on the
                    # next iteration (line ~8475).  Even when
                    # per-component checks above find nothing
                    # (e.g. non-ASCII only in api_messages'
                    # reasoning_content), the flag catches it.
                    # Bounded by _unicode_sanitization_passes < 2.
                    agent._unicode_sanitization_passes += 1
                    _any_sanitized = (
                        _messages_sanitized
                        or _prefill_sanitized
                        or _tools_sanitized
                        or _system_sanitized
                        or _headers_sanitized
                        or _credential_sanitized
                    )
                    if _any_sanitized:
                        agent._vprint(
                            f"{agent.log_prefix}⚠️  System encoding is ASCII — stripped non-ASCII characters from request payload. Retrying...",
                            force=True,
                        )
                    else:
                        agent._vprint(
                            f"{agent.log_prefix}⚠️  System encoding is ASCII — enabling full-payload sanitization for retry...",
                            force=True,
                        )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue

            # ── Image-rejection recovery ──────────────────────────────
            # Some providers (mlx-lm, text-only endpoints, text-only
            # fallbacks on multimodal models) reject any message that
            # contains image_url content with a 4xx error like
            # "Only 'text' content type is supported."  On first hit,
            # strip all images from the message list, mark the session
            # as vision-unsupported, and retry with text only.
            #
            # Detection is best-effort English phrase matching — a
            # locale-translated or heavily-reworded upstream error
            # will bypass this guard and fall through to the normal
            # error handler.  Expand the phrase list when new
            # provider wordings are observed in the wild.
            _err_body = ""
            try:
                _err_body = str(getattr(api_error, "body", None) or
                                getattr(api_error, "message", None) or
                                str(api_error))
            except Exception:
                pass
            _err_status = getattr(api_error, "status_code", None)
            _IMAGE_REJECTION_PHRASES = (
                "only 'text' content type is supported",
                "only text content type is supported",
                "image_url is not supported",
                "image content is not supported",
                "multimodal is not supported",
                "multimodal content is not supported",
                "multimodal input is not supported",
                "vision is not supported",
                "vision input is not supported",
                "does not support images",
                "does not support image input",
                "does not support multimodal",
                "does not support vision",
                "model does not support image",
                # ChatGPT-account Codex backend
                # (https://chatgpt.com/backend-api/codex) rejects
                # data:image/...base64 URLs in input_image fields
                # with HTTP 400 "Invalid 'input[N].content[K].image_url'.
                # Expected a valid URL, but got a value with an
                # invalid format." The OpenAI Responses API on the
                # public endpoint accepts data URLs, but the
                # ChatGPT-account variant does not. Without this
                # phrase the agent cascaded into compression /
                # context-too-large recovery instead of just
                # stripping the images. Match is narrow on
                # purpose — keyed on the field-path apostrophe so
                # we don't false-trip on other URL validation
                # errors. (issue #23570)
                "image_url'. expected",
                # DeepSeek's OpenAI-compatible API reports text-only
                # request-body variants as:
                # "unknown variant `image_url`, expected `text`".
                "unknown variant `image_url`, expected `text`",
                "unknown variant image_url, expected text",
                # OpenRouter routes a request to upstream endpoints and,
                # when none of the candidate endpoints for the model accept
                # image input, returns HTTP 404 "No endpoints found that
                # support image input". Without this phrase the agent never
                # strips the images, the retry loop re-sends the same
                # rejected request until exhaustion, and the gateway leaves
                # every subsequent message queued behind the stuck turn —
                # the P1 in issue #21160. The 404 passes the 4xx gate below.
                "no endpoints found that support image input",
            )
            _err_lower = _err_body.lower()
            _looks_like_image_rejection = any(
                p in _err_lower for p in _IMAGE_REJECTION_PHRASES
            )
            # 4xx-only gate: never interpret 5xx/timeout as "server
            # said no to images" — those are transient and must
            # route to the normal retry path.
            _status_ok = _err_status is None or (400 <= int(_err_status) < 500)
            if (
                getattr(agent, "_vision_supported", True)
                and _looks_like_image_rejection
                and _status_ok
            ):
                agent._vision_supported = False
                _imgs_removed = _loop._strip_images_from_messages(_ctx.messages)
                if isinstance(api_messages, list):
                    _loop._strip_images_from_messages(api_messages)
                agent._vprint(
                    f"{agent.log_prefix}⚠️  Server rejected image content — "
                    f"switching to text-only mode for this session"
                    + (". Stripped images from history and retrying." if _imgs_removed else "."),
                    force=True,
                )
                _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                continue

            # ── Bedrock AnthropicBedrock SDK streaming failure ──
            # The Anthropic SDK's stream accumulator raises RuntimeError
            # "Unexpected event order" when Bedrock returns an error event
            # before message_start (throttling, overload, validation).
            # Fall back to the native Converse API path for the rest of
            # this session — it handles these errors gracefully.  Ref: #28156.
            if (
                isinstance(api_error, RuntimeError)
                and "unexpected event order" in str(api_error).lower()
                and getattr(agent, "provider", "") == "bedrock"
                and agent.api_mode == "anthropic_messages"
                and not getattr(agent, "_bedrock_converse_fallback_attempted", False)
            ):
                agent._bedrock_converse_fallback_attempted = True
                agent.api_mode = "bedrock_converse"
                agent._bedrock_region = getattr(agent, "_bedrock_region", None) or "us-east-1"
                agent.client = None  # Drop the AnthropicBedrock client
                agent._client_kwargs = {}
                agent._vprint(
                    f"{agent.log_prefix}⚠️  AnthropicBedrock SDK streaming failed — "
                    f"falling back to native Converse API for this session.",
                    force=True,
                )
                _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PROVIDER_SWITCH)
                continue

            status_code = getattr(api_error, "status_code", None)
            error_context = agent._extract_api_error_context(api_error)

            # ── Classify the error for structured recovery decisions ──
            _compressor = getattr(agent, "context_compressor", None)
            _ctx_len = getattr(_compressor, "context_length", 200000) if _compressor else 200000
            classified = _loop.classify_api_error(
                api_error,
                provider=getattr(agent, "provider", "") or "",
                model=getattr(agent, "model", "") or "",
                approx_tokens=approx_tokens,
                context_length=_ctx_len,
                num_messages=len(api_messages) if api_messages else 0,
            )
            _loop.logger.debug(
                "Error classified: reason=%s status=%s retryable=%s compress=%s rotate=%s fallback=%s",
                classified.reason.value, classified.status_code,
                classified.retryable, classified.should_compress,
                classified.should_rotate_credential, classified.should_fallback,
            )
            agent._invoke_api_request_error_hook(
                task_id=_ctx.effective_task_id,
                turn_id=_ctx.turn_id,
                api_request_id=api_request_id,
                api_call_count=api_call_count,
                api_start_time=api_start_time,
                api_kwargs=api_kwargs,
                error_type=type(api_error).__name__,
                error_message=str(api_error),
                status_code=status_code,
                retry_count=_cycle.retry_count,
                max_retries=_cycle.max_retries,
                retryable=classified.retryable,
                reason=classified.reason.value,
            )

            # ── Raw transport drop: same incident, same owner ──────────
            # A stream that dies before any delta reaches the consumer
            # never becomes a PARTIAL_STREAM_STUB — it raises.  Without
            # this branch those drops (the COMMON shape) fall into the
            # generic retry machinery below: retry_count attempts, then
            # _try_recover_primary_transport resetting retry_count to 0
            # for another full cycle.  That is the multiplicative recovery
            # this work exists to remove, so the bounded policy has to own
            # raw exceptions and stub responses alike.
            #
            # Placed after the error hook (observability still fires for
            # every failure) but before ALL generic recovery, so a drop
            # this policy owns can never also be handled by that path.
            # Scope: P1 owns MID-STREAM interruption.  A timeout on a
            # plain non-streaming request is a different incident whose
            # established owner is _try_recover_primary_transport (stale
            # pool / client rebuild), so it keeps that path.  Once a
            # recovery IS in flight the incident stays owned here, which
            # is what makes the second failure terminal instead of
            # restarting the generic budget.
            _tr_owned_call = (
                _use_streaming
                or _recovery.transport is not _loop.TransportRecoveryState.NONE
            )
            if (
                _tr_owned_call
                and _loop.is_transport_retryable_error(classified)
                and not agent._interrupt_requested
            ):
                # An interrupt that closes the socket surfaces as a
                # transport error rather than InterruptedError; the guard
                # above keeps cancel from being answered with a new
                # model request.
                _raw_visible = bool(
                    (getattr(agent, "_current_streamed_assistant_text", "") or "").strip()
                )
                _raw_plan = _loop.plan_transport_recovery(
                    state=_recovery.transport,
                    has_visible_text=_raw_visible,
                )
                # One structured line per logical interruption, matching
                # the stub path's shape.  Counts and verdicts only — no
                # prompt, credential, or tool-argument content.
                _loop.logger.warning(
                    "%stransport interrupted (raw): visible_partial=%s "
                    "recovery=%s attempt=%s error_type=%s provider=%s "
                    "model=%s",
                    agent.log_prefix,
                    _raw_visible,
                    _raw_plan.action.value,
                    "1/1" if _raw_plan.force_nonstreaming else "spent",
                    type(api_error).__name__,
                    agent.provider,
                    agent.model,
                )
                if _raw_plan.action is _loop.TransportRecoveryAction.NONSTREAM_RETRY:
                    # CASE A/C.  Nothing the user saw, and any half-parsed
                    # tool call died with the socket — it was never
                    # executed, because tools only run after a stream
                    # completes.  Replay the SAME logical request without
                    # streaming: no synthetic prompt, and no output-budget
                    # escalation (a dropped connection is not an output
                    # cap).  retry_count is deliberately NOT incremented:
                    # this incident is owned here, not by that budget.
                    _recovery.transport = _raw_plan.next_state
                    agent._ephemeral_max_output_tokens = None
                    agent._buffer_vprint(
                        "⚠️  Stream interrupted — retrying once "
                        "without streaming..."
                    )
                    agent._emit_status(
                        "Stream interrupted; retrying without streaming..."
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.TRANSPORT_RETRY)
                    continue

                if _raw_plan.action is _loop.TransportRecoveryAction.PARTIAL_CONTINUATION:
                    # CASE B via a raw error: text already reached the
                    # user, so replaying would show it twice.  Checkpoint
                    # it and ask for exactly one non-streaming
                    # continuation.
                    _recovery.transport = _raw_plan.next_state
                    _raw_text = (
                        getattr(agent, "_current_streamed_assistant_text", "") or ""
                    )
                    _loop.append_message(_ctx.messages, {
                        "role": "assistant",
                        "content": _raw_text,
                        "finish_reason": "length",
                        "_length_continuation_fragment": True,
                    })
                    _continuation.parts.append(_raw_text)
                    _loop.append_message(_ctx.messages, {
                        "role": "user",
                        "content": _loop._get_continuation_prompt(True, None),
                        "_length_continuation_nudge": True,
                    })
                    agent._session_messages = _ctx.messages
                    agent._emit_status(
                        "Stream interrupted; continuing without streaming..."
                    )
                    _turn_controller.move(
                        _loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.TRANSPORT_PARTIAL,
                        durability=_loop.DurabilityBoundary.RECOVERY_SCAFFOLD,
                    )
                    continue

                # EXHAUSTED — the bounded attempt already ran and the
                # connection dropped again.  Never re-enter streaming and
                # never hand this incident to the generic retry budget.
                _recovery.transport = _raw_plan.next_state
                if agent._has_pending_fallback():
                    agent._buffer_status(
                        "⚠️ Stream kept dropping — trying fallback..."
                    )
                if agent._try_activate_fallback():
                    if _continuation.parts:
                        _ctx.messages = agent._get_messages_up_to_last_assistant(_ctx.messages)
                    _loop._strip_continuation_scaffold(_ctx.messages, _ctx.current_turn_user_idx)
                    for _frag in _ctx.messages:
                        if isinstance(_frag, dict):
                            _frag.pop("_length_continuation_fragment", None)
                            _frag.pop("_length_continuation_nudge", None)
                    agent._session_messages = _ctx.messages
                    _continuation.parts = []
                    _continuation.length_retries = 0
                    _recovery.transport = _loop.TransportRecoveryState.NONE
                    _ctx.active_system_prompt = _loop._sync_failover_system_message(
                        agent, api_messages, _ctx.active_system_prompt)
                    _cycle.retry_count = 0
                    _recovery.compression_attempts = 0
                    _cycle.recovery.primary_recovery_attempted = False
                    _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                    break
                agent._vprint(
                    f"{agent.log_prefix}❌ Stream interrupted again after "
                    f"the non-streaming retry — stopping instead of "
                    f"retrying further.",
                    force=True,
                )
                return _loop._complete_direct_turn(_turn_controller, _loop._transport_exhausted_result(
                    agent,
                    messages=_ctx.messages,
                    conversation_history=_ctx.conversation_history,
                    truncated_response_parts=_continuation.parts,
                    current_turn_user_idx=_ctx.current_turn_user_idx,
                    api_call_count=api_call_count,
                    effective_task_id=_ctx.effective_task_id,
                    error_text=(
                        "Stream connection dropped again after a "
                        "non-streaming retry"
                    ),
                ))

            if (
                classified.reason == _loop.FailoverReason.billing
                and _loop._is_nous_inference_route(
                    getattr(agent, "provider", "") or "",
                    getattr(agent, "base_url", "") or "",
                )
                and not _cycle.recovery.nous_paid_entitlement_refresh_attempted
            ):
                _cycle.recovery.nous_paid_entitlement_refresh_attempted = True
                if _loop._try_refresh_nous_paid_entitlement_credentials(agent):
                    agent._vprint(
                        f"{agent.log_prefix}🔐 Nous paid access verified — "
                        "refreshed runtime credentials and retrying request...",
                        force=True,
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                    continue

            recovered_with_pool, _cycle.recovery.has_retried_429 = agent._recover_with_credential_pool(
                status_code=status_code,
                has_retried_429=_cycle.recovery.has_retried_429,
                classified_reason=classified.reason,
                error_context=error_context,
                billing_unverified=classified.billing_unverified,
            )
            if recovered_with_pool:
                _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                continue

            # Image-too-large recovery: shrink oversized native image
            # parts in-place and retry once.  Triggered by Anthropic's
            # per-image 5 MB ceiling (400 with "image exceeds 5 MB
            # maximum") or any other provider that complains about
            # image size.  If shrink fails or a second attempt still
            # fails, fall through to normal error handling.
            if (
                classified.reason == _loop.FailoverReason.image_too_large
                and not _cycle.recovery.image_shrink_retry_attempted
            ):
                _cycle.recovery.image_shrink_retry_attempted = True
                image_max_dimension = _loop._image_error_max_dimension(api_error) or 8000
                if agent._try_shrink_image_parts_in_messages(
                    api_messages,
                    max_dimension=image_max_dimension,
                ):
                    agent._vprint(
                        f"{agent.log_prefix}📐 Image(s) exceeded provider size limit — "
                        f"shrank and retrying...",
                        force=True,
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue
                else:
                    _loop.logger.info(
                        "image-shrink recovery: no data-URL image parts found "
                        "or shrink didn't reduce size; surfacing original error."
                    )

            # Multimodal-tool-content recovery: providers that follow
            # the OpenAI spec strictly (tool message content must be a
            # string) reject our list-type content with a 400.  Strip
            # image parts from any list-type tool messages, mark the
            # (provider, model) as no-list-tool-content for the rest
            # of this session so future tool results preemptively
            # downgrade, and retry once.  See issue #27344.
            if (
                classified.reason == _loop.FailoverReason.multimodal_tool_content_unsupported
                and not _cycle.recovery.multimodal_tool_content_retry_attempted
            ):
                _cycle.recovery.multimodal_tool_content_retry_attempted = True
                if agent._try_strip_image_parts_from_tool_messages(api_messages):
                    agent._vprint(
                        f"{agent.log_prefix}📐 Provider rejected list-type tool content — "
                        f"downgraded screenshots to text and retrying...",
                        force=True,
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue
                else:
                    _loop.logger.info(
                        "multimodal-tool-content recovery: no list-type tool "
                        "messages with image parts found; surfacing original error."
                    )

            # Anthropic OAuth subscription rejected the 1M-context beta
            # header ("long context beta is not yet available for this
            # subscription"). Disable the beta for the rest of this
            # session, rebuild the client, and retry once.  1M-capable
            # subscriptions never hit this branch — they accept the
            # beta and keep full 1M context.  See PR #17680 for the
            # original report (we chose reactive recovery over the
            # proposed unconditional omit so capable subscriptions
            # don't silently lose the capability).
            if (
                classified.reason == _loop.FailoverReason.oauth_long_context_beta_forbidden
                and agent.api_mode == "anthropic_messages"
                and agent._is_anthropic_oauth
                and not _cycle.recovery.oauth_1m_beta_retry_attempted
            ):
                _cycle.recovery.oauth_1m_beta_retry_attempted = True
                if not getattr(agent, "_oauth_1m_beta_disabled", False):
                    agent._oauth_1m_beta_disabled = True
                    try:
                        agent._anthropic_client.close()
                    except Exception:
                        pass
                    agent._rebuild_anthropic_client()
                    agent._vprint(
                        f"{agent.log_prefix}🔕 OAuth subscription doesn't support "
                        f"the 1M-context beta — disabled for this session and retrying...",
                        force=True,
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue

            if (
                agent.api_mode == "codex_responses"
                and agent.provider in {"openai-codex", "xai-oauth"}
                and status_code == 401
                and not _cycle.recovery.codex_auth_retry_attempted
            ):
                _cycle.recovery.codex_auth_retry_attempted = True
                if agent._try_refresh_codex_client_credentials(force=True):
                    _label = "xAI OAuth" if agent.provider == "xai-oauth" else "Codex"
                    agent._buffer_vprint(f"🔐 {_label} auth refreshed after 401. Retrying request...")
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                    continue
            if (
                agent.api_mode == "chat_completions"
                and agent.provider == "vertex"
                and status_code == 401
                and not _cycle.recovery.vertex_auth_retry_attempted
            ):
                _cycle.recovery.vertex_auth_retry_attempted = True
                if agent._try_refresh_vertex_client_credentials():
                    agent._buffer_vprint("🔐 Vertex AI token refreshed after 401. Retrying request...")
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                    continue
            if (
                agent.api_mode in ("chat_completions", "anthropic_messages")
                and agent.provider == "nous"
                and status_code == 401
                and not _cycle.recovery.nous_auth_retry_attempted
            ):
                _cycle.recovery.nous_auth_retry_attempted = True
                if agent._try_refresh_nous_client_credentials(force=True):
                    print(f"{agent.log_prefix}🔐 Nous agent key refreshed after 401. Retrying request...")
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                    continue
                # Credential refresh didn't help — show diagnostic info.
                # Most common causes: Portal OAuth expired/revoked,
                # account out of credits, or agent key blocked.
                from hermes_constants import display_hermes_home as _dhh_fn
                _dhh = _dhh_fn()
                _body_text = ""
                try:
                    _body = getattr(api_error, "body", None) or getattr(api_error, "response", None)
                    if _body is not None:
                        _body_text = str(_body)[:200]
                except Exception:
                    pass
                print(f"{agent.log_prefix}🔐 Nous 401 — Portal authentication failed.")
                if _body_text:
                    print(f"{agent.log_prefix}   Response: {_body_text}")
                if not _loop._print_nous_entitlement_guidance(agent, "Nous model access"):
                    print(f"{agent.log_prefix}   Most likely: Portal OAuth expired, account out of credits, or agent key revoked.")
                print(f"{agent.log_prefix}   Troubleshooting:")
                print(f"{agent.log_prefix}     • Re-authenticate: hermes auth add nous")
                print(f"{agent.log_prefix}     • Check credits / billing: https://portal.nousresearch.com")
                print(f"{agent.log_prefix}     • Verify stored credentials: {_dhh}/auth.json")
                print(f"{agent.log_prefix}     • Switch providers temporarily: /model <model> --provider openrouter")
            if (
                _loop._is_copilot_provider(agent)
                and status_code == 401
                and not _cycle.recovery.copilot_auth_retry_attempted
            ):
                _cycle.recovery.copilot_auth_retry_attempted = True
                if agent._try_refresh_copilot_client_credentials():
                    agent._buffer_vprint("🔐 Copilot credentials refreshed after 401. Retrying request...")
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                    continue
            if (
                agent.api_mode == "anthropic_messages"
                and status_code == 401
                and hasattr(agent, '_anthropic_api_key')
                and not _cycle.recovery.anthropic_auth_retry_attempted
            ):
                _cycle.recovery.anthropic_auth_retry_attempted = True
                from agent.anthropic_adapter import _is_oauth_token
                from agent.azure_identity_adapter import is_token_provider
                if agent._try_refresh_anthropic_client_credentials():
                    print(f"{agent.log_prefix}🔐 Anthropic credentials refreshed after 401. Retrying request...")
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                    continue
                # Credential refresh didn't help — show diagnostic info
                key = agent._anthropic_api_key
                print(f"{agent.log_prefix}🔐 Anthropic 401 — authentication failed.")
                if is_token_provider(key):
                    # Azure Foundry Entra ID — the bearer token is
                    # minted per-request by an httpx event hook on a
                    # custom http_client passed to the SDK. The 401
                    # means Azure rejected the JWT (RBAC role missing,
                    # az login expired, IMDS unreachable, etc.).
                    print(f"{agent.log_prefix}   Auth method: Microsoft Entra ID (httpx event hook)")
                    print(f"{agent.log_prefix}   Run `hermes doctor` for credential-chain diagnostics, or")
                    print(f"{agent.log_prefix}   `az login` if your developer session expired.")
                else:
                    auth_method = "Bearer (OAuth/setup-token)" if _is_oauth_token(key) else "x-api-key (API key)"
                    print(f"{agent.log_prefix}   Auth method: {auth_method}")
                    print(f"{agent.log_prefix}   Token prefix: {key[:12]}..." if isinstance(key, str) and len(key) > 12 else f"{agent.log_prefix}   Token: (empty or short)")
                print(f"{agent.log_prefix}   Troubleshooting:")
                from hermes_constants import display_hermes_home as _dhh_fn
                _dhh = _dhh_fn()
                print(f"{agent.log_prefix}     • Check ANTHROPIC_TOKEN in {_dhh}/.env for Hermes-managed OAuth/setup tokens")
                print(f"{agent.log_prefix}     • Check ANTHROPIC_API_KEY in {_dhh}/.env for API keys or legacy token values")
                print(f"{agent.log_prefix}     • For API keys: verify at https://platform.claude.com/settings/keys")
                print(f"{agent.log_prefix}     • For Claude Code: run 'claude /login' to refresh, then retry")
                print(f"{agent.log_prefix}     • Legacy cleanup: hermes config set ANTHROPIC_TOKEN \"\"")
                print(f"{agent.log_prefix}     • Clear stale keys: hermes config set ANTHROPIC_API_KEY \"\"")

            # Thinking block signature recovery.
            #
            # Anthropic signs thinking blocks against the full turn
            # content. Any upstream mutation (context compression,
            # session truncation, message merging) invalidates the
            # signature and the API replies HTTP 400 ("invalid
            # signature" or "cannot be modified"). Recovery strips
            # ``reasoning_details`` so the retry sends no thinking
            # blocks at all. One-shot per outer loop.
            #
            # The strip targets ``api_messages``, which is the
            # API-call-time list that ``_build_api_kwargs`` consumes
            # on every retry. ``api_messages`` was populated once at
            # the start of the turn from shallow copies of
            # ``messages``, so mutating it does not touch the
            # canonical store. The previous implementation popped
            # ``reasoning_details`` from ``messages`` instead, which
            # had two problems: ``api_messages`` carried its own
            # reference to the field through the shallow copy, so the
            # retry's wire payload still included thinking blocks and
            # the recovery never reached the API; and the mutation
            # persisted into ``state.db`` through any subsequent
            # ``_persist_session`` call, permanently corrupting the
            # conversation. Future turns would replay the stripped
            # state, hit the same 400, and the agent would terminate
            # with ``max_retries_exhausted``, often spawning
            # cascading compaction-ended sessions chained off the
            # corrupted parent.
            if (
                classified.reason == _loop.FailoverReason.thinking_signature
                and not _cycle.recovery.thinking_sig_retry_attempted
            ):
                _cycle.recovery.thinking_sig_retry_attempted = True
                _api_stripped = 0
                for _m in api_messages:
                    if isinstance(_m, dict) and "reasoning_details" in _m:
                        _m.pop("reasoning_details", None)
                        _api_stripped += 1
                agent._vprint(
                    f"{agent.log_prefix}⚠️  Thinking block signature invalid, "
                    f"stripped reasoning_details from api_messages for retry...",
                    force=True,
                )
                _loop.logger.warning(
                    "%sThinking block signature recovery: stripped "
                    "reasoning_details from %d api_messages "
                    "(canonical messages unchanged)",
                    agent.log_prefix, _api_stripped,
                )
                _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                continue

            # ── Invalid encrypted reasoning replay recovery ───────
            # OpenAI Responses API surfaces (and some compatible relays)
            # return HTTP 400 ``invalid_encrypted_content`` when a
            # replayed ``codex_reasoning_items`` blob from a previous
            # turn fails verification (provider rotated the encryption
            # key, the route doesn't actually persist reasoning state,
            # etc.).  Recovery: disable replay for the rest of the
            # session, strip cached items from history, retry once.
            # One-shot — if a second 400 fires we fall through to the
            # normal retry/backoff path.  Only fires for codex_responses
            # mode with at least one assistant message that has cached
            # ``codex_reasoning_items``; without replay state, the
            # error is unrelated to our cache so the normal retry path
            # handles it (the provider is rejecting something else).
            if (
                classified.reason == _loop.FailoverReason.invalid_encrypted_content
                and not _cycle.recovery.invalid_encrypted_content_retry_attempted
                and agent.api_mode == "codex_responses"
                and bool(getattr(agent, "_codex_reasoning_replay_enabled", True))
                and any(
                    isinstance(_m, dict)
                    and _m.get("role") == "assistant"
                    and isinstance(_m.get("codex_reasoning_items"), list)
                    and _m.get("codex_reasoning_items")
                    for _m in _ctx.messages
                )
            ):
                _cycle.recovery.invalid_encrypted_content_retry_attempted = True
                replay_stats = agent._disable_codex_reasoning_replay(_ctx.messages)
                agent._vprint(
                    f"{agent.log_prefix}⚠️  Encrypted reasoning replay was rejected by the provider — "
                    f"disabled replay and stripped {replay_stats['items']} item(s) from "
                    f"{replay_stats['messages']} message(s), retrying...",
                    force=True,
                )
                _loop.logger.warning(
                    "%sInvalid encrypted reasoning recovery: disabled replay and stripped %d items from %d messages",
                    agent.log_prefix,
                    replay_stats["items"],
                    replay_stats["messages"],
                )
                _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                continue

            # ── Native compaction rejection recovery ──────────────
            # Provider explicitly rejected the ``context_management``
            # field (structured 400 naming the param). One-shot: turn
            # native compaction off for the rest of the session and
            # retry — the next _build_api_kwargs re-resolves the gate
            # and omits the field, and Hermes' local compression takes
            # over as the sole owner. Generic 4xx/5xx/timeouts do NOT
            # match (see is_native_compaction_rejection) and take the
            # normal retry path.
            if (
                agent.api_mode == "codex_responses"
                and not _cycle.recovery.native_compaction_reject_retry_attempted
                and bool(getattr(agent, "codex_responses_native_compaction", False))
            ):
                from agent.native_compaction import is_native_compaction_rejection
                if is_native_compaction_rejection(
                    api_error, getattr(api_error, "status_code", None)
                ):
                    _cycle.recovery.native_compaction_reject_retry_attempted = True
                    agent.codex_responses_native_compaction = False
                    agent._vprint(
                        f"{agent.log_prefix}⚠️  Provider rejected native compaction "
                        f"(context_management) — disabled for this session, "
                        f"local compression stays active. Retrying...",
                        force=True,
                    )
                    _loop.logger.warning(
                        "%sNative compaction rejection recovery: disabled "
                        "codex_responses_native for this session and retrying",
                        agent.log_prefix,
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue

            # ── llama.cpp grammar-parse recovery ──────────────────
            # llama.cpp's ``json-schema-to-grammar`` converter rejects
            # regex escape classes (``\d``, ``\w``, ``\s``) and most
            # ``format`` values in tool schemas.  MCP servers emit
            # these routinely for date/phone/email params.  Recovery:
            # strip ``pattern``/``format`` from ``agent.tools`` and
            # retry once.  We keep the keywords by default so cloud
            # providers get the full prompting hints; this branch
            # fires only for users on llama.cpp's OAI server.
            if (
                classified.reason == _loop.FailoverReason.llama_cpp_grammar_pattern
                and not _cycle.recovery.llama_cpp_grammar_retry_attempted
            ):
                _cycle.recovery.llama_cpp_grammar_retry_attempted = True
                try:
                    from tools.schema_sanitizer import strip_pattern_and_format
                    _, _stripped = strip_pattern_and_format(agent.tools)
                except Exception as _strip_exc:  # pragma: no cover — defensive
                    _loop.logger.warning(
                        "%sllama.cpp grammar recovery: strip helper failed: %s",
                        agent.log_prefix, _strip_exc,
                    )
                    _stripped = 0
                if _stripped:
                    agent._vprint(
                        f"{agent.log_prefix}⚠️  llama.cpp rejected tool schema grammar — "
                        f"stripped {_stripped} pattern/format keyword(s), retrying...",
                        force=True,
                    )
                    _loop.logger.warning(
                        "%sllama.cpp grammar recovery: stripped %d "
                        "pattern/format keyword(s) from tool schemas",
                        agent.log_prefix, _stripped,
                    )
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                    continue
                # No keywords found to strip — fall through to normal
                # retry path rather than loop forever on the same error.
                _loop.logger.warning(
                    "%sllama.cpp grammar error but no pattern/format "
                    "keywords to strip — falling through to normal retry",
                    agent.log_prefix,
                )

            _cycle.retry_count += 1
            elapsed_time = _loop.time.time() - api_start_time
            agent._touch_activity(
                f"API error recovery (attempt {_cycle.retry_count}/{_cycle.max_retries})"
            )

            error_type = type(api_error).__name__
            error_msg = str(api_error).lower()
            _error_summary = agent._summarize_api_error(api_error)
            _loop.logger.warning(
                "API call failed (attempt %s/%s) error_type=%s %s summary=%s",
                _cycle.retry_count,
                _cycle.max_retries,
                error_type,
                agent._client_log_context(),
                _error_summary,
            )

            _provider = getattr(agent, "provider", "unknown")
            _base = getattr(agent, "base_url", "unknown")
            _model = getattr(agent, "model", "unknown")
            _status_code_str = f" [HTTP {status_code}]" if status_code else ""
            agent._buffer_vprint(f"⚠️  API call failed (attempt {_cycle.retry_count}/{_cycle.max_retries}): {error_type}{_status_code_str}")
            agent._buffer_vprint(f"   🔌 Provider: {_provider}  Model: {_model}")
            agent._buffer_vprint(f"   🌐 Endpoint: {_base}")
            agent._buffer_vprint(f"   📝 Error: {_error_summary}")
            if status_code and status_code < 500:
                _err_body = getattr(api_error, "body", None)
                _err_body_str = str(_err_body)[:300] if _err_body else None
                if _err_body_str:
                    agent._buffer_vprint(f"   📋 Details: {_err_body_str}")
            agent._buffer_vprint(f"   ⏱️  Elapsed: {elapsed_time:.2f}s  Context: {len(api_messages)} msgs, ~{approx_tokens:,} tokens")

            # Actionable hint for OpenRouter "no tool endpoints" error.
            # Buffered like the rest of the retry trace — surfaced only
            # if every retry+fallback exhausts.  Avoids spamming users
            # who recover automatically via fallback.
            if (
                agent._is_openrouter_url()
                and "support tool use" in error_msg
            ):
                agent._buffer_vprint(
                    f"   💡 No OpenRouter providers for {_model} support tool calling with your current settings."
                )
                if agent.providers_allowed:
                    agent._buffer_vprint(
                        "      Your provider_routing.only restriction is filtering out tool-capable providers."
                    )
                    agent._buffer_vprint(
                        "      Try removing the restriction or adding providers that support tools for this model."
                    )
                agent._buffer_vprint(
                    f"      Check which providers support tools: https://openrouter.ai/models/{_model}"
                )

            # Actionable hint for a bare 404 on a provider whose catalogue
            # uses ``vendor/model`` ids.  A model id that lost its prefix
            # (e.g. ``nemotron-…`` instead of ``nvidia/nemotron-…``) gets
            # a content-free "404 page not found" from the provider that
            # never names the model, so it reads like an outage or an auth
            # failure.  Name the real cause and the exact id to use (#78796).
            if getattr(api_error, "status_code", None) == 404:
                try:
                    from hermes_cli.model_normalize import suggest_prefixed_model_id

                    _suggestion = suggest_prefixed_model_id(_provider, _model)
                except Exception:
                    _suggestion = None
                if _suggestion:
                    agent._buffer_vprint(
                        f"   💡 Model '{_model}' is not a valid id for provider {_provider} — "
                        f"it is missing its vendor prefix."
                    )
                    agent._buffer_vprint(
                        f"      Did you mean '{_suggestion}'?  Re-pick it with `hermes model`."
                    )

            # Check for interrupt before deciding to retry
            if agent._interrupt_requested:
                # Preserve a pending redirect (mid-stream correction): the
                # user is steering, not stopping. Rebuild the turn from the
                # correction instead of aborting with a dead-end interrupt.
                if agent.clear_interrupt(preserve_redirect=True):
                    _cycle.restart(_turn_controller, _loop.TurnReason.REDIRECT)
                    break
                agent._vprint(f"{agent.log_prefix}⚡ Interrupt detected during error handling, aborting retries.", force=True)
                _interrupt_text = f"Operation interrupted: handling API error ({error_type}: {agent._clean_error_message(str(api_error))})."
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

            # Check for 413 payload-too-large BEFORE generic 4xx handler.
            # A 413 is a payload-size error — the correct response is to
            # compress history and retry, not abort immediately.
            status_code = getattr(api_error, "status_code", None)

            # ── Respect disabled auto-compaction on overflow ──────
            # Ported from anomalyco/opencode#30749.  When the user has
            # turned auto-compaction off (``compression.enabled: false``),
            # NO automatic compaction trigger may fire — including the
            # provider/request-size overflow recovery paths below
            # (long-context-tier 429, 413 payload-too-large, and
            # context-overflow).  Without this guard the proactive
            # threshold path correctly honours the setting (see the
            # preflight check and the post-response ``should_compress``
            # gate) but a provider overflow error would still silently
            # compress + rotate the session, bypassing the user's
            # explicit choice.  Surface a terminal error instead so the
            # user can compact manually (``/compress``), start fresh
            # (``/new``), switch to a larger-context model, or reduce
            # attachments.  Forced compaction via ``/compress``
            # (``force=True``) is unaffected — it never reaches this loop.
            #
            # Output-cap errors (max_tokens too large) are NOT input
            # overflow — the recovery is a max_tokens-only retry that
            # does not require compression.  Exempt them from this guard
            # so the retry still fires even when compression is disabled.
            _overflow_reasons = {
                _loop.FailoverReason.long_context_tier,
                _loop.FailoverReason.payload_too_large,
                _loop.FailoverReason.context_overflow,
            }
            _is_output_cap_error = (
                _loop.is_output_cap_error(error_msg)
                or _loop.parse_available_output_tokens_from_error(error_msg) is not None
            )
            if (
                classified.reason in _overflow_reasons
                and not getattr(agent, "compression_enabled", True)
                and not _is_output_cap_error
            ):
                agent._flush_status_buffer()
                agent._vprint(
                    f"{agent.log_prefix}❌ Context overflow, but auto-compaction is disabled "
                    f"(compression.enabled: false).",
                    force=True,
                )
                agent._vprint(
                    f"{agent.log_prefix}   💡 Run /compress to compact manually, /new to start fresh, "
                    f"switch to a larger-context model, or reduce attachments.",
                    force=True,
                )
                _loop.logger.error(
                    f"{agent.log_prefix}Context overflow ({classified.reason.value}) with "
                    f"auto-compaction disabled — not compressing."
                )
                agent._persist_session(_ctx.messages, _ctx.conversation_history)
                _final_response = (
                    "Context overflow and auto-compaction is disabled "
                    "(compression.enabled: false). Run /compress to compact manually, "
                    "/new to start fresh, or switch to a larger-context model."
                )
                return _loop._complete_direct_turn(_turn_controller, {
                    "final_response": _final_response,
                    "messages": _ctx.messages,
                    "completed": False,
                    "api_calls": api_call_count,
                    "error": _final_response,
                    "partial": True,
                    "failed": True,
                    "compaction_disabled": True,
                })

            # ── Anthropic Sonnet long-context tier gate ───────────
            # Anthropic returns HTTP 429 "Extra usage is required for
            # long context requests" when a Claude Max (or similar)
            # subscription doesn't include the 1M-context tier.  This
            # is NOT a transient rate limit — retrying or switching
            # credentials won't help.  Reduce context to 200k (the
            # standard tier) and compress.
            if classified.reason == _loop.FailoverReason.long_context_tier:
                _reduced_ctx = 200000
                compressor = agent.context_compressor
                old_ctx = compressor.context_length
                if old_ctx > _reduced_ctx:
                    compressor.update_model(
                        model=agent.model,
                        context_length=_reduced_ctx,
                        base_url=agent.base_url,
                        api_key=getattr(agent, "api_key", ""),
                        provider=agent.provider,
                        api_mode=agent.api_mode,
                    )
                    # Context probing flags — only set on built-in
                    # compressor (plugin engines manage their own).
                    if hasattr(compressor, "_context_probed"):
                        compressor._context_probed = True
                        # Don't persist — this is a subscription-tier
                        # limitation, not a model capability.  If the
                        # user later enables extra usage the 1M limit
                        # should come back automatically.
                        compressor._context_probe_persistable = False
                    agent._buffer_vprint(
                        f"⚠️  Anthropic long-context tier "
                        f"requires extra usage — reducing context: "
                        f"{old_ctx:,} → {_reduced_ctx:,} tokens"
                    )

                _recovery.compression_attempts += 1
                if _recovery.compression_attempts <= _recovery.max_compression_attempts:
                    original_len = len(_ctx.messages)
                    # Option A (LCM issue 441): overhead-aware request size so recovery arms on
                    # the true request (msgs + tools + system), not the tool-blind message count.
                    _ctx.messages, _ctx.active_system_prompt = agent._compress_context(
                        _ctx.messages, system_message,
                        approx_tokens=_loop.estimate_request_tokens_rough(api_messages, tools=agent.tools or None),
                        task_id=_ctx.effective_task_id,
                    )
                    _ctx.conversation_history = _loop.conversation_history_after_compression(
                        agent, _ctx.messages, _ctx.conversation_history
                    )
                    if len(_ctx.messages) < original_len or old_ctx > _reduced_ctx:
                        agent._buffer_status(
                            _loop.COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE.format(
                                new_ctx=_reduced_ctx, old_ctx=old_ctx
                            )
                        )
                        _loop.time.sleep(2)
                        _cycle.restart(_turn_controller, _loop.TurnReason.COMPRESSION)
                        break
                # Fall through to normal error handling if compression
                # is exhausted or didn't help.

            # Eager fallback for rate-limit errors (429 or quota exhaustion)
            # and transport errors (connection failure / timeout / provider
            # overloaded).  Rate limits and billing: switch immediately —
            # the primary provider won't recover within the retry window.
            # Transport errors: allow 1 retry first (transient hiccups
            # recover), then fall back if the provider is truly unreachable.
            is_rate_limited = classified.reason in {
                _loop.FailoverReason.rate_limit,
                _loop.FailoverReason.billing,
                _loop.FailoverReason.upstream_rate_limit,
            }
            _is_transport_failure = classified.reason in {
                _loop.FailoverReason.timeout,
                _loop.FailoverReason.overloaded,
            }
            # Z.AI Coding Plan GLM-5.2 overload 429s classify as
            # `overloaded` (to spare the credential pool), but `overloaded`
            # is excluded from `is_rate_limited` — the gate for the adaptive
            # Z.AI backoff below. Detect the overload directly so its
            # long-backoff schedule runs, and raise the retry ceiling so the
            # long tier (30/60/90/120s) is reachable. See
            # zai_coding_overload_retry_ceiling() for the ceiling rationale.
            _is_zai_coding_overload = _loop.is_zai_coding_overload_error(
                base_url=str(_base), model=_model, error=api_error
            )
            if _is_zai_coding_overload:
                _cycle.max_retries = max(_cycle.max_retries, _loop.zai_coding_overload_retry_ceiling())
            _should_fallback = (
                is_rate_limited
                or (_is_transport_failure and _cycle.retry_count >= 2)
            )
            if _should_fallback and agent._fallback_index < len(agent._fallback_chain):
                # Don't eagerly fallback if credential pool rotation may
                # still recover.  See _pool_may_recover_from_rate_limit
                # for the single-credential-pool exception.  Fixes #11314.
                #
                # Exception: an upstream-aggregator 429 — the credential
                # pool can't help when the *upstream* model (DeepSeek,
                # etc.) is throttling OpenRouter, so always fall back to a
                # different model regardless of pool state.
                _is_upstream = classified.reason == _loop.FailoverReason.upstream_rate_limit
                pool_may_recover = (
                    False if _is_upstream
                    else _loop._ra()._pool_may_recover_from_rate_limit(
                        agent._credential_pool,
                    )
                )
                if not pool_may_recover:
                    if _is_upstream:
                        _upstream_name = (classified.error_context or {}).get(
                            "upstream_provider", "aggregator"
                        )
                        agent._buffer_status(
                            f"⚠️ Upstream {_upstream_name} rate-limited — "
                            "switching to fallback model..."
                        )
                    elif classified.reason == _loop.FailoverReason.billing:
                        if classified.billing_unverified:
                            # Ambiguous body (#82154) — don't assert billing.
                            agent._buffer_status(
                                "⚠️ Provider reported usage/credit exhaustion "
                                "(unverified — may be a content-filter rejection) "
                                "— switching to fallback provider..."
                            )
                        else:
                            agent._buffer_status(
                                "⚠️ Billing or credits exhausted — switching to fallback provider..."
                            )
                    elif _is_transport_failure:
                        agent._buffer_status(
                            "⚠️ Provider unreachable — switching to fallback provider..."
                        )
                    else:
                        agent._buffer_status("⚠️ Rate limited — switching to fallback provider...")
                    if agent._try_activate_fallback(reason=classified.reason):
                        _ctx.active_system_prompt = _loop._sync_failover_system_message(
                            agent, api_messages, _ctx.active_system_prompt)
                        _cycle.retry_count = 0
                        _recovery.compression_attempts = 0
                        _cycle.recovery.primary_recovery_attempted = False
                        _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                        break

            # ── Auth-failure provider failover ───────────────────────
            # A 401/403 that survives the per-provider credential-refresh
            # attempt above (each guarded by its own
            # ``*_auth_retry_attempted`` flag) means the active provider's
            # credential or endpoint is broken in a way refreshing can't
            # fix (revoked OAuth, blocked/expired key, an account pinned to
            # a dead/staging endpoint). Previously the loop only printed
            # "switch providers manually" advice and fell through, so a
            # user with a configured fallback chain kept thrashing on the
            # same dead credential every turn instead of failing over.
            # Escalate to the fallback chain here, mirroring the rate-
            # limit/billing failover above. When no fallback is configured
            # (or the chain is exhausted), _try_activate_fallback returns
            # False and we fall through to the existing terminal handling
            # + provider-specific troubleshooting guidance unchanged.
            if (
                classified.is_auth
                and not _cycle.recovery.auth_failover_attempted
                and agent._fallback_index < len(agent._fallback_chain)
            ):
                _cycle.recovery.auth_failover_attempted = True
                agent._buffer_status(
                    "🔐 Authentication failed and could not be refreshed — "
                    "switching to fallback provider..."
                )
                if agent._try_activate_fallback(reason=classified.reason):
                    _ctx.active_system_prompt = _loop._sync_failover_system_message(
                        agent, api_messages, _ctx.active_system_prompt)
                    _cycle.retry_count = 0
                    _recovery.compression_attempts = 0
                    _cycle.recovery.primary_recovery_attempted = False
                    _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                    break

            # ── Nous Portal: record rate limit & skip retries ─────
            # When Nous returns a 429 that is a genuine account-
            # level rate limit, record the reset time to a shared
            # file so ALL sessions (cron, gateway, auxiliary) know
            # not to pile on, then skip further retries -- each
            # one burns another RPH request and deepens the hole.
            # The retry loop's top-of-iteration guard will catch
            # this on the next pass and try fallback or bail.
            #
            # IMPORTANT: Nous Portal multiplexes multiple upstream
            # providers (DeepSeek, Kimi, MiMo, Hermes).  A 429 can
            # also mean an UPSTREAM provider is out of capacity
            # for one specific model -- transient, clears in
            # seconds, nothing to do with the caller's quota.
            # Tripping the cross-session breaker on that would
            # block every Nous model for minutes.  We use
            # ``is_genuine_nous_rate_limit`` to tell the two
            # apart via the 429's own x-ratelimit-* headers and
            # the last-known-good state captured on the previous
            # successful response.
            if (
                is_rate_limited
                and agent.provider == "nous"
                and classified.reason == _loop.FailoverReason.rate_limit
                and not recovered_with_pool
            ):
                _genuine_nous_rate_limit = False
                try:
                    from agent.nous_rate_guard import (
                        is_genuine_nous_rate_limit,
                        record_nous_rate_limit,
                    )
                    _err_resp = getattr(api_error, "response", None)
                    _err_hdrs = (
                        getattr(_err_resp, "headers", None)
                        if _err_resp else None
                    )
                    _genuine_nous_rate_limit = is_genuine_nous_rate_limit(
                        headers=_err_hdrs,
                        last_known_state=agent._rate_limit_state,
                    )
                    if _genuine_nous_rate_limit:
                        record_nous_rate_limit(
                            headers=_err_hdrs,
                            error_context=error_context,
                        )
                    else:
                        _loop.logger.info(
                            "Nous 429 looks like upstream capacity "
                            "(no exhausted bucket in headers or "
                            "last-known state) -- not tripping "
                            "cross-session breaker."
                        )
                except Exception:
                    pass
                if _genuine_nous_rate_limit:
                    # Re-enter the loop exactly once so the
                    # top-of-loop Nous guard handles fallback or
                    # bails cleanly. (Setting retry_count to
                    # max_retries would make the while condition
                    # false immediately and the guard would never
                    # run -- no fallback, generic exhaustion error.)
                    _cycle.retry_count = max(0, _cycle.max_retries - 1)
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.RATE_LIMIT)
                    continue
                # Upstream capacity 429: fall through to normal
                # retry logic.  A different model (or the same
                # model a moment later) will typically succeed.

            is_payload_too_large = (
                classified.reason == _loop.FailoverReason.payload_too_large
            )

            # Actionable hint for GitHub Models (Azure) 413 errors.
            # The free tier enforces a hard 8K token cap per request,
            # which Hermes' system prompt + tool schemas alone exceed.
            # Compression can't help — the floor is the system prompt
            # itself, not the conversation — so surface a clear "not
            # compatible" message instead of looping into three futile
            # compression attempts.
            if (
                status_code == 413
                and isinstance(agent.base_url, str)
                and _loop.base_url_host_matches(agent.base_url, "models.inference.ai.azure.com")
            ):
                agent._vprint(
                    f"{agent.log_prefix}   💡 GitHub Models free tier (models.inference.ai.azure.com) caps every",
                    force=True,
                )
                agent._vprint(
                    f"{agent.log_prefix}      request at ~8K tokens. Hermes' system prompt + tool schemas baseline",
                    force=True,
                )
                agent._vprint(
                    f"{agent.log_prefix}      exceeds that floor, so this endpoint cannot run an agentic loop.",
                    force=True,
                )
                agent._vprint(
                    f"{agent.log_prefix}      Use the `copilot` provider with a Copilot subscription token (`hermes",
                    force=True,
                )
                agent._vprint(
                    f"{agent.log_prefix}      setup` → GitHub Copilot), or pick any other provider.",
                    force=True,
                )

            if is_payload_too_large:
                _recovery.compression_attempts += 1
                if _recovery.compression_attempts > _recovery.max_compression_attempts:
                    # Terminal — surface the buffered retry trace.
                    agent._flush_status_buffer()
                    agent._vprint(f"{agent.log_prefix}❌ Max compression attempts ({_recovery.max_compression_attempts}) reached for payload-too-large error.", force=True)
                    agent._vprint(f"{agent.log_prefix}   💡 Try /new to start a fresh conversation, or /compress to retry compression.", force=True)
                    _loop.logger.error("%s413 compression failed after %d attempts.", agent.log_prefix, _recovery.max_compression_attempts)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    _final_response = f"Request payload too large: max compression attempts ({_recovery.max_compression_attempts}) reached."
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _final_response,
                        "messages": _ctx.messages,
                        "completed": False,
                        "api_calls": api_call_count,
                        "error": _final_response,
                        "partial": True,
                        "failed": True,
                        "compression_exhausted": True,
                    })
                agent._buffer_status(f"⚠️  Request payload too large (413) — compression attempt {_recovery.compression_attempts}/{_recovery.max_compression_attempts}...")

                original_len = len(_ctx.messages)
                original_tokens = _loop.estimate_messages_tokens_rough(_ctx.messages)
                _overflow_input = _ctx.messages
                # Option A (LCM issue 441): overhead-aware request size so recovery arms on the
                # true request (msgs + tools + system), not the tool-blind message count.
                _ctx.messages, _ctx.active_system_prompt = agent._compress_context(
                    _ctx.messages, system_message,
                    approx_tokens=_loop.estimate_request_tokens_rough(api_messages, tools=agent.tools or None),
                    task_id=_ctx.effective_task_id,
                )
                if _ctx.messages is _overflow_input and _loop.compression_skipped_due_to_lock(agent):
                    # #69870 lock-skip: the provider proved the request
                    # does not fit, but this compression pass no-oped only
                    # because another path holds the session's compression
                    # lock. Temporary defer, not exhaustion — refund the
                    # attempt and end the turn softly so the gateway does
                    # NOT auto-reset the session (#9893/#35809).
                    _recovery.compression_attempts -= 1
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    return _loop._complete_direct_turn(_turn_controller, _loop._compression_deferred_result(
                        agent, _ctx.messages, api_call_count
                    ))
                _ctx.conversation_history = _loop.conversation_history_after_compression(
                    agent, _ctx.messages, _ctx.conversation_history
                )

                # Re-estimate tokens after compression.  Same-message-count
                # compression (tool-result pruning, in-place summarization)
                # can materially reduce request size without reducing the
                # message array.  (#39550)
                new_tokens = _loop.estimate_messages_tokens_rough(_ctx.messages)
                approx_tokens = new_tokens  # update for downstream logging

                if len(_ctx.messages) < original_len or (new_tokens > 0 and new_tokens < original_tokens * 0.95):
                    if len(_ctx.messages) < original_len:
                        agent._buffer_status(_loop.COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=original_len, after=len(_ctx.messages)))
                    else:
                        agent._buffer_status(_loop.COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE.format(before=original_tokens, after=new_tokens))
                    _loop.time.sleep(2)  # Brief pause between compression retries
                    _cycle.restart(_turn_controller, _loop.TurnReason.COMPRESSION)
                    break
                else:
                    if agent._try_strip_image_parts_from_tool_messages(
                        api_messages,
                        remember_model=False,
                    ):
                        agent._buffer_status(
                            "📐 Compression could not reduce the request further — "
                            "removed retained vision payloads and retrying..."
                        )
                        _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PAYLOAD_REPAIR)
                        continue

                    # Terminal — surface buffered context so the user
                    # sees what compression attempts were made.
                    agent._flush_status_buffer()
                    agent._vprint(f"{agent.log_prefix}❌ Payload too large and cannot compress further.", force=True)
                    agent._vprint(f"{agent.log_prefix}   💡 Try /new to start a fresh conversation, or /compress to retry compression.", force=True)
                    _loop.logger.error("%s413 payload too large. Cannot compress further.", agent.log_prefix)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    _final_response = "Request payload too large (413). Cannot compress further."
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _final_response,
                        "messages": _ctx.messages,
                        "completed": False,
                        "api_calls": api_call_count,
                        "error": _final_response,
                        "partial": True,
                        "failed": True,
                        "compression_exhausted": True,
                    })

            # Check for context-length errors BEFORE generic 4xx handler.
            # The classifier detects context overflow from: explicit error
            # messages, generic 400 + large session heuristic (#1630), and
            # server disconnect + large session pattern (#2153).
            is_context_length_error = (
                classified.reason == _loop.FailoverReason.context_overflow
            )

            if is_context_length_error:
                compressor = agent.context_compressor
                old_ctx = compressor.context_length

                # ── Distinguish two very different errors ───────────
                # 1. "Prompt too long": the INPUT exceeds the context window.
                #    Fix: reduce context_length + compress history.
                # 2. "max_tokens too large": input is fine, but
                #    input_tokens + requested max_tokens > context_window.
                #    Fix: reduce max_tokens (the OUTPUT cap) for this call.
                #    Do NOT shrink context_length — the window is unchanged.
                #
                # Note: max_tokens = output token cap (one response).
                #       context_length = total window (input + output combined).
                available_out = _loop.parse_available_output_tokens_from_error(error_msg)
                if available_out is not None:
                    # This is an output-cap error, not input overflow.
                    # The provider's available_tokens is the authoritative
                    # cap for the failed request, so keep it as an upper
                    # bound.  Also estimate the current API request shape
                    # (system prompt, injected context, tool schemas) because
                    # Hermes may add API-only content not present in persisted
                    # messages.  Use the smaller budget and apply a small
                    # safety margin.  Do not alter context_length.
                    request_input_estimate = _loop.estimate_request_tokens_rough(
                        api_messages, tools=agent.tools or None,
                    )
                    local_available_out = old_ctx - request_input_estimate
                    if local_available_out > 0:
                        safe_out = max(1, min(available_out, local_available_out) - 64)
                    else:
                        # The rough local estimate can overshoot the real
                        # request size.  Fall back to the provider-reported
                        # budget, which is authoritative for the failed
                        # request.
                        safe_out = max(1, available_out - 64)
                    agent._ephemeral_max_output_tokens = safe_out
                    agent._buffer_vprint(
                        f"⚠️  Output cap too large for current prompt — "
                        f"retrying with max_tokens={safe_out:,} "
                        f"(provider_available={available_out:,}, "
                        f"estimated_request_tokens={request_input_estimate:,}; "
                        f"context_length unchanged at {old_ctx:,})"
                    )
                    # Still count against compression_attempts so we don't
                    # loop forever if the error keeps recurring.
                    _recovery.compression_attempts += 1
                    if _recovery.compression_attempts > _recovery.max_compression_attempts:
                        agent._flush_status_buffer()
                        agent._vprint(f"{agent.log_prefix}❌ Max compression attempts ({_recovery.max_compression_attempts}) reached.", force=True)
                        agent._vprint(f"{agent.log_prefix}   💡 Try /new to start a fresh conversation, or /compress to retry compression.", force=True)
                        _loop.logger.error("%sContext compression failed after %d attempts.", agent.log_prefix, _recovery.max_compression_attempts)
                        agent._persist_session(_ctx.messages, _ctx.conversation_history)
                        _final_response = f"Context length exceeded: max compression attempts ({_recovery.max_compression_attempts}) reached."
                        return _loop._complete_direct_turn(_turn_controller, {
                            "final_response": _final_response,
                            "messages": _ctx.messages,
                            "completed": False,
                            "api_calls": api_call_count,
                            "error": _final_response,
                            "partial": True,
                            "failed": True,
                            "compression_exhausted": True,
                        })
                    # Also compress the message history so the output-cap
                    # retry does not just spin on max_tokens alone.  The
                    # compressor drops the middle window, freeing enough
                    # tokens for the total to fit inside context_length.
                    # (#55546)
                    try:
                        original_len = len(_ctx.messages)
                        original_tokens = _loop.estimate_messages_tokens_rough(_ctx.messages)
                        _overflow_input = _ctx.messages
                        _ctx.messages, _ctx.active_system_prompt = agent._compress_context(
                            _ctx.messages, system_message,
                            approx_tokens=request_input_estimate,
                            task_id=_ctx.effective_task_id,
                        )
                        if _ctx.messages is _overflow_input and _loop.compression_skipped_due_to_lock(agent):
                            _recovery.compression_attempts -= 1
                            agent._persist_session(_ctx.messages, _ctx.conversation_history)
                            return _loop._complete_direct_turn(_turn_controller, _loop._compression_deferred_result(
                                agent, _ctx.messages, api_call_count
                            ))
                        _ctx.conversation_history = _loop.conversation_history_after_compression(
                            agent, _ctx.messages, _ctx.conversation_history
                        )
                        new_tokens = _loop.estimate_messages_tokens_rough(_ctx.messages)
                        if len(_ctx.messages) < original_len:
                            agent._buffer_status(_loop.COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=original_len, after=len(_ctx.messages)))
                        elif new_tokens > 0 and new_tokens < original_tokens * 0.95:
                            agent._buffer_status(_loop.COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE.format(before=original_tokens, after=new_tokens))
                    except Exception:
                        # Compression must never turn an output-cap error
                        # fatal — fall through and retry on max_tokens alone.
                        _loop.logger.warning(
                            "%sOutput-cap compression hit an error; retrying on max_tokens only.",
                            agent.log_prefix,
                        )
                    _cycle.restart(_turn_controller, _loop.TurnReason.COMPRESSION)
                    break

                # The error is output-cap-shaped (about max_tokens being
                # too large) but the provider's wording didn't let us parse
                # the available output budget.  Compression CANNOT help here
                # — the input already fits; the call fails deterministically
                # on the oversized max_tokens.  Routing it into compression
                # re-sends the same max_tokens, gets the identical 400, and
                # death-loops until "cannot compress further" (#55546).
                # Fail fast with an actionable message instead of looping.
                if _loop.is_output_cap_error(error_msg):
                    agent._flush_status_buffer()
                    agent._vprint(
                        f"{agent.log_prefix}❌ The provider rejected the request because "
                        f"max_tokens exceeds its output cap for this model.",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}   💡 Lower model.max_tokens in your config.yaml to "
                        f"at or below the model's max-output limit. "
                        f"(This is an output-cap error, not a context overflow — "
                        f"compression cannot fix it.)",
                        force=True,
                    )
                    _loop.logger.error(
                        f"{agent.log_prefix}Output-cap error not routed into compression "
                        f"(max_tokens over provider cap): {error_msg[:200]}"
                    )
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    _final_response = (
                        "max_tokens exceeds the provider's output cap for this model. "
                        "Lower model.max_tokens in config.yaml."
                    )
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _final_response,
                        "messages": _ctx.messages,
                        "completed": False,
                        "api_calls": api_call_count,
                        "error": _final_response,
                        "partial": True,
                        "failed": True,
                    })

                # Error is about the INPUT being too large.  Only reduce
                # context_length when the provider explicitly reports the
                # real lower limit.  If the provider only says "input
                # exceeds the context window", keep the configured window
                # and try compression; guessing probe tiers can incorrectly
                # turn a user-configured 1M window into 256K/128K/64K.
                new_ctx = _loop.get_context_length_from_provider_error(error_msg, old_ctx)
                _provider_lower = (getattr(agent, "provider", "") or "").lower()
                _base_lower = (getattr(agent, "base_url", "") or "").rstrip("/").lower()
                is_minimax_provider = (
                    _provider_lower in {"minimax", "minimax-cn"}
                    or _base_lower.startswith((
                        "https://api.minimax.io/anthropic",
                        "https://api.minimaxi.com/anthropic",
                    ))
                )
                minimax_delta_only_overflow = (
                    is_minimax_provider
                    and new_ctx is None
                    and "context window exceeds limit (" in error_msg
                )

                if new_ctx is not None:
                    agent._buffer_vprint(f"Context limit detected from API: {new_ctx:,} tokens (was {old_ctx:,})")
                    compressor.update_model(
                        model=agent.model,
                        context_length=new_ctx,
                        base_url=agent.base_url,
                        api_key=getattr(agent, "api_key", ""),
                        provider=agent.provider,
                        api_mode=agent.api_mode,
                    )
                    # Persist an explicit provider-reported limit before
                    # compression/retry. The next request can be rate
                    # limited, omit usage, or the process can restart; none
                    # of those should discard metadata the provider already
                    # confirmed. Keep the probe flags as a best-effort
                    # post-success retry if this write cannot complete.
                    _loop.save_context_length(agent.model, agent.base_url, new_ctx)
                    # Context probing flags — only set on built-in
                    # compressor (plugin engines manage their own).  This
                    # value came from the provider, so it is safe to cache.
                    if hasattr(compressor, "_context_probed"):
                        compressor._context_probed = True
                        compressor._context_probe_persistable = True
                    agent._buffer_vprint(f"⚠️  Context length exceeded — using provider limit: {old_ctx:,} → {new_ctx:,} tokens")
                elif minimax_delta_only_overflow:
                    agent._buffer_vprint(
                        f"Provider reported overflow amount only; "
                        f"keeping context_length at {old_ctx:,} tokens and compressing."
                    )
                else:
                    agent._buffer_vprint(
                        f"⚠️  Context length exceeded, but provider did not report a max context length; "
                        f"keeping context_length at {old_ctx:,} tokens and compressing."
                    )

                _recovery.compression_attempts += 1
                if _recovery.compression_attempts > _recovery.max_compression_attempts:
                    agent._flush_status_buffer()
                    agent._vprint(f"{agent.log_prefix}❌ Max compression attempts ({_recovery.max_compression_attempts}) reached.", force=True)
                    agent._vprint(f"{agent.log_prefix}   💡 Try /new to start a fresh conversation, or /compress to retry compression.", force=True)
                    _loop.logger.error("%sContext compression failed after %d attempts.", agent.log_prefix, _recovery.max_compression_attempts)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    _final_response = f"Context length exceeded: max compression attempts ({_recovery.max_compression_attempts}) reached."
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _final_response,
                        "messages": _ctx.messages,
                        "completed": False,
                        "api_calls": api_call_count,
                        "error": _final_response,
                        "partial": True,
                        "failed": True,
                        "compression_exhausted": True,
                    })
                agent._buffer_status(_loop.COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE.format(tokens=approx_tokens, attempt=_recovery.compression_attempts, cap=_recovery.max_compression_attempts))

                original_len = len(_ctx.messages)
                original_tokens = _loop.estimate_messages_tokens_rough(_ctx.messages)
                _overflow_input = _ctx.messages
                # Option A (LCM issue 441): pass the OVERHEAD-AWARE request size (msgs + tool
                # schemas + system), not the tool-blind message count, so LCM forced-overflow
                # recovery arms on the TRUE request that overflowed. See hermes-lcm engine
                # _should_force_overflow_recovery. (approx_tokens stays for the status display.)
                _ctx.messages, _ctx.active_system_prompt = agent._compress_context(
                    _ctx.messages, system_message,
                    approx_tokens=_loop.estimate_request_tokens_rough(api_messages, tools=agent.tools or None),
                    task_id=_ctx.effective_task_id,
                )
                if _ctx.messages is _overflow_input and _loop.compression_skipped_due_to_lock(agent):
                    # #69870 lock-skip: the provider proved the request
                    # does not fit, but this compression pass no-oped only
                    # because another path holds the session's compression
                    # lock. Temporary defer, not exhaustion — refund the
                    # attempt and end the turn softly so the gateway does
                    # NOT auto-reset the session (#9893/#35809).
                    _recovery.compression_attempts -= 1
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    return _loop._complete_direct_turn(_turn_controller, _loop._compression_deferred_result(
                        agent, _ctx.messages, api_call_count
                    ))
                _ctx.conversation_history = _loop.conversation_history_after_compression(
                    agent, _ctx.messages, _ctx.conversation_history
                )

                # Re-estimate tokens after compression.  Same-message-count
                # compression (tool-result pruning, in-place summarization)
                # can materially reduce request size without reducing the
                # message array.  (#39550)
                new_tokens = _loop.estimate_messages_tokens_rough(_ctx.messages)
                approx_tokens = new_tokens  # update for downstream logging

                if len(_ctx.messages) < original_len or (new_tokens > 0 and new_tokens < original_tokens * 0.95) or (new_ctx and new_ctx < old_ctx):
                    if len(_ctx.messages) < original_len:
                        agent._buffer_status(_loop.COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE.format(before=original_len, after=len(_ctx.messages)))
                    elif new_tokens > 0 and new_tokens < original_tokens * 0.95:
                        agent._buffer_status(_loop.COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE.format(before=original_tokens, after=new_tokens))
                    _loop.time.sleep(2)  # Brief pause between compression retries
                    _cycle.restart(_turn_controller, _loop.TurnReason.COMPRESSION)
                    break
                else:
                    # Can't compress further and already at minimum tier
                    agent._flush_status_buffer()
                    agent._vprint(f"{agent.log_prefix}❌ Context length exceeded and cannot compress further.", force=True)
                    agent._vprint(f"{agent.log_prefix}   💡 The conversation has accumulated too much content. Try /new to start fresh, or /compress to manually trigger compression.", force=True)
                    _loop.logger.error("%sContext length exceeded: %s tokens. Cannot compress further.", agent.log_prefix, f"{new_tokens:,}")
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    _final_response = f"Context length exceeded ({new_tokens:,} tokens). Cannot compress further."
                    return _loop._complete_direct_turn(_turn_controller, {
                        "final_response": _final_response,
                        "messages": _ctx.messages,
                        "completed": False,
                        "api_calls": api_call_count,
                        "error": _final_response,
                        "partial": True,
                        "failed": True,
                        "compression_exhausted": True,
                    })

            # Check for non-retryable client errors.  The classifier
            # already accounts for 413, 429, 529 (transient), context
            # overflow, and generic-400 heuristics.  Local validation
            # errors (ValueError, TypeError) are programming bugs.
            # Exclude UnicodeEncodeError — it's a ValueError subclass
            # but is handled separately by the surrogate sanitization
            # path above.  Exclude json.JSONDecodeError — also a
            # ValueError subclass, but it indicates a transient
            # provider/network failure (malformed response body,
            # truncated stream, routing layer corruption), not a
            # local programming bug, and should be retried (#14782).
            is_local_validation_error = (
                isinstance(api_error, (ValueError, TypeError))
                and not isinstance(
                    api_error, (UnicodeEncodeError, _loop.json.JSONDecodeError)
                )
                # ssl.SSLError (and its subclass SSLCertVerificationError)
                # inherits from OSError *and* ValueError via Python MRO,
                # so the isinstance(ValueError) check above would
                # misclassify a TLS transport failure as a local
                # programming bug and abort without retrying.  Exclude
                # ssl.SSLError explicitly so the error classifier's
                # retryable=True mapping takes effect instead.
                and not isinstance(api_error, _loop.ssl.SSLError)
                # Provider/SDK "NoneType is not iterable" failures are
                # shape mismatches from upstream (e.g. chatgpt.com Codex
                # backend response.completed.output=null) — not local
                # programming bugs.  Even after #33042 made our own
                # consumer immune, third-party shims and mocked clients
                # can still surface this shape via TypeError.  Treat
                # them as retryable so the error classifier's normal
                # retry/fallback path runs instead of killing the turn
                # as non-retryable (which left Telegram users staring
                # at a bare "Non-retryable error" with no recovery).
                and not (
                    isinstance(api_error, TypeError)
                    and "nonetype" in str(api_error).lower()
                    and "not iterable" in str(api_error).lower()
                )
            )
            # ``FailoverReason.billing`` (HTTP 402) is NOT in this
            # exclusion set.  By the time we reach this block:
            #   • credential-pool rotation (line ~2031) has already
            #     fired for billing and either ``continue``d or
            #     returned (False, ...) — pool is exhausted or absent.
            #   • the eager-fallback branch above (line ~2422) also
            #     fires on billing and ``continue``s if a fallback
            #     provider is configured.
            # Falling through to here means BOTH recovery paths
            # gave up.  Treating 402 as retryable from this point
            # just burns more paid requests against a depleted
            # balance with no recovery mechanism left — see #31273
            # (real-world: ~$40 in 48h on a 24/7 gateway).  Aborting
            # mirrors how 401/403 (also ``should_fallback=True``)
            # already behave once their recovery paths have failed.
            is_client_error = (
                is_local_validation_error
                or (
                    not classified.retryable
                    and not classified.should_compress
                    and classified.reason not in {
                        _loop.FailoverReason.rate_limit,
                        _loop.FailoverReason.overloaded,
                        _loop.FailoverReason.context_overflow,
                        _loop.FailoverReason.payload_too_large,
                        _loop.FailoverReason.long_context_tier,
                        _loop.FailoverReason.thinking_signature,
                    }
                )
            ) and not is_context_length_error

            if is_client_error:
                # Copilot self-heal BEFORE fallback: a stale/degraded
                # credential surfaces as a 400
                # ``model_not_available_for_integrator`` /
                # ``model_not_supported`` (not a clean 401), so the 401
                # refresh path above never fired. Force a fresh token
                # exchange + client rebuild and retry once on the SAME
                # provider — a fresh 437-char API token routes to the
                # correct integrator and the model becomes available again.
                # Single-shot guard prevents looping on a genuinely
                # unavailable model. Copilot-scoped so other providers'
                # real 400s are untouched.
                if (
                    _loop._is_copilot_provider(agent)
                    and not _cycle.recovery.copilot_stale_cred_retry_attempted
                    and _loop._is_stale_copilot_credential_error(
                        status_code, str(getattr(api_error, "message", "") or api_error)
                    )
                ):
                    _cycle.recovery.copilot_stale_cred_retry_attempted = True
                    if agent._try_recover_stale_copilot_credential():
                        agent._buffer_vprint(
                            "🔐 Copilot credential re-exchanged after "
                            "model_not_available 400. Retrying request..."
                        )
                        _cycle.retry_count = 0
                        _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.AUTH_RECOVERY)
                        continue
                # Try fallback before aborting — a different provider may
                # not have the same issue (rate limit, auth, etc.). Only
                # announce the attempt when a fallback chain actually
                # exists; otherwise "trying fallback..." is a lie and the
                # session looks like it's recovering when it's about to
                # abort silently (#35314, #17446).
                if agent._has_pending_fallback():
                    if classified.reason == _loop.FailoverReason.content_policy_blocked:
                        agent._buffer_status("⚠️ Provider safety filter blocked this request — trying fallback...")
                    elif classified.reason == _loop.FailoverReason.ssl_cert_verification:
                        agent._buffer_status("⚠️ TLS certificate verification failed — trying fallback...")
                    else:
                        agent._buffer_status(f"⚠️ Non-retryable error (HTTP {status_code}) — trying fallback...")
                if agent._try_activate_fallback():
                    _ctx.active_system_prompt = _loop._sync_failover_system_message(
                        agent, api_messages, _ctx.active_system_prompt)
                    _cycle.retry_count = 0
                    _recovery.compression_attempts = 0
                    _cycle.recovery.primary_recovery_attempted = False
                    _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                    break
                if api_kwargs is not None:
                    agent._dump_api_request_debug(
                        api_kwargs, reason="non_retryable_client_error", error=api_error,
                    )
                # Terminal — flush buffered context so the user sees
                # what was tried before the abort.
                agent._flush_status_buffer()
                # Summarize once: Cloudflare/proxy HTML challenge pages and
                # other raw provider bodies must be collapsed to a short
                # one-liner here, otherwise the full page leaks into the
                # returned ``error`` field and downstream consumers deliver
                # it verbatim (e.g. a cron failure notification dumped a
                # ~60KB Cloudflare challenge page as 31 Discord messages).
                _nonretryable_summary = agent._summarize_api_error(api_error)
                if classified.reason == _loop.FailoverReason.content_policy_blocked:
                    agent._emit_status(
                        f"❌ Provider safety filter blocked this request: "
                        f"{_nonretryable_summary}"
                    )
                elif classified.reason == _loop.FailoverReason.ssl_cert_verification:
                    agent._emit_status(
                        f"❌ TLS certificate verification failed: "
                        f"{_nonretryable_summary}"
                    )
                else:
                    agent._emit_status(
                        f"❌ Non-retryable error (HTTP {status_code}): "
                        f"{_nonretryable_summary}"
                    )
                agent._vprint(f"{agent.log_prefix}❌ Non-retryable client error (HTTP {status_code}). Aborting.", force=True)
                agent._vprint(f"{agent.log_prefix}   🔌 Provider: {_provider}  Model: {_model}", force=True)
                agent._vprint(f"{agent.log_prefix}   🌐 Endpoint: {_base}", force=True)
                # Actionable guidance for common auth errors
                if classified.is_auth or classified.reason == _loop.FailoverReason.billing:
                    if classified.reason == _loop.FailoverReason.billing and _loop._print_billing_or_entitlement_guidance(
                        agent,
                        capability="model access",
                        provider=_provider,
                        base_url=str(_base),
                        model=_model,
                        unverified=classified.billing_unverified,
                    ):
                        pass
                    elif _provider == "nous" and _loop._print_nous_entitlement_guidance(
                        agent,
                        "Nous model access",
                    ):
                        pass
                    elif _provider in {"openai-codex", "xai-oauth", "nous"} and status_code == 401:
                        if _provider == "openai-codex":
                            agent._vprint(f"{agent.log_prefix}   💡 Codex OAuth token was rejected (HTTP 401). Your token may have been", force=True)
                            agent._vprint(f"{agent.log_prefix}      refreshed by another client (Codex CLI, VS Code). To fix:", force=True)
                            agent._vprint(f"{agent.log_prefix}      1. Run `codex` in your terminal to generate fresh tokens.", force=True)
                            agent._vprint(f"{agent.log_prefix}      2. Then run `hermes auth` to re-authenticate.", force=True)
                        elif _provider == "xai-oauth":
                            agent._vprint(f"{agent.log_prefix}   💡 xAI OAuth token was rejected (HTTP 401). To fix:", force=True)
                            agent._vprint(f"{agent.log_prefix}      re-authenticate with xAI Grok OAuth (SuperGrok / Premium+) from `hermes model`.", force=True)
                        else:  # nous
                            agent._vprint(f"{agent.log_prefix}   💡 Nous Portal OAuth token was rejected (HTTP 401). Your token may be", force=True)
                            agent._vprint(f"{agent.log_prefix}      expired, revoked, or your account may be out of credits. To fix:", force=True)
                            agent._vprint(f"{agent.log_prefix}      1. Re-authenticate: hermes portal", force=True)
                            agent._vprint(f"{agent.log_prefix}      2. Check your portal account: https://portal.nousresearch.com", force=True)
                            # ``:free`` is OpenRouter slug syntax; Nous Portal will reject
                            # the model name even after a successful re-auth.
                            if isinstance(_model, str) and _model.endswith(":free"):
                                agent._vprint(f"{agent.log_prefix}      ⚠️  Note: `{_model}` looks like an OpenRouter slug (`:free` suffix).", force=True)
                                agent._vprint(f"{agent.log_prefix}         Nous Portal won't recognize that model name. Either switch to a", force=True)
                                agent._vprint(f"{agent.log_prefix}         Nous catalog model, or run `/model openrouter:{_model}` to use OpenRouter.", force=True)
                    else:
                        agent._vprint(f"{agent.log_prefix}   💡 Your API key was rejected by the provider. Check:", force=True)
                        agent._vprint(f"{agent.log_prefix}      • Is the key valid? Run: hermes setup", force=True)
                        agent._vprint(f"{agent.log_prefix}      • Does your account have access to {_model}?", force=True)
                        if _loop.base_url_host_matches(str(_base), "openrouter.ai"):
                            agent._vprint(f"{agent.log_prefix}      • Check credits: https://openrouter.ai/settings/credits", force=True)
                else:
                    agent._vprint(f"{agent.log_prefix}   💡 This type of error won't be fixed by retrying.", force=True)
                # Content-policy blocks deserve their own actionable
                # guidance — neither "fix your API key" nor "retry won't
                # help" tells the user what to actually do. The provider
                # has refused this specific prompt, so the recovery is
                # either a rephrase or routing to a different model.
                if classified.reason == _loop.FailoverReason.content_policy_blocked:
                    agent._vprint(
                        f"{agent.log_prefix}   💡 The provider's safety filter rejected this specific prompt.",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      • Try rephrasing the request, narrowing the context, or splitting into smaller steps.",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      • Configure a fallback provider so future blocks route automatically:",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}        hermes fallback add   (interactive picker — same as `hermes model`)",
                        force=True,
                    )
                # TLS certificate failures are environment problems, not
                # provider/prompt problems — tell the user exactly which
                # knobs fix each common cause. Inspired by Claude Code
                # v2.1.199's immediate SSL fix hints.
                if classified.reason == _loop.FailoverReason.ssl_cert_verification:
                    agent._vprint(
                        f"{agent.log_prefix}   💡 The TLS certificate chain could not be verified. This fails the same",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      way on every retry — fix the environment, then try again:",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      • Corporate TLS-inspecting proxy? Point Python at its CA bundle:",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}        export SSL_CERT_FILE=/path/to/corp-ca.pem  (also REQUESTS_CA_BUNDLE)",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      • Missing/stale system CA store? Install/refresh it:",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}        pip install --upgrade certifi   (macOS: run 'Install Certificates.command')",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      • Self-signed local endpoint (llama.cpp, LM Studio, vLLM)? Use http://",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}        for localhost, or add the server's cert to your trust store.",
                        force=True,
                    )
                _loop.logger.error("%sNon-retryable client error: %s", agent.log_prefix, api_error)
                # Skip session persistence when the error is likely
                # context-overflow related (status 400 + large session).
                # Persisting the failed user message would make the
                # session even larger, causing the same failure on the
                # next attempt. (#1630)
                if status_code == 400 and (approx_tokens > 50000 or len(api_messages) > 80):
                    agent._vprint(
                        f"{agent.log_prefix}⚠️  Skipping session persistence "
                        f"for large failed session to prevent growth loop.",
                        force=True,
                    )
                else:
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                if classified.reason == _loop.FailoverReason.content_policy_blocked:
                    _policy_response = (
                        "⚠️  The model provider's safety filter blocked this request "
                        "(not a Hermes/gateway failure).\n\n"
                        f"Provider message: {_nonretryable_summary}\n\n"
                        f"{_loop._CONTENT_POLICY_RECOVERY_HINT}"
                    )
                    return _loop._complete_direct_turn(_turn_controller, _loop._content_policy_blocked_result(
                        _ctx.messages,
                        api_call_count,
                        final_response=_policy_response,
                        error_detail=_nonretryable_summary,
                    ))
                # Billing walls are the common non-retryable abort: enrich
                # the result with the same structured recovery descriptor as
                # the max-retries path so every surface (CLI, TUI, desktop)
                # renders one consistent billing signal.
                if classified.reason == _loop.FailoverReason.billing:
                    return _loop._complete_direct_turn(_turn_controller, _loop._billing_failure_result(
                        classified=classified,
                        summary=_nonretryable_summary,
                        messages=_ctx.messages,
                        api_call_count=api_call_count,
                        provider=_provider,
                        base_url=_base,
                        model=_model,
                    ))
                return _loop._complete_direct_turn(_turn_controller, {
                    "final_response": _nonretryable_summary,
                    "messages": _ctx.messages,
                    "api_calls": api_call_count,
                    "completed": False,
                    "failed": True,
                    "error": _nonretryable_summary,
                })

            if _cycle.retry_count >= _cycle.max_retries:
                # Before falling back, try rebuilding the primary
                # client once for transient transport errors (stale
                # connection pool, TCP reset).  Only attempted once
                # per API call block.
                if not _cycle.recovery.primary_recovery_attempted and agent._try_recover_primary_transport(
                    api_error, retry_count=_cycle.retry_count, max_retries=_cycle.max_retries,
                ):
                    _cycle.recovery.primary_recovery_attempted = True
                    _cycle.retry_count = 0
                    # Primary transport recovery starts a fresh attempt
                    # cycle. Re-open fallback state so a follow-on 429 can
                    # still activate fallback_providers after stale
                    # pre-recovery fallback/credential-pool bookkeeping.
                    _cycle.recovery.has_retried_429 = False
                    agent._fallback_index = 0
                    agent._fallback_activated = False
                    _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.PRIMARY_RECOVERY)
                    continue
                # Try fallback before giving up entirely
                if agent._has_pending_fallback():
                    agent._buffer_status(f"⚠️ Max retries ({_cycle.max_retries}) exhausted — trying fallback...")
                if agent._try_activate_fallback():
                    _ctx.active_system_prompt = _loop._sync_failover_system_message(
                        agent, api_messages, _ctx.active_system_prompt)
                    _cycle.retry_count = 0
                    _recovery.compression_attempts = 0
                    _cycle.recovery.primary_recovery_attempted = False
                    _cycle.restart(_turn_controller, _loop.TurnReason.PROVIDER_SWITCH)
                    break
                # Terminal — flush buffered retry/fallback trace.
                agent._flush_status_buffer()
                _final_summary = agent._summarize_api_error(api_error)
                _billing_guidance = ""
                if classified.reason == _loop.FailoverReason.billing:
                    if classified.billing_unverified:
                        # Ambiguous body (#82154) — hedge the terminal line.
                        agent._emit_status(
                            "❌ Provider reported usage/credit exhaustion "
                            f"(unverified — may be a content-filter rejection) — {_final_summary}"
                        )
                    else:
                        agent._emit_status(f"❌ Billing or credits exhausted — {_final_summary}")
                    _billing_guidance = _loop._billing_or_entitlement_message(
                        capability="model access",
                        provider=_provider,
                        base_url=str(_base),
                        model=_model,
                        unverified=classified.billing_unverified,
                    )
                    _loop._print_billing_or_entitlement_guidance(
                        agent,
                        capability="model access",
                        provider=_provider,
                        base_url=str(_base),
                        model=_model,
                        unverified=classified.billing_unverified,
                    )
                elif is_rate_limited:
                    agent._emit_status(f"❌ Rate limited after {_cycle.max_retries} retries — {_final_summary}")
                else:
                    agent._emit_status(f"❌ API failed after {_cycle.max_retries} retries — {_final_summary}")
                agent._vprint(f"{agent.log_prefix}   💀 Final error: {_final_summary}", force=True)

                # Detect SSE stream-drop pattern (e.g. "Network
                # connection lost") and surface actionable guidance.
                # This typically happens when the model generates a
                # very large tool call (write_file with huge content)
                # and the proxy/CDN drops the stream mid-response.
                _is_stream_drop = (
                    not getattr(api_error, "status_code", None)
                    and any(p in error_msg for p in (
                        "connection lost", "connection reset",
                        "connection closed", "network connection",
                        "network error", "terminated",
                    ))
                )
                if _is_stream_drop:
                    agent._vprint(
                        f"{agent.log_prefix}   💡 The provider's stream "
                        f"connection keeps dropping. This often happens "
                        f"when the model tries to write a very large "
                        f"file in a single tool call.",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      Try asking the model "
                        f"to use execute_code with Python's open() for "
                        f"large files, or to write the file in smaller "
                        f"sections.",
                        force=True,
                    )

                # Detect thinking-timeout pattern: a known reasoning model
                # hit a transport-layer error before the first content
                # token arrived.  Distinct from _is_stream_drop above
                # (which fires for large file-write stream drops) and
                # from any classifier reason that's not a transport
                # timeout.  Reuses the reasoning-model allowlist from
                # agent/reasoning_timeouts.py (Fixes #52217) so the
                # trigger is consistent with what the per-model
                # stale-timeout floor covers.  After the classifier
                # override at agent/error_classifier.py:720-738 (this
                # PR), transport disconnects on reasoning models route
                # to FailoverReason.timeout rather than
                # context_overflow, so this branch actually fires.
                # Detection and message text live in
                # agent.thinking_timeout_guidance so they're
                # unit-testable without driving the full retry loop.
                # (Part 2 of Fixes #52310.)
                from agent.thinking_timeout_guidance import (
                    is_thinking_timeout,
                )
                _is_thinking_timeout = is_thinking_timeout(
                    classified,
                    _model,
                    error_msg,
                )
                if _is_thinking_timeout:
                    agent._vprint(
                        f"{agent.log_prefix}   💡 The model's thinking "
                        f"phase exceeded the upstream proxy's idle "
                        f"timeout before the first content token "
                        f"arrived. This is a known issue with "
                        f"reasoning models behind cloud gateways "
                        f"(NVIDIA NIM, OpenAI, Anthropic, DeepSeek).",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      Workarounds in priority order:",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      1. Set "
                        f"`providers.{_provider}.models.{_model}.stale_timeout_seconds: 900` "
                        f"in `~/.hermes/config.yaml` to extend the per-call "
                        f"timeout. (Hermes's built-in floor is 600s for "
                        f"known reasoning models — if you still see this "
                        f"after raising, the upstream cap is even shorter.)",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      2. Lower `reasoning_budget` or set "
                        f"`reasoning_effort: medium` on this model if the provider supports it.",
                        force=True,
                    )
                    agent._vprint(
                        f"{agent.log_prefix}      3. Use a smaller / faster reasoning "
                        f"model if the task doesn't require deep thinking.",
                        force=True,
                    )

                _loop.logger.error(
                    "%sAPI call failed after %s retries. %s | provider=%s model=%s msgs=%s tokens=~%s",
                    agent.log_prefix, _cycle.max_retries, _final_summary,
                    _provider, _model, len(api_messages), f"{approx_tokens:,}",
                )
                if api_kwargs is not None:
                    agent._dump_api_request_debug(
                        api_kwargs, reason="max_retries_exhausted", error=api_error,
                    )
                agent._persist_session(_ctx.messages, _ctx.conversation_history)
                _billing_block = None
                _billing_unverified = False
                if classified.reason == _loop.FailoverReason.billing:
                    _billing_unverified = classified.billing_unverified
                    _final_response = _loop._billing_terminal_label(
                        _final_summary, _billing_unverified
                    )
                    if _billing_guidance:
                        _final_response += f"\n\n{_billing_guidance}"
                    # Structured recovery descriptor so every surface renders
                    # the same link + label from one signal (see helper).
                    _billing_block = _loop._billing_block_dict(
                        _provider, _base, _model, _billing_guidance,
                        unverified=_billing_unverified,
                    )
                else:
                    _final_response = f"API call failed after {_cycle.max_retries} retries: {_final_summary}"
                if _is_thinking_timeout:
                    # Thinking-timeout guidance overrides the generic
                    # stream-drop guidance — the latter is wrong for
                    # this case (it suggests splitting large file
                    # writes, which isn't what happened).  See the
                    # reasoning-model override at
                    # agent/error_classifier.py:720-738 and the
                    # detection block above for context.
                    from agent.thinking_timeout_guidance import (
                        build_thinking_timeout_guidance,
                    )
                    _final_response += build_thinking_timeout_guidance(
                        provider=_provider,
                        model=_model,
                    )
                elif _is_stream_drop:
                    _final_response += (
                        "\n\nThe provider's stream connection keeps "
                        "dropping — this often happens when generating "
                        "very large tool call responses (e.g. write_file "
                        "with long content). Try asking me to use "
                        "execute_code with Python's open() for large "
                        "files, or to write in smaller sections."
                    )
                return _loop._complete_direct_turn(_turn_controller, {
                    "final_response": _final_response,
                    "messages": _ctx.messages,
                    "api_calls": api_call_count,
                    "completed": False,
                    "failed": True,
                    "error": _final_summary,
                    # Surface the classified reason so callers (notably the
                    # kanban worker path in cli.py) can distinguish a
                    # transient throttle from a real failure and choose a
                    # different exit code. ``rate_limit`` / ``billing`` here
                    # mean "quota wall, not a task error".
                    "failure_reason": classified.reason.value,
                    # True when the billing verdict rests on an ambiguous
                    # body (#82154) — may be a content-filter rejection.
                    "billing_unverified": _billing_unverified,
                    # Present only for billing walls: structured recovery
                    # descriptor (provider, billing_url, is_nous, message).
                    "billing_block": _billing_block,
                })

            # For rate limits, respect the Retry-After header if present
            _retry_after = None
            if is_rate_limited:
                _resp_headers = getattr(getattr(api_error, "response", None), "headers", None)
                if _resp_headers and hasattr(_resp_headers, "get"):
                    _ra_raw = _resp_headers.get("retry-after") or _resp_headers.get("Retry-After")
                    if _ra_raw:
                        try:
                            # Cap at 10 minutes. Anthropic Tier 1 input-token
                            # buckets reset in ~171s, so a 120s cap caused us to
                            # retry before the actual reset window and re-trip the
                            # limit. 600s covers all realistic provider reset
                            # windows while still rejecting pathological values. (#26293)
                            _retry_after = min(float(_ra_raw), 600)
                        except (TypeError, ValueError):
                            pass
            wait_time = _retry_after if _retry_after else _loop.jittered_backoff(_cycle.retry_count, base_delay=2.0, max_delay=60.0)
            _backoff_policy = None
            if (is_rate_limited or _is_zai_coding_overload) and not _retry_after:
                wait_time, _backoff_policy = _loop.adaptive_rate_limit_backoff(
                    _cycle.retry_count,
                    base_url=str(_base),
                    model=_model,
                    error=api_error,
                    default_wait=wait_time,
                )
            if is_rate_limited or _is_zai_coding_overload:
                _policy_note = ""
                if _backoff_policy == "zai_coding_overload_long":
                    _policy_note = " (Z.AI Coding overload adaptive long backoff)"
                elif _backoff_policy == "zai_coding_overload_short":
                    _policy_note = " (Z.AI Coding overload short retry)"
                _wait_reason = "Provider overloaded" if _is_zai_coding_overload and not is_rate_limited else "Rate limited"
                _rate_limit_status = f"⏱️ {_wait_reason}. Waiting {wait_time:.1f}s (attempt {_cycle.retry_count + 1}/{_cycle.max_retries}){_policy_note}..."
                # Normal retries are buffered to avoid noisy transient chatter. Long
                # Z.AI Coding waits are different: they can last minutes, so surface
                # progress immediately instead of making the TUI look frozen.
                if _backoff_policy == "zai_coding_overload_long":
                    agent._emit_status(_rate_limit_status)
                else:
                    agent._buffer_status(_rate_limit_status)
            else:
                agent._buffer_status(f"⏳ Retrying in {wait_time:.1f}s (attempt {_cycle.retry_count}/{_cycle.max_retries})...")
            _loop.logger.warning(
                "Retrying API call in %ss (attempt %s/%s) %s policy=%s error=%s",
                wait_time,
                _cycle.retry_count,
                _cycle.max_retries,
                agent._client_log_context(),
                _backoff_policy or "default",
                api_error,
            )
            # Sleep in small increments so we can respond to interrupts quickly
            # instead of blocking the entire wait_time in one sleep() call
            sleep_end = _loop.time.time() + wait_time
            _backoff_touch_counter = 0
            while _loop.time.time() < sleep_end:
                if agent._interrupt_requested:
                    # Same preserve-redirect rule as the retry-wait above:
                    # a steering correction must survive backoff, not die
                    # as "Operation interrupted".
                    if agent.clear_interrupt(preserve_redirect=True):
                        _cycle.restart(_turn_controller, _loop.TurnReason.REDIRECT)
                        break
                    agent._vprint(f"{agent.log_prefix}⚡ Interrupt detected during retry wait, aborting.", force=True)
                    _interrupt_text = f"Operation interrupted: retrying API call after error (retry {_cycle.retry_count}/{_cycle.max_retries})."
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
                _loop.time.sleep(0.2)  # Check interrupt every 200ms
                # Touch activity every ~30s so the gateway's inactivity
                # monitor knows we're alive during backoff waits.
                _backoff_touch_counter += 1
                if _backoff_touch_counter % 150 == 0:  # 150 × 0.2s = 30s
                    agent._touch_activity(
                        f"error retry backoff ({_cycle.retry_count}/{_cycle.max_retries}), "
                        f"{int(sleep_end - _loop.time.time())}s remaining"
                    )
            if _cycle.has_restart(_loop.TurnReason.REDIRECT):
                # Leave the retry loop — the check right below rebuilds this
                # iteration from the correction instead of re-firing the
                # stale request.
                break
            _turn_controller.move(_loop.TransitionKind.RETRY_ATTEMPT, _loop.TurnReason.GENERIC_RETRY)

    return RequestOutcome(
        response=response, api_kwargs=api_kwargs, api_messages=api_messages,
        api_duration=api_duration, interrupted=interrupted,
        final_response=final_response,
    )

"""The agent conversation loop — extracted from ``run_agent.AIAgent``.

This is the biggest single chunk pulled out of ``run_agent.py``: the
roughly 3,900-line :func:`run_conversation` body that drives one user
turn through the agent (model call, tool dispatch, retries, fallbacks,
compression, post-turn hooks, background memory/skill review nudges).

The function takes the parent ``AIAgent`` instance as its first
argument (``agent``) and accesses its state via attribute lookup.
``_ra().AIAgent.run_conversation`` is now a thin forwarder.

Symbols that production code or tests patch on ``run_agent`` directly
(``handle_function_call``, ``_set_interrupt``, ``OpenAI``, ...) are
resolved through :func:`_ra` so those patches keep working.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import ssl
import time
from typing import Any, Dict, List, Optional

from agent.codex_responses_adapter import _summarize_user_message_for_log
from agent.conversation_compression import (
    COMPRESSION_RETRY_CONTEXT_REDUCED_STATUS_TEMPLATE,
    COMPRESSION_RETRY_MESSAGES_STATUS_TEMPLATE,
    COMPRESSION_RETRY_TOKENS_STATUS_TEMPLATE,
    COMPRESSION_RETRY_TOO_LARGE_STATUS_TEMPLATE,
    PRE_API_COMPRESSION_STATUS_TEMPLATE,
    compression_skipped_due_to_lock,
    conversation_history_after_compression,
)
from agent.context_engine import automatic_compaction_status_message
from agent.display import KawaiiSpinner
from agent.error_classifier import FailoverReason, classify_api_error
from agent.message_metadata import append_message
from agent.turn_context import (
    _compression_warrants_another_preflight_pass,
    build_turn_context,
    compose_user_api_content,
    reanchor_current_turn_user_idx,
)
from agent.request_cycle import RequestCycle, TurnRecovery, run_request_cycle
from agent.turn_continuation import TurnContinuation, handle_text_response
from agent.turn_state_machine import (
    TurnController, TurnPhase, TurnReason, TurnTransition, TransitionKind, DurabilityBoundary, BudgetEffect,
    complete_direct_turn as _complete_direct_turn,
    complete_finalized_turn as _complete_finalized_turn,
    resolve_tool_completion,
)
from agent.semantic_progress import (
    SEMANTIC_PROGRESS_HALT,
    SEMANTIC_PROGRESS_NUDGE,
    SemanticProgressTracker,
    SemanticRoundObservation,
)
from agent.runtime_cwd import resolve_agent_cwd
from agent.message_sanitization import (
    close_interrupted_tool_sequence,
    _repair_tool_call_arguments,
    _sanitize_messages_non_ascii,
    _sanitize_messages_surrogates,
    _sanitize_structure_non_ascii,
    _sanitize_structure_surrogates,
    _sanitize_surrogates,
    _sanitize_tools_non_ascii,
    _strip_images_from_messages,
    _strip_non_ascii,
)
# Must mirror _STALE_TOOL_CALL_MARKER_RE in hermes_state.py — kept local
# to avoid importing hermes_state at module load time (its module-level
# DEFAULT_DB_PATH = get_hermes_home() / "state.db" breaks tests that
# monkeypatch get_hermes_home to return a str).
_STALE_MARKER_RE = re.compile(r"^\[[A-Za-z_][A-Za-z0-9_.-]*\]$")
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    _estimate_tools_tokens_rough,
    estimate_messages_tokens_rough,
    estimate_request_tokens_rough,
    get_context_length_from_provider_error,
    is_output_cap_error,
    parse_available_output_tokens_from_error,
    save_context_length,
)
from agent.process_bootstrap import _install_safe_stdio
from agent.prompt_caching import (
    build_prompt_cache_plan,
    effective_cache_ttl,
    strip_anthropic_cache_control,
    strip_anthropic_tool_cache_control,
)
from agent.retry_utils import (
    adaptive_rate_limit_backoff,
    is_zai_coding_overload_error,
    jittered_backoff,
    zai_coding_overload_retry_ceiling,
)
from agent.repetition_guard import is_repetition_dominated
from agent.trajectory import has_incomplete_scratchpad
# Bind before the turn starts so a source-tree swap cannot load a skewed
# finalizer at turn end.
from agent.turn_finalizer import finalize_turn
from agent.usage_pricing import estimate_usage_cost, normalize_usage
from agent import empty_response_guard as _empty_guard
from hermes_constants import PARTIAL_STREAM_STUB_ID
from agent.transport_recovery import (
    TransportRecoveryAction,
    TransportRecoveryState,
    is_transport_interrupted,
    is_transport_retryable_error,
    plan_transport_recovery,
    transport_had_visible_text,
)
from hermes_logging import set_session_context
from tools.skill_provenance import set_current_write_origin
from utils import base_url_host_matches, env_var_enabled

logger = logging.getLogger(__name__)


# Scaffold marker used by _apply_active_turn_redirect and the ghost-row filter
# in the api_messages loop. Module-level so both sites can never drift.
_INTERRUPT_SCAFFOLD_MARKER = "[This response was interrupted by a user correction.]"


def _restore_user_after_reference_handoff(
    messages: List[Dict[str, Any]], user_message: Any
) -> bool:
    """Re-append this turn's real user ask when compaction left only a handoff.

    Returns True when a restore append happened. The caller has already
    established that a reference-only handoff would drive the next model
    call (#80622); this helper only decides whether a restorable ask exists.
    """
    if user_message is None:
        return False
    if isinstance(user_message, str):
        if not user_message.strip():
            return False
        content: Any = user_message
    elif isinstance(user_message, list):
        if not user_message:
            return False
        content = user_message
    else:
        return False
    if (
        messages
        and isinstance(messages[-1], dict)
        and messages[-1].get("role") == "user"
        and messages[-1].get("content") == content
    ):
        return False
    append_message(messages, {"role": "user", "content": content})
    return True


def _should_skip_model_call_for_reference_handoff(
    messages: List[Dict[str, Any]], user_message: Any
) -> bool:
    """Guard post-compaction continues against sole-handoff active turns (#80622)."""
    from agent.context_compressor import reference_handoff_would_drive_next_model_call

    if not reference_handoff_would_drive_next_model_call(messages):
        return False
    if _restore_user_after_reference_handoff(messages, user_message):
        # The restored ask is an actionable non-synthetic user row appended
        # after the handoff — by construction the handoff no longer drives.
        return False
    return True


# Fallback final_response for a turn ended by the sole-handoff skip (#80622).
# Deliberately NOT a replay of the last assistant text: finalize_turn's
# non-assistant-tail chokepoint (#43849) appends final_response as a fresh
# assistant row, so recovering the previous turn's prose here would duplicate
# it in the durable transcript AND re-deliver it to the user as if it were
# this turn's answer. A short status is honest and idempotent.
_HANDOFF_SKIP_FINAL_RESPONSE = (
    "Context was compacted. The previous response is complete — "
    "awaiting your next message."
)


# Stable prefix of the local interrupt status string emitted when a turn is
# cancelled while waiting on the provider. Surfaces (ACP, TUI) match on this
# to treat it as cancellation metadata rather than assistant prose.
INTERRUPT_WAITING_FOR_MODEL_PREFIX = "Operation interrupted: waiting for model response ("


def _should_rearm_compression_budget(
    compression_attempts: int,
    *,
    completed_compaction_pending: bool,
    prompt_tokens: int,
    threshold_tokens: int,
) -> bool:
    """Return True after a provider proves a completed compaction worked.

    Rough estimates cannot safely rearm the anti-thrash budget: they can dip
    below the threshold while the provider-visible prompt remains too large.
    Require the completed-compaction latch plus a positive, normalized prompt
    count below the threshold from the next successful provider response.
    """
    return bool(
        compression_attempts
        and completed_compaction_pending
        and threshold_tokens > 0
        and 0 < prompt_tokens < threshold_tokens
    )


# Modules that indicate a deterministic local processing error when they
# appear in an exception traceback WITHOUT any API-call module. Used by the
# outer-loop error classifier to avoid retrying bugs that will fail
# identically every time (e.g. TypeError from passing list content into a
# regex helper).  IMPORTANT: do NOT include "conversation_loop" or
# "run_agent" here — those are the container modules for the try/except
# itself, so every exception passes through them, which would make
# _hit_local always True and misclassify transient API/network errors as
# non-retryable local bugs. (#66267)
_LOCAL_PROCESSING_MODULES = frozenset({
    "agent_runtime_helpers",
    "message_content",
    "message_sanitization",
    "chat_completion_helpers",  # only local when NOT also an API-call module
})
_API_CALL_MODULES = frozenset({
    "chat_completion_helpers",
})


def _moa_client_consumes_prepared_request(client: Any) -> bool:
    """True when ``client`` is the in-process MoA facade.

    ``_moa_prepared_request`` is a private handshake with
    ``MoAChatCompletions.create``, and only that facade exposes ``prepare()``.
    Every other chat-completions object raises TypeError on the unexpected
    keyword — including the native OpenAI client that credential rotation,
    provider fallback and dead-connection cleanup rebuild from
    ``_client_kwargs`` while ``agent.provider`` stays ``"moa"``.
    """
    completions = getattr(getattr(client, "chat", None), "completions", None)
    return callable(getattr(completions, "prepare", None))


def _join_truncated_parts(parts: List[str]) -> str:
    """Join continuation fragments, adding a newline where two would glue together (#78577)."""
    joined = ""
    for part in parts:
        if joined and not joined[-1].isspace() and part and not part[0].isspace():
            joined += "\n"
        joined += part
    return joined



def _strip_continuation_scaffold(messages: list, current_turn_user_idx) -> None:
    """Drop synthetic continuation rows belonging to the current turn.

    Unanswered continuation scaffolding (the checkpointed fragment plus the
    synthetic "continue" nudge) steers every later turn back into a response
    that never completed, so a spent transport incident must leave none of it
    behind. Scoped to the current turn so earlier turns stay untouched.
    """
    start = (
        current_turn_user_idx + 1
        if isinstance(current_turn_user_idx, int) and current_turn_user_idx >= 0
        else 0
    )
    messages[start:] = [
        m for m in messages[start:]
        if not (
            isinstance(m, dict)
            and (
                m.get("_length_continuation_fragment")
                or m.get("_length_continuation_nudge")
            )
        )
    ]


def _transport_exhausted_result(
    agent,
    *,
    messages: list,
    conversation_history,
    truncated_response_parts: List[str],
    current_turn_user_idx,
    api_call_count: int,
    effective_task_id,
    error_text: str,
) -> dict:
    """Terminal result for a transport incident whose budget is spent.

    Shared by the swallowed-stub path and the raw-exception path so both leave
    the transcript in the same clean state: no synthetic scaffolding, honest
    partial text preserved, any interrupted tool sequence closed, session
    persisted. Returning one helper keeps the two entry points from drifting.
    """
    agent._flush_status_buffer()
    partial = agent._strip_think_blocks(
        _join_truncated_parts(truncated_response_parts)
    ).strip()
    _strip_continuation_scaffold(messages, current_turn_user_idx)
    if partial:
        append_message(messages, {
            "role": "assistant",
            "content": partial,
            "finish_reason": "length",
        })
    agent._session_messages = messages
    # A prior tool batch can leave a tool-result tail; this path never
    # reaches finalize_turn (#48879).
    close_interrupted_tool_sequence(
        messages,
        "Stream connection dropped; the turn did not complete",
    )
    agent._cleanup_task_resources(effective_task_id)
    agent._persist_session(messages, conversation_history)
    return {
        "final_response": partial or None,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "partial": True,
        "error": error_text,
    }


def _moa_reference_metrics_for_hook(agent: Any) -> Any:
    """Per-advisor metrics for post_api_request, or None off the MoA path.

    MoA runs N advisor models before its aggregator and returns only the
    aggregator's response, so an observability plugin sees one generation for
    the whole fan-out. The advisor spend is already computed per slot (see
    ``_RefAccounting``); this only carries it across the hook boundary.
    """
    client = getattr(agent, "client", None)
    getter = getattr(client, "last_reference_metrics", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _apply_active_turn_redirect(agent: Any, messages: List[Dict[str, Any]], text: str) -> None:
    """Append a provider-safe checkpoint and correction to the live turn.

    Incomplete provider reasoning blocks are not valid replay items (Anthropic
    signs them; Responses reasoning items require their following output).
    Preserve only the *visible* response text, demoted to ordinary text, then
    add the correction as a real user message. This keeps role alternation
    valid and leaves every previously cached message byte-for-byte unchanged.

    INVARIANT — raw chain-of-thought must never be serialized into replayable
    message content. Streamed reasoning is display-only state: it may be shown
    live, but it does not re-enter the transcript as assistant (or user) text.
    An assistant turn whose content inlines its own chain-of-thought reads to
    Anthropic's output classifier as reasoning-injection/prefill jailbreak,
    and because the poisoned checkpoint is persisted and replayed on every
    subsequent call, the session dies permanently with deterministic
    "Provider returned an empty response" storms that no retry, nudge, or
    empty-recovery branch can escape (July 2026: four sessions bricked this
    way; every reasoning-free checkpoint that week was untouched — same
    mechanism as the ~/.hermes/prefill.json incident, 20/20 blocked with
    assistant-exposed CoT vs 0/20 without). The interrupted reasoning was
    incomplete by definition; the model regenerates it on the retried turn.
    If a future path needs to preserve interrupted thinking, carry it in a
    provider-gated reasoning *field*, never in content.
    INVARIANT — the scaffolding is provider-replay text, not transcript text.
    ``[This response was interrupted by a user correction.]`` and its
    ``Visible response before the interruption:`` header exist so the MODEL
    understands its own reply was cut off. They are not prose the user wrote
    or the agent said. Persisting them into an assistant row's ``content`` or
    ``api_content`` made the model treat the scaffold as *its own previous
    reply*, echo it, and self-replicate ghost rows across turns (#81841).
    Carry the scaffolded form only in the *user correction's* ``api_content``
    sidecar — never on the placeholder assistant row. When nothing was on
    screen the placeholder is marked ``display_kind="hidden"`` (empty
    content) so every transcript surface drops it, exactly like
    compaction-reference rows.
    """
    visible = agent._strip_think_blocks(
        getattr(agent, "_current_streamed_assistant_text", "") or ""
    ).strip()

    checkpoint_parts = [_INTERRUPT_SCAFFOLD_MARKER]
    if visible:
        checkpoint_parts.extend(
            ["Visible response before the interruption:", visible]
        )
    checkpoint = "\n\n".join(checkpoint_parts)
    correction = (
        "[Context from the interrupted assistant response]\n"
        f"{checkpoint}\n\n"
        f"{text}"
    )

    # The normal live tail is user or tool, so an assistant placeholder
    # followed by the correction preserves strict alternation. If a transport
    # already committed an assistant item, attribute the checkpoint inside the
    # user correction instead of creating assistant→assistant.
    if messages and messages[-1].get("role") == "assistant":
        # Transcript shows the user's own words; the provider replays the
        # scaffolded form so it still sees the interrupted context.
        append_message(
            messages,
            {"role": "user", "content": text, "api_content": correction},
        )
    else:
        # Placeholder preserves role alternation only. Scaffold bytes must
        # never land here — the API replay path substitutes api_content back
        # into content, and a scaffold-as-assistant-reply is what the model
        # then echoes (#81841 / incomplete #73146 else branch).
        placeholder: Dict[str, Any] = {
            "role": "assistant",
            "content": visible or "",
        }
        if not visible:
            placeholder["display_kind"] = "hidden"
        append_message(messages, placeholder)
        append_message(
            messages,
            {"role": "user", "content": text, "api_content": correction},
        )

    agent._current_streamed_assistant_text = ""
    agent._stream_needs_break = True


def _is_copilot_provider(agent: Any) -> bool:
    """Delegate to ``AIAgent._is_copilot_provider`` (single owner of the check).

    ``agent.provider`` is not always the normalized ``copilot`` slug —
    ``/model`` and profile configs can leave the alias ``github-copilot`` (or
    ``github``) in place, and a bare ``provider == "copilot"`` gate silently
    skips credential recovery for those spellings.
    """
    try:
        return bool(agent._is_copilot_provider())
    except Exception:
        return (getattr(agent, "provider", "") or "").strip().lower() in {
            "copilot",
            "github-copilot",
            "github",
        }


def _is_stale_copilot_credential_error(status_code: Optional[int], error_message: str) -> bool:
    """Detect a Copilot 400 that is really a STALE / DEGRADED credential.

    Copilot surfaces a stale or degraded credential as an HTTP 400 rather than a
    clean 401. Two body markers indicate this class:

    - ``model_not_available_for_integrator`` — the request reached the
      restricted ``copilot-language-server`` integrator (the server's fallback
      when it receives a raw OAuth token instead of an exchanged API token),
      whose model allowlist omits enterprise-only models.
    - ``model_not_supported`` / "the requested model is not supported" — the
      cached bearer's Copilot entitlement rotated out from under a long-lived
      process.

    Matched narrowly (status 400 AND a specific marker) so a genuinely wrong
    model name — a real 400 — never triggers the single-shot re-exchange. The
    caller enforces copilot-provider scoping and the single-shot guard.
    """
    lowered = (error_message or "").lower()
    is_400 = status_code == 400 or "error code: 400" in lowered
    if not is_400:
        return False
    return (
        "model_not_available_for_integrator" in lowered
        or "not available for integrator" in lowered
        or "model_not_supported" in lowered
        or "the requested model is not supported" in lowered
    )


def _image_error_max_dimension(error: Exception) -> Optional[int]:
    """Extract a provider-reported image dimension ceiling, if present."""
    parts = []
    for value in (
        error,
        getattr(error, "message", None),
        getattr(error, "body", None),
    ):
        if value:
            try:
                parts.append(str(value))
            except Exception:
                pass
    text = " ".join(parts).lower()
    if "image" not in text or "dimension" not in text or "max allowed size" not in text:
        return None

    match = re.search(r"max allowed size(?:\s+for [^:]+)?:\s*(\d{3,5})\s*pixels?", text)
    if not match:
        return None
    try:
        max_dimension = int(match.group(1))
    except ValueError:
        return None
    if 512 <= max_dimension <= 8000:
        return max_dimension
    return None


def _ollama_context_limit_error(agent: Any, request_tokens: int) -> Optional[str]:
    """Return a user-facing error when Ollama is loaded with too little context."""
    if not getattr(agent, "tools", None):
        return None

    runtime_ctx = getattr(agent, "_ollama_num_ctx", None)
    if not isinstance(runtime_ctx, int) or runtime_ctx <= 0:
        return None
    if runtime_ctx >= MINIMUM_CONTEXT_LENGTH:
        return None

    model = getattr(agent, "model", "") or "the selected model"
    base_url = getattr(agent, "base_url", "") or "unknown base URL"
    provider = getattr(agent, "provider", "") or "unknown"
    tool_count = len(getattr(agent, "tools", None) or [])

    logger.warning(
        "Ollama runtime context too small for Hermes tool use: "
        "model=%s provider=%s base_url=%s runtime_context=%d "
        "minimum_context=%d estimated_request_tokens=%d tool_count=%d "
        "session=%s",
        model,
        provider,
        base_url,
        runtime_ctx,
        MINIMUM_CONTEXT_LENGTH,
        request_tokens,
        tool_count,
        getattr(agent, "session_id", None) or "none",
    )

    return (
        f"Ollama loaded `{model}` with only {runtime_ctx:,} tokens of runtime "
        f"context, but Hermes needs at least {MINIMUM_CONTEXT_LENGTH:,} tokens "
        "for reliable tool use.\n\n"
        "Increase the Ollama context for this model and restart/reload the "
        "model before trying again. A known-good starting point is 65,536 "
        "tokens. In Hermes config, set `model.ollama_num_ctx: 65536` "
        "(and `model.context_length: 65536` if you also override the displayed "
        "model context). If you manage the model through an Ollama Modelfile, "
        "set `PARAMETER num_ctx 65536` there instead."
    )


def _ra():
    """Lazy reference to ``run_agent`` so callers can patch
    ``run_agent.handle_function_call`` / ``run_agent._set_interrupt`` /
    ``run_agent.OpenAI`` and have those patches reach this code path.
    """
    import run_agent
    return run_agent


def _nous_entitlement_message(capability: str) -> str:
    try:
        from hermes_cli.nous_account import (
            format_nous_portal_entitlement_message,
            get_nous_portal_account_info,
        )

        account_info = get_nous_portal_account_info(force_fresh=True)
        message = format_nous_portal_entitlement_message(
            account_info,
            capability=capability,
        )
        return message or ""
    except Exception:
        return ""


def _print_nous_entitlement_guidance(agent, capability: str) -> bool:
    message = _nous_entitlement_message(capability)
    if not message:
        return False
    for line in message.splitlines():
        agent._vprint(f"{agent.log_prefix}   💡 {line}", force=True)
    return True


def _system_prompt_for_hooks(api_kwargs: Any, request_messages: Any) -> Any:
    """System prompt as actually sent to the provider, for observability hooks.

    Providers move it out of ``messages``: Anthropic Messages uses a separate
    ``system`` kwarg (str or content-block list), the Responses/Codex API uses
    top-level ``instructions``; Chat Completions keeps it as ``messages[0]``.
    Returns None when the request carries no system prompt.
    """
    system_prompt = api_kwargs.get("system")
    if system_prompt is None:
        system_prompt = api_kwargs.get("instructions")
    if system_prompt is None and isinstance(request_messages, list) and request_messages:
        first = request_messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            system_prompt = first.get("content")
    return system_prompt


def _is_nous_inference_route(provider: str, base_url: str) -> bool:
    provider = (provider or "").strip().lower()
    if provider == "nous":
        return True
    base = str(base_url or "")
    return (
        base_url_host_matches(base, "inference-api.nousresearch.com")
    )


def _billing_or_entitlement_message(
    *,
    capability: str,
    provider: str,
    base_url: str,
    model: str,
    unverified: bool = False,
) -> str:
    if _is_nous_inference_route(provider, base_url):
        return _nous_entitlement_message(capability)

    provider_label = (provider or "").strip() or "the selected provider"
    model_label = (model or "").strip() or "the selected model"

    # Anthropic Claude Pro/Max OAuth subscriptions surface exhaustion of the
    # metered "extra usage" bucket as a hard 400 ("You're out of extra
    # usage"). Point at the exact settings page and note the cycle-reset
    # option, since the generic "add credits with that provider" line doesn't
    # apply to a subscription — the user waits for the reset or switches to an
    # API key.
    if (provider or "").strip().lower() == "anthropic":
        # ``unverified`` (ClassifiedError.billing_unverified, #82154): the
        # "out of extra usage" 400 is ambiguous — Anthropic returns the same
        # body when its server-side content filter rejects part of the request
        # on a subscription OAuth token, so the message reliably misdirects
        # diagnosis toward buying quota. Hedge the claim and name the other
        # cause. A confirmed verdict (e.g. a real 402 or an API-key credit
        # depletion) keeps the assertive wording.
        if unverified:
            lines = [
                (
                    f"{provider_label} reported that your Claude subscription usage may be "
                    f"exhausted for {model_label} (included quota + extra-usage credits) — "
                    "but this specific error is not proof of a billing problem."
                ),
                "If https://claude.ai/settings/usage still shows quota remaining, this is "
                "probably NOT a billing problem: on a Claude subscription (OAuth) token "
                "Anthropic returns this same message when its content filter rejects part "
                "of the request — typically a phrase in the system prompt.",
                "If usage really is exhausted: wait for the billing cycle to reset, or add "
                "extra usage at https://claude.ai/settings/usage",
                "You can also switch to an Anthropic API key or another provider with "
                "/model <model> --provider <provider>.",
                # The exhaustion latch replays the stored error without issuing
                # a request, so a real fix looks like it didn't work.
                "Retry with a fresh credential state: `hermes auth reset anthropic`. Until "
                "that cooldown clears, this error can be replayed from cache without "
                "contacting the API.",
            ]
        else:
            lines = [
                (
                    f"{provider_label} reported that your Claude subscription usage is "
                    f"exhausted for {model_label} (included quota + extra-usage credits)."
                ),
                "Options: wait for the billing cycle to reset, or add extra usage at "
                "https://claude.ai/settings/usage",
                "You can also switch to an Anthropic API key or another provider with "
                "/model <model> --provider <provider>.",
            ]
        return "\n".join(lines)

    # Provider-agnostic billing URL derivation (OpenAI, DeepSeek, xAI, Groq,
    # OpenRouter, …) so every text surface — CLI, gateway messaging, TUI
    # transcript — shows the same actionable link, not just OpenRouter.
    try:
        from agent.billing_links import build_billing_block

        _link = build_billing_block(provider=provider, base_url=base_url, model=model)
        if _link.provider_label:
            provider_label = _link.provider_label
        billing_url = _link.billing_url
    except Exception:
        billing_url = None

    lines = [
        (
            f"{provider_label} reported that billing, credits, or account "
            f"entitlement is exhausted for {model_label}."
        ),
        "Add credits or update billing with that provider, then retry.",
    ]
    if billing_url:
        lines.append(f"{provider_label} billing: {billing_url}")
    lines.append("You can switch providers temporarily with /model <model> --provider <provider>.")
    return "\n".join(lines)


def _billing_block_dict(
    provider, base_url, model, message="", *, unverified: bool = False
) -> Optional[dict]:
    """Best-effort structured billing descriptor (None if billing_links is unavailable)."""
    try:
        from agent.billing_links import build_billing_block

        block = build_billing_block(
            provider=provider, base_url=str(base_url), model=model, message=message
        ).to_dict()
    except Exception:
        return None
    if block is not None and unverified:
        # Carry the classifier's ambiguity into the structured descriptor so
        # every surface rendering the block can hedge too (#82154).
        block["unverified"] = True
    return block


def _billing_terminal_label(summary: str, unverified: bool) -> str:
    """Terminal-failure prefix for a billing-classified error.

    ``unverified`` (#82154): the Anthropic "out of extra usage" 400 can be a
    content-filter rejection, so the terminal line must not assert billing
    exhaustion as fact.
    """
    if unverified:
        return (
            "Provider reported usage/credit exhaustion (unverified — the same "
            f"error can be a content-filter rejection, not billing): {summary}"
        )
    return f"Billing or credits exhausted: {summary}"


def _billing_failure_result(
    *,
    classified,
    summary: str,
    messages,
    api_call_count: int,
    provider: str,
    base_url,
    model: str,
    guidance: Optional[str] = None,
) -> dict:
    """Structured terminal result for a billing-classified failure.

    Single construction point for the returned terminal response so the
    label, guidance, structured block, and ambiguity flag stay consistent
    across the non-retryable abort and max-retries paths (#82154).
    """
    unverified = bool(getattr(classified, "billing_unverified", False))
    if guidance is None:
        guidance = _billing_or_entitlement_message(
            capability="model access",
            provider=provider,
            base_url=str(base_url),
            model=model,
            unverified=unverified,
        )
    final = _billing_terminal_label(summary, unverified)
    if guidance:
        final += f"\n\n{guidance}"
    return {
        "final_response": final,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": summary,
        "failure_reason": classified.reason.value,
        # The billing verdict may rest on an ambiguous body (#82154) — carry
        # that through the structured result, not just the prose.
        "billing_unverified": unverified,
        "billing_block": _billing_block_dict(
            provider, base_url, model, guidance, unverified=unverified
        ),
    }


def _print_billing_or_entitlement_guidance(
    agent,
    *,
    capability: str,
    provider: str,
    base_url: str,
    model: str,
    unverified: bool = False,
) -> bool:
    message = _billing_or_entitlement_message(
        capability=capability,
        provider=provider,
        base_url=base_url,
        model=model,
        unverified=unverified,
    )
    if not message:
        return False
    for line in message.splitlines():
        agent._vprint(f"{agent.log_prefix}   💡 {line}", force=True)
    return True


def _try_refresh_nous_paid_entitlement_credentials(agent) -> bool:
    """Refresh Nous runtime credentials after a fresh paid-entitlement check."""
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        account_info = get_nous_portal_account_info(force_fresh=True)
        if account_info.paid_service_access is not True:
            return False
        return agent._try_refresh_nous_client_credentials(
            force=True,
        )
    except Exception:
        return False


def _restore_or_build_system_prompt(agent, system_message, conversation_history):
    """Restore the cached system prompt from the session DB or build it fresh.

    Mutates ``agent._cached_system_prompt`` and persists a freshly-built
    prompt back to the session DB on first build.  Extracted from
    ``run_conversation`` so the prefix-cache restore path can be tested in
    isolation.

    Three-way state distinction for the stored row, surfaced via logs so
    silent prefix-cache misses are visible in ``agent.log``:

      * ``missing`` — no session row yet (legitimate first turn).
      * ``null``   — row exists, ``system_prompt`` column is NULL.
        Legacy session predating system-prompt persistence, or a migration
        leftover.  Warns when ``conversation_history`` is non-empty.
      * ``empty``  — row exists, ``system_prompt`` column is the empty
        string.  Indicates a previous-turn write that ran but stored
        nothing (silent persistence bug).  Always warns.
      * ``present`` — row exists with a usable prompt → reused verbatim.

    Read or write failures against the session DB log at WARNING (not
    DEBUG) so persistent issues (disk full, schema drift, lock contention)
    surface without needing verbose mode.  This used to be a debug-level
    log that silently broke prefix-cache reuse on the gateway path
    (which constructs a fresh ``AIAgent`` per turn and depends on this
    DB roundtrip).
    """
    stored_prompt = None
    stored_state = "missing"
    if conversation_history and agent._session_db:
        try:
            session_row = agent._session_db.get_session(agent.session_id)
            if session_row is not None:
                raw_prompt = session_row.get("system_prompt")
                if raw_prompt is None:
                    stored_state = "null"
                elif raw_prompt == "":
                    stored_state = "empty"
                else:
                    stored_prompt = raw_prompt
                    stored_state = "present"
        except Exception as exc:
            logger.warning(
                "Session DB get_session failed for system-prompt restore "
                "(session=%s): %s. Falling back to fresh build — prefix "
                "cache will miss for this turn.",
                agent.session_id, exc,
            )

    if stored_prompt and _stored_prompt_matches_runtime(agent, stored_prompt):
        # Bot Chat capability epoch: an eternal bot session must adopt
        # user-initiated capability changes (skills/toolsets/MCP/SOUL/roster)
        # on the next message, not at /new or compression. The stored prompt
        # embeds a fingerprint of the capability surface; a mismatch against
        # disk is a deliberate, once-per-change rebuild — the /model
        # exception applied to capabilities. Prompts without the stamp
        # (every non-Bot-Chat session) never take this branch, and the check
        # fails closed to "reuse" so a probe failure can't burn cache.
        _bot_stale = False
        try:
            from tools.bot_mode_probe import (
                BOT_CHAT_TITLE,
                stored_bot_chat_prompt_needs_upgrade,
                stored_prompt_capability_stale,
            )

            _home_for_epoch = None
            try:
                from agent.system_prompt import _agent_home

                _home_for_epoch = _agent_home(agent)
            except Exception:
                pass
            _bot_stale = stored_prompt_capability_stale(stored_prompt, _home_for_epoch)
            if not _bot_stale and getattr(agent, "_bot_mode_protocol", True):
                # Legacy upgrade: a Bot Chat whose prompt predates the epoch
                # mechanism (no stamp, no protocol) gets ONE migration
                # rebuild — otherwise pre-existing bots would never learn
                # the messaging protocol. Title-gated so ordinary unstamped
                # sessions (i.e. all of them) never take this path; the
                # rebuilt prompt carries the stamp, so it cannot re-fire.
                _t = str(getattr(agent, "_session_title_hint", "") or "").strip()
                if not _t and agent._session_db and agent.session_id:
                    try:
                        _t = str(agent._session_db.get_session_title(agent.session_id) or "").strip()
                    except Exception:
                        _t = ""
                if _t == BOT_CHAT_TITLE:
                    _bot_stale = stored_bot_chat_prompt_needs_upgrade(stored_prompt, _home_for_epoch)
        except Exception:
            _bot_stale = False
        if _bot_stale:
            logger.info(
                "Bot Chat capability epoch changed for session %s; rebuilding "
                "system prompt to adopt the new capability surface (one-time "
                "prefix-cache break).",
                agent.session_id,
            )
            agent._session_title_hint = "Bot Chat"
            # The skills index inside the prompt comes from a two-layer cache
            # (in-process LRU + disk snapshot) that doesn't watch the skills
            # dir; a capability refresh must rebuild THROUGH it or a freshly
            # installed skill stays invisible in the new prompt.
            try:
                from agent.prompt_builder import clear_skills_system_prompt_cache

                clear_skills_system_prompt_cache(clear_snapshot=True)
            except Exception:
                pass
            agent._cached_system_prompt = agent._build_system_prompt(system_message)
            agent._bot_capability_refreshed = True
            # Persist the refreshed prompt so the NEXT turn restores the new
            # bytes verbatim — the cache break is once per capability change,
            # never per turn. (on_session_start deliberately not re-fired:
            # this is a continuation, not a new session.)
            if agent._session_db:
                try:
                    agent._session_db.update_system_prompt(
                        agent.session_id, agent._cached_system_prompt
                    )
                except Exception as exc:
                    logger.warning(
                        "Session DB update_system_prompt failed after Bot Chat "
                        "capability refresh (session=%s): %s. The refresh will "
                        "re-fire next turn.",
                        agent.session_id, exc,
                    )
            return
        # Continuing session — reuse the exact system prompt from the
        # previous turn so the Anthropic cache prefix matches.
        agent._cached_system_prompt = stored_prompt
        # Prompt-section callbacks are new-session-only. Recover their frozen
        # bytes from the persisted full prompt so a later compression rebuild
        # keeps them without evaluating plugin state in this resumed process.
        from agent.system_prompt import restore_plugin_prompt_sections

        restore_plugin_prompt_sections(agent, stored_prompt)
        # Reconstruct the cross-session-stable prefix for the early cache
        # breakpoint. The static prefix is not persisted (only the full
        # prompt is), so gateway surfaces that build a fresh AIAgent per
        # turn would otherwise lose the two-block system layout after the
        # first turn — flip-flopping the wire shape mid-conversation and
        # silently degrading to the legacy single-breakpoint layout.
        #
        # ``reconstruct_static_prefix`` gates on ``_use_prompt_caching`` (so
        # non-Anthropic routes skip the rebuild), applies the startswith
        # safety gate (stored prompt bytes are never rewritten), and
        # fails open to the legacy cache layout.
        from agent.system_prompt import reconstruct_static_prefix

        reconstruct_static_prefix(agent, system_message=system_message)
        return
    if stored_prompt:
        stored_state = "stale_runtime"
        logger.info(
            "Stored system prompt for session %s has stale runtime identity; "
            "rebuilding for model=%s provider=%s.",
            agent.session_id,
            getattr(agent, "model", "") or "",
            getattr(agent, "provider", "") or "",
        )

    if conversation_history and stored_state in ("null", "empty"):
        # Continuing session whose stored prompt is unusable.  The
        # previous turn's write either never happened or wrote an empty
        # string — either way every turn now rebuilds and the prefix
        # cache misses every time.
        logger.warning(
            "Stored system prompt for session %s is %s; rebuilding "
            "from scratch this turn. Prefix cache will miss until "
            "the rebuild persists. Investigate the previous turn's "
            "update_system_prompt write path.",
            agent.session_id, stored_state,
        )

    # First turn of a new session (or recovering from a broken stored
    # prompt) — build from scratch.
    agent._cached_system_prompt = agent._build_system_prompt(system_message)

    # Plugin hook: on_session_start — fired once when a brand-new
    # session is created (not on continuation).  Plugins can use this
    # to initialise session-scoped state (e.g. warm a memory cache).
    try:
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        _invoke_hook(
            "on_session_start",
            session_id=agent.session_id,
            model=agent.model,
            platform=getattr(agent, "platform", None) or "",
        )
    except Exception as exc:
        logger.warning("on_session_start hook failed: %s", exc)

    # Cold-start credits seed (L3) — fallback for the first-turn path. The TUI/
    # desktop build seeds at session OPEN (see seed_credits_at_session_start in
    # tui_gateway), so this call is usually a no-op there (idempotent: skips when
    # _credits_state already exists). For the plain CLI / any path that didn't seed
    # at build, it primes credits state from /api/oauth/account (or a fixture) on the
    # first turn so depletion / usage-band warnings fire. Fail-open inside the helper.
    try:
        from agent.credits_tracker import seed_credits_at_session_start

        seed_credits_at_session_start(agent)
    except Exception:
        logger.debug("cold-start credits seed failed (fail-open)", exc_info=True)

    # Persist the system prompt snapshot in SQLite.  Failure here used
    # to log at DEBUG, which silently broke prefix-cache reuse on the
    # gateway path (fresh AIAgent per turn → reads from this row every
    # subsequent turn).
    if agent._session_db:
        try:
            agent._session_db.update_system_prompt(agent.session_id, agent._cached_system_prompt)
        except Exception as exc:
            logger.warning(
                "Session DB update_system_prompt failed for session %s: "
                "%s. Subsequent turns will rebuild the system prompt and "
                "miss the prefix cache.",
                agent.session_id, exc,
            )


def _stored_prompt_matches_runtime(agent, prompt: str) -> bool:
    """Return False when the persisted runtime-identity lines are stale."""

    def line_value(label: str) -> str:
        """Last matching line wins.

        Safe ONLY for fields emitted in the volatile tier at the very END of
        the prompt (Model / Provider / Platform). User-supplied project
        context (AGENTS.md / CLAUDE.md / .cursorrules) is embedded in the
        middle context tier, so a last-match scan lets project prose shadow
        any field emitted EARLIER — see ``host_info_value``.
        """
        prefix = f"{label}:"
        value = ""
        for line in prompt.splitlines():
            if line.startswith(prefix):
                value = line[len(prefix):].strip()
        return value

    def host_info_value(label: str) -> str:
        """Read a field from the prompt's own host-info block.

        The host-info block (``build_environment_hints``) sits in the STABLE
        tier, ahead of the embedded project context files. A bare scan of the
        whole prompt would therefore match a user's ``AGENTS.md`` that merely
        contains a line starting with the same label, comparing runtime state
        against project prose. That mismatch never clears, so the check would
        reject the stored prompt on EVERY turn — rebuilding the system prompt
        each message and destroying the prefix cache for the whole session,
        which is far worse than the staleness this function guards against.

        Anchor on the ``User home directory:`` line that immediately precedes
        the working-directory line in that block, and take the FIRST such
        occurrence, so only Hermes' own emitted block can satisfy the read.
        """
        prefix = f"{label}:"
        lines = prompt.splitlines()
        for idx, line in enumerate(lines):
            if not line.startswith("User home directory:"):
                continue
            for candidate in lines[idx + 1: idx + 4]:
                if candidate.startswith(prefix):
                    return candidate[len(prefix):].strip()
        return ""

    stored_model = line_value("Model")
    current_model = str(getattr(agent, "model", "") or "").strip()
    if stored_model and current_model and stored_model != current_model:
        return False

    stored_provider = line_value("Provider")
    current_provider = str(getattr(agent, "provider", "") or "").strip()
    if stored_provider and current_provider and stored_provider != current_provider:
        return False

    # Detect cwd drift: if the stored prompt was built in a different working
    # directory, reuse would silently inject a stale path into the prefix cache.
    # Compare against resolve_agent_cwd() — the SAME resolver used to build the
    # prompt — so gateway/TUI sessions that set TERMINAL_CWD are not falsely
    # rejected (they would always differ from the launch dir's os.getcwd()).
    stored_cwd = host_info_value("Current working directory")
    if stored_cwd:
        if stored_cwd != str(resolve_agent_cwd()):
            return False

    # Detect runtime-surface drift: the stored prompt records which platform it
    # was built for (e.g. "desktop" vs "cli"). Reusing a desktop-built prompt on
    # a terminal session (or vice versa) would inject the wrong runtime hints.
    stored_platform = line_value("Platform")
    current_platform = str(getattr(agent, "platform", "") or "").strip()
    if stored_platform and current_platform and stored_platform != current_platform:
        return False

    return True


# The three _get_continuation_prompt variants below, in named-constant form
# so agent.context_compressor's _is_synthetic_compression_user_turn can
# recognize them by content after a crash/interrupt persists one mid-list —
# these rows carry no durable role beyond driving the retry, and SessionDB
# projection strips the _length_continuation_nudge metadata tag that marks
# them in live memory (see agent/context_compressor.py).
_LENGTH_CONTINUATION_NETWORK_STUB = (
    "[System: The previous response was cut off by a "
    "network error mid-stream. Continue exactly where "
    "you left off. Do not restart or repeat prior text. "
    "Finish the answer directly.]"
)
_LENGTH_CONTINUATION_OUTPUT_LIMIT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)
# The dropped-tools variant interpolates the tool name list right after this
# prefix, so it can't be exact-matched — this stable prefix is what
# _is_synthetic_compression_user_turn checks with str.startswith instead.
_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[System: Your previous tool call "


def _get_continuation_prompt(is_partial_stub: bool, dropped_tools: Optional[List[str]] = None) -> str:
    if is_partial_stub and dropped_tools:
        tool_list = ", ".join(dropped_tools[:3])
        return (
            f"{_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX}"
            f"({tool_list}) was too large and "
            "the stream timed out before it "
            "could be delivered. Do NOT retry "
            "the same tool call with the same "
            "large content. Instead, break the "
            "content into multiple smaller tool "
            "calls (e.g. use multiple patch calls "
            "or write smaller files). Each tool "
            "call's arguments must be under ~8K "
            "tokens to avoid stream timeouts.]"
        )
    elif is_partial_stub:
        return _LENGTH_CONTINUATION_NETWORK_STUB
    else:
        return _LENGTH_CONTINUATION_OUTPUT_LIMIT


# Continuation nudge for Codex/Responses turns that came back with only
# internal reasoning (no visible content, no tool calls).  When the interim
# assistant message also carries no encrypted reasoning items and no
# replayable message items, _chat_messages_to_responses_input emits nothing
# for it — a bare retry would be byte-identical to the request that just
# failed, so the model (observed: grok-4.20 on xai-oauth) deterministically
# repeats the reasoning-only response until the retry budget is exhausted.
_CODEX_INCOMPLETE_NUDGE = (
    "[System: Your previous response contained only internal reasoning and "
    "never produced a visible answer or tool call. Do not keep thinking. "
    "Produce your final answer as plain text now (or make the tool call "
    "you were planning).]"
)


# Re-prompt sent after a Codex/Responses turn ends with an acknowledgment-only
# reply (no tool calls, no final answer) — named so
# agent.context_compressor's _is_synthetic_compression_user_turn can
# recognize it by content the same way it recognizes _CODEX_INCOMPLETE_NUDGE.
_CODEX_ACK_CONTINUATION_NUDGE = (
    "[System: Continue now. Execute the required tool calls and only "
    "send your final answer after completing the task.]"
)

# Re-prompt sent when a provider returns finish_reason="tool_calls" with an
# empty tool_calls array (dropped-tool-call recovery, see the retry loop
# below). Named for the same reason as _CODEX_ACK_CONTINUATION_NUDGE — this
# pair is only stripped from the durable transcript once the turn reaches
# finalization; an interrupt/crash mid-retry can still persist it.
_DROPPED_TOOLCALL_NUDGE_CONTENT = (
    "Your previous turn indicated a tool call but none was "
    "included. Do not narrate a plan or restate intent — issue "
    "the actual tool call now to continue the task."
)

# Re-prompt sent when the model returns an empty response after executing tool
# calls (#9400). Named for the same reason as the nudges above — its
# _empty_recovery_synthetic metadata flag doesn't survive SessionDB projection.
_EMPTY_TOOL_RESPONSE_NUDGE = (
    "You just executed tool calls but returned an "
    "empty response. Please process the tool "
    "results above and continue with the task."
)


# Shared recovery hint appended to every content-policy refusal message. Both
# the HTTP-200 refusal path (``finish_reason=content_filter``) and the
# exception path (a provider moderation error classified as
# ``content_policy_blocked``) end with the same actionable next steps, so they
# share one trailer to keep the guidance from drifting between the two sites.
_CONTENT_POLICY_RECOVERY_HINT = (
    "Try rephrasing the request, narrowing the context, or "
    "adding a fallback provider with `hermes fallback add`."
)


# Memo for the send-path tool-call argument canonicalization inside
# run_conversation().  That pass re-canonicalizes the arguments string of
# EVERY historical tool call on EVERY API-call iteration (quadratic in
# session tool-call count), and the api_messages copies share the exact
# argument string objects with the persisted history, so the same strings
# come through unchanged iteration after iteration.
#
# Soundness: canonicalization is a pure, deterministic function of the
# input string (fixed separators, sort_keys=True), so a value-keyed memo
# is exact — equal inputs always produce the canonical form computed the
# first time.  Malformed strings raise out of json.loads BEFORE anything
# is stored, so the repair fallback below is never memoized and reruns on
# every occurrence, exactly as before.  Bounded FIFO eviction mirrors the
# _MSG_TOKENS_CACHE idiom in agent/model_metadata.py.
_CANON_ARGS_CACHE: Dict[str, str] = {}
_CANON_ARGS_CACHE_MAX = 4096
# Count bound alone doesn't bound MEMORY: write_file/patch argument strings
# run 100KB+, so 4096 entries could pin ~800MB in a long-lived gateway
# process. The byte budget keeps the memo effective for the common case
# (args ~0.5-2KB) while bounding the worst case.
_CANON_ARGS_CACHE_MAX_BYTES = 32 * 1024 * 1024
_canon_args_cache_bytes = 0


def _canonicalize_tool_call_arguments(arg_str: str) -> str:
    """Return the canonical wire form of a tool-call arguments JSON string.

    Raises whatever ``json.loads`` raises on malformed input; the caller
    falls back to ``_repair_tool_call_arguments``, exactly as before.
    """
    global _canon_args_cache_bytes
    cached = _CANON_ARGS_CACHE.get(arg_str)
    if cached is not None:
        return cached
    canonical = json.dumps(
        json.loads(arg_str), separators=(",", ":"), sort_keys=True,
    )
    _CANON_ARGS_CACHE[arg_str] = canonical
    _canon_args_cache_bytes += len(arg_str) + len(canonical)
    while len(_CANON_ARGS_CACHE) > _CANON_ARGS_CACHE_MAX or (
        _canon_args_cache_bytes > _CANON_ARGS_CACHE_MAX_BYTES
        and len(_CANON_ARGS_CACHE) > 1
    ):
        try:
            evicted_key = next(iter(_CANON_ARGS_CACHE))
            evicted_val = _CANON_ARGS_CACHE.pop(evicted_key)
            _canon_args_cache_bytes -= len(evicted_key) + len(evicted_val)
        except (StopIteration, KeyError, RuntimeError):
            break
    return canonical


def _clone_message_for_send(msg):
    """Structural clone of a history message for the per-call API copy.

    The send path builds ``api_messages`` from the persisted history and
    then rewrites the copies in place (canonicalization/repair of tool-call
    arguments, surrogate and non-ASCII sanitization, content strips, cache
    decoration). A shallow ``msg.copy()`` only decouples TOP-LEVEL fields:
    nested containers — ``tool_calls`` entries and their ``function`` dicts,
    multimodal ``content`` part lists, ``reasoning_details`` — remain the
    SAME objects the persisted history holds, so any in-place write there
    silently rewrites the stored transcript (#80498: an unrepairable
    ``write_file`` argument string was replaced with ``{}`` in the persisted
    turn, destroying the streamed file content).

    Cloning every container (dict/list) recursively while SHARING immutable
    leaves (strings, numbers, None) makes every downstream in-place
    transform safe by construction — current and future — at container-count
    cost, not string-byte cost: big argument strings and base64 image
    payloads are shared, never copied. Measured: ~1-5ms per 2000-message
    pathological build (20% multimodal, 30% tool calls) vs ~0.4ms for the
    shallow copy; compression keeps real request histories far smaller, and
    the build runs once per API call — noise next to the call itself.
    copy.deepcopy would be equally correct (CPython deepcopy also shares
    immutable str) but ~4x slower again and needs its memo machinery;
    history messages are JSON-shaped and acyclic (depth < 10 in practice;
    a >~1000-deep pathological nest would hit the recursion limit, exactly
    as deepcopy would), so cycle handling isn't needed here. Tuples are
    shared as leaves: JSON-derived message content never contains tuples,
    so a mutable container smuggled inside one is not a reachable shape on
    this path.
    """
    if isinstance(msg, dict):
        return {
            k: _clone_message_for_send(v) if isinstance(v, (dict, list)) else v
            for k, v in msg.items()
        }
    if isinstance(msg, list):
        return [
            _clone_message_for_send(v) if isinstance(v, (dict, list)) else v
            for v in msg
        ]
    return msg


def _canonicalize_api_tool_calls(api_messages) -> None:
    """Canonicalize tool-call argument JSON on the send-path message copy.

    Rewrites each message's ``tool_calls`` in place (copy-on-write for the
    tool-call dicts it canonicalizes; the persisted history is untouched).
    The pass still traverses every message and tool call each iteration;
    the memo above bounds the JSON parse/serialize work to one round-trip
    per UNIQUE argument string instead of one per string per iteration —
    the quadratic part of the cost. The remaining traversal is pointer
    chasing and dict copies, cheap next to a json.loads + json.dumps.
    """
    for am in api_messages:
        tcs = am.get("tool_calls")
        if not tcs:
            continue
        new_tcs = []
        for tc in tcs:
            if isinstance(tc, dict) and "function" in tc:
                try:
                    tc = {**tc, "function": {
                        **tc["function"],
                        "arguments": _canonicalize_tool_call_arguments(
                            tc["function"]["arguments"]
                        ),
                    }}
                except Exception:
                    # Copy-on-write here too. The send-path build now hands
                    # this pass structurally-cloned messages (see
                    # _clone_message_for_send), but this branch keeps its own
                    # copy as defense in depth: some callers (tests, future
                    # call sites) pass shallow copies, and assigning into a
                    # shared ``tc["function"]`` would rewrite the stored
                    # turn. On the unrepairable path the repair returns "{}",
                    # so a write-through here replaced the model's real
                    # arguments with an empty object in the transcript: a
                    # stream that died mid ``write_file`` lost the file
                    # content it had already streamed, with only a WARNING
                    # to show for it (#80498).
                    tc = {**tc, "function": {
                        **tc["function"],
                        "arguments": _repair_tool_call_arguments(
                            tc["function"]["arguments"],
                            tc["function"].get("name", "?"),
                        ),
                    }}
            new_tcs.append(tc)
        am["tool_calls"] = new_tcs


def _invalid_tool_name_error_content(name: str, valid_tool_names) -> str:
    """Error-result content for a tool call whose name isn't a real tool.

    A blank/whitespace-only name is not a typo the model can fuzzy-correct
    toward a real tool — it is almost always a weak open model echoing
    tool-call XML/JSON it saw in file or tool output (#47967:
    <tool_call>/<invoke name=...> payloads in a file prime
    mimo/nemotron-class models to emit empty structured calls), or a model
    degrading at very large context (observed with gpt-5.6 past ~350K input).
    Dumping the full tool catalog in that case feeds the priming loop more
    names to mimic and inflates context 3-4x across retries, so send a terse
    error that tells the model in-context tool-call syntax is DATA, not a
    call to make. A genuinely-wrong-but-nonempty name (an actual typo) still
    gets the catalog so the model can self-correct.
    """
    if not (name or "").strip():
        return (
            "Tool call rejected: the tool name was empty. "
            "If tool-call XML or JSON appeared in file "
            "contents or tool output, that is data — do "
            "not re-emit it as a tool call. To call a "
            "tool, use a valid name from your tool list; "
            "otherwise reply in plain text."
        )
    available = ", ".join(sorted(valid_tool_names))
    return f"Tool '{name}' does not exist. Available tools: {available}"


def _content_policy_blocked_result(
    messages: List[Dict],
    api_call_count: int,
    *,
    final_response: str,
    error_detail: str,
) -> Dict[str, Any]:
    """Build the terminal turn result for a content-policy block.

    A content-policy refusal is deterministic for the unchanged prompt, so the
    turn ends here (no retry). Both the HTTP-200 refusal handler and the
    exception-path handler return the identical shape — a failed, non-completed
    turn carrying the user-facing message and a ``content_policy_blocked:``
    prefixed error — so they funnel through this one builder.
    """
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": f"content_policy_blocked: {error_detail}",
    }


def _compression_deferred_result(
    agent,
    messages: List[Dict],
    api_call_count: int,
) -> Dict[str, Any]:
    """Build the soft turn result for a lock-contended compression defer.

    Another path (a sibling turn, a background review fork, a manual
    ``/compress``) holds this session's compression lock, so every
    compression pass this turn no-oped and the request still does not fit.
    This is a TEMPORARY condition — the lock winner is actively shrinking
    the same session — so the turn must end as a soft defer
    (``compression_deferred``), never as ``compression_exhausted``: the
    gateway auto-resets (wipes) the session on exhaustion (#9893/#35809),
    which would destroy a session that the concurrent compressor is about
    to make healthy again.

    ``failed`` stays False so the gateway persists the user turn (transient
    branch) and retry-next-message semantics apply.
    """
    holder = getattr(agent, "_compression_skipped_due_to_lock", None)
    logger.info(
        "turn deferred: compression lock held by another path "
        "(session=%s holder=%s) — not counting as compression exhaustion",
        agent.session_id or "none",
        holder if isinstance(holder, str) else "unconfirmed",
    )
    try:
        agent._flush_status_buffer()
    except Exception:
        pass
    _final = (
        "Context compression is already running for this session. "
        "Please retry in a moment — your next message will be processed "
        "once the concurrent compression finishes."
    )
    return {
        "final_response": _final,
        "messages": messages,
        "completed": False,
        "api_calls": api_call_count,
        "error": _final,
        "partial": True,
        "failed": False,
        "compression_deferred": True,
        "session_id": agent.session_id,
    }


def _rewrite_system_content_blocks(system_message: dict, effective: str) -> bool:
    """Rewrite a cache-decorated system message in place, keeping its blocks.

    ``apply_anthropic_cache_control`` runs once per call block, *before* the
    retry loop, and splits the system prompt into ``[static prefix, volatile
    tail]`` text blocks carrying the cache_control breakpoints. Assigning a bare
    string over that list drops both breakpoints, so the failover retry ships
    the whole system prompt uncached and re-bills it in full.

    ``rewrite_prompt_model_identity`` only touches the LAST ``Model:`` /
    ``Provider:`` lines, and those live in the volatile tail — so the static
    prefix stays byte-identical and its cache entry keeps matching. Returns
    False when the shape is not one we can safely patch, so the caller falls
    back to the plain-string assignment.
    """
    content = system_message.get("content")
    if not isinstance(content, list) or not content:
        return False
    if not all(
        isinstance(part, dict) and part.get("type") == "text" for part in content
    ):
        return False
    if len(content) == 1:
        content[0]["text"] = effective
        return True
    if len(content) == 2:
        head = content[0].get("text") or ""
        if head and effective.startswith(head):
            tail = effective[len(head):]
            if tail:
                content[1]["text"] = tail
                return True
    return False


def _sync_failover_system_message(agent, api_messages, active_system_prompt):
    """Refresh the in-flight system message after a provider failover.

    ``try_activate_fallback`` rewrites the ``Model:``/``Provider:`` identity
    lines on ``agent._cached_system_prompt`` (see
    ``rewrite_prompt_model_identity``) so the agent reports the model that is
    actually answering.  But the current call block's ``api_messages`` were
    built from the pre-failover prompt, and the retry loop rebuilds
    ``api_kwargs`` from that list each iteration — without this sync the
    whole turn (and every gateway turn, since fallback re-activates per
    message while the primary is down) ships the stale identity.

    Mutates ``api_messages[0]`` in place and returns the prompt to use as
    ``active_system_prompt`` for subsequent call-block rebuilds.
    """
    sp = getattr(agent, "_cached_system_prompt", None)
    if not isinstance(sp, str) or not sp:
        return active_system_prompt
    if api_messages and api_messages[0].get("role") == "system":
        effective = sp
        if agent.ephemeral_system_prompt:
            effective = (effective + "\n\n" + agent.ephemeral_system_prompt).strip()
        if not _rewrite_system_content_blocks(api_messages[0], effective):
            api_messages[0]["content"] = effective
    return sp


def _ensure_cached_system_prompt_static(agent, system_message=None) -> None:
    """Rebuild ``_cached_system_prompt_static`` when caching becomes active.

    Sessions restored under a cache-off primary skip the static-prefix rebuild
    (gated on ``_use_prompt_caching`` at restore time). A later failover to a
    cache-on provider would otherwise redecorate with ``static_system_prefix=
    None`` and silently fall back to the legacy system-plus-3 layout (#72626).

    Thin wrapper over :func:`agent.system_prompt.reconstruct_static_prefix`,
    which memoizes failed rebuilds so this stays cheap on the retry-loop hot
    path (it runs at the top of every attempt).
    """
    from agent.system_prompt import reconstruct_static_prefix

    reconstruct_static_prefix(
        agent, system_message=system_message, log_label="failover redecoration"
    )


def _peel_moa_guidance(
    messages: List[Dict[str, Any]],
    guidance: Any,
) -> List[Dict[str, Any]]:
    """Remove MoA reference guidance previously attached by ``_attach_reference_guidance``.

    Thin wrapper over :func:`agent.moa_loop.peel_reference_guidance` (kept
    adjacent to the attach so the forward/inverse shapes evolve together).
    Lazy import mirrors the module's other moa_loop touchpoints.
    """
    from agent.moa_loop import peel_reference_guidance

    return peel_reference_guidance(messages, guidance)


def _redecorate_prompt_cache_for_provider(
    agent,
    api_messages: List[Dict[str, Any]],
    *,
    system_message=None,
    moa_prepared: Optional[Dict[str, Any]] = None,
    tools_for_api: Optional[List[Dict[str, Any]]] = None,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]] | tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Strip and re-apply cache_control for the *current* provider policy.

    Decoration runs once per call block before the retry loop for the primary
    provider. ``try_activate_fallback`` refreshes ``_use_prompt_caching`` /
    ``_use_native_cache_layout`` but the nine failover ``continue`` paths reused
    the old ``api_messages`` (#72626). Mirror ``_reapply_reasoning_echo_for_provider``
    by reshaping at the top of each retry attempt.

    The source list is the mutated in-flight request (image shrink / ASCII /
    reasoning_details recoveries already applied), never a pristine
    pre-decoration snapshot. MoA guidance is peeled and rebased without
    decoration; the acting aggregator plans its resolved destination later.
    """
    messages: List[Dict[str, Any]] = [
        dict(m) if isinstance(m, dict) else m for m in (api_messages or [])
    ]
    prepared = moa_prepared
    guidance = prepared.get("guidance") if isinstance(prepared, dict) else None
    if guidance:
        messages = _peel_moa_guidance(messages, guidance)

    strip_anthropic_cache_control(messages)
    planned_tools = strip_anthropic_tool_cache_control(
        tools_for_api if tools_for_api is not None else getattr(agent, "tools", [])
    )

    if prepared is not None and getattr(agent, "provider", None) == "moa":
        # Prepared MoA state is canonical: the synchronous acting-aggregator
        # sender owns its destination-local cache plan after it resolves the slot.
        completions = getattr(getattr(agent.client, "chat", None), "completions", None)
        rebase = getattr(completions, "rebase_prepared_request", None)
        if callable(rebase):
            prepared = rebase(prepared, messages)
            messages = prepared["messages"]
        if tools_for_api is None:
            return messages, prepared
        return messages, prepared, planned_tools

    # Direct attribute access matches the call-block decoration site — the
    # flags are unconditionally initialized on AIAgent, and a getattr
    # default here would mask a real init bug as silent cache-off.
    if agent._use_prompt_caching:
        _ensure_cached_system_prompt_static(agent, system_message=system_message)
        static = getattr(agent, "_cached_system_prompt_static", None)
        direct_tool_cache = getattr(
            agent,
            "_direct_native_anthropic_tool_cache_capability",
            lambda: False,
        )()
        plan = build_prompt_cache_plan(
            messages,
            planned_tools,
            # Clamp per-destination: a configured 1h regresses to 5m on
            # Qwen/Alibaba routes, whose context cache is 5m-only (#84733).
            cache_ttl=effective_cache_ttl(
                agent._cache_ttl,
                provider=agent.provider,
                model=agent.model,
            ),
            native_anthropic=agent._use_native_cache_layout,
            static_system_prefix=static if isinstance(static, str) else None,
            direct_native_tool_cache=direct_tool_cache,
        )
        messages = plan.messages
        planned_tools = plan.tools

    if tools_for_api is None:
        return messages, prepared
    return messages, prepared, planned_tools


def _apply_context_engine_selection(
    agent: Any,
    api_messages: List[Dict[str, Any]],
    conversation_messages: List[Dict[str, Any]],
    incoming_message: Optional[Dict[str, Any]],
    *,
    logger: Any,
) -> List[Dict[str, Any]]:
    """Run the optional per-turn ``ContextEngine.select_context()`` hook.

    Returns the (possibly replaced) request message list. The hook is for
    context *selection / routing* (retrieval, topic routing, role switching),
    which is distinct from compression and fires every turn independent of
    ``should_compress()``.

    Fail-open by design: a missing hook, any exception, or an invalid return
    value yields the unmodified ``api_messages``. The result is request-only —
    persisted conversation history is never mutated here.
    """
    engine = getattr(agent, "context_compressor", None)
    if engine is None or not hasattr(engine, "select_context"):
        return api_messages

    # Skip the no-op base implementation so non-implementing engines —
    # including the built-in ContextCompressor — pay nothing per request:
    # no history copies below, no call. ``hasattr`` alone is not enough,
    # because the ABC defines a default ``select_context`` that every engine
    # inherits. Mirrors the base-method short-circuit in
    # ``_notify_context_engine_turn_complete``. Lazy import avoids any import
    # cycle with agent.context_engine.
    try:
        from agent.context_engine import ContextEngine as _CE
        if getattr(engine.select_context, "__func__", None) is _CE.select_context:
            return api_messages
    except Exception:
        pass

    session_label = getattr(agent, "session_id", None) or "-"
    # Pass shallow copies of the reference-only inputs so an engine that
    # mutates them in place cannot alter persisted transcript state. Only
    # ``request_messages`` (the per-call request list) is meant to be acted on,
    # and it may be replaced wholesale via the return value — never mutated in
    # place either. ``conversation_messages`` / ``incoming_message`` are
    # read-only context; copying enforces the request-only contract rather than
    # merely documenting it. Structural clones, not dict(m): a shallow copy
    # would leave nested containers (tool_calls, content parts) aliased to
    # the persisted history, so an engine writing into them would rewrite
    # the transcript (#80498 aliasing class).
    _conv_copy = [_clone_message_for_send(m) for m in conversation_messages] \
        if conversation_messages is not None else None
    _incoming_copy = _clone_message_for_send(incoming_message) if isinstance(incoming_message, dict) else incoming_message
    try:
        selected = engine.select_context(
            api_messages,
            conversation_messages=_conv_copy,
            incoming_message=_incoming_copy,
            budget_tokens=getattr(engine, "context_length", 0) or 0,
        )
    except Exception:
        logger.warning(
            "Context engine select_context hook failed; using unmodified "
            "request messages (session=%s)",
            session_label,
            exc_info=True,
        )
        return api_messages

    if selected is None:
        return api_messages
    # Require a NON-EMPTY list of dicts. An empty list must fall open to the
    # original request: ``all([])`` is ``True``, so without the emptiness check
    # a ``[]`` returned by a buggy/failing engine would replace a valid request
    # with an empty message list that the downstream sanitizers cannot restore,
    # reaching the provider as an invalid request instead of failing open.
    if isinstance(selected, list) and selected and all(isinstance(m, dict) for m in selected):
        return selected

    logger.warning(
        "Context engine select_context returned an invalid value "
        "(not a non-empty list of dicts); ignoring (session=%s)",
        session_label,
    )
    return api_messages


def _notify_context_engine_turn_complete(
    agent: Any,
    messages: List[Dict[str, Any]],
    *,
    usage: Optional[Dict[str, Any]] = None,
    logger: Any,
    **meta: Any,
) -> None:
    """Notify the active context engine that a user turn has finished.

    Calls the optional ``ContextEngine.on_turn_complete()`` observation hook
    once per turn, after the assistant/tool loop has produced the finalized
    transcript. The complement to ``select_context()`` (pre-request selection):
    this lets an engine ingest / index / summarize the completed turn.

    Fail-open: a missing or no-op hook, or any exception, is swallowed.
    ``messages`` is passed as a shallow copy so the engine cannot mutate the
    persisted transcript.
    """
    engine = getattr(agent, "context_compressor", None)
    hook = getattr(engine, "on_turn_complete", None)
    if engine is None or not callable(hook):
        return

    # Skip the no-op base implementation so non-implementing engines (incl.
    # the built-in compressor) pay nothing per turn. Lazy import avoids any
    # import cycle with agent.context_engine.
    try:
        from agent.context_engine import ContextEngine as _CE
        if getattr(hook, "__func__", None) is _CE.on_turn_complete:
            return
    except Exception:
        pass

    try:
        hook(
            # Structural clones: on_turn_complete receives the PERSISTED
            # history; a shallow dict(m) would let a hook write through
            # nested containers into the transcript (#80498 aliasing class).
            [_clone_message_for_send(m) for m in messages],
            usage=usage,
            **meta,
        )
    except Exception:
        logger.warning(
            "Context engine on_turn_complete hook failed (session=%s)",
            getattr(agent, "session_id", None) or "-",
            exc_info=True,
        )


def run_conversation(
    agent,
    user_message: Any,
    system_message: str = None,
    conversation_history: List[Dict[str, Any]] = None,
    task_id: str = None,
    stream_callback: Optional[callable] = None,
    persist_user_message: Optional[Any] = None,
    persist_user_timestamp: Optional[float] = None,
    persist_user_display_kind: Optional[str] = None,
    persist_user_display_metadata: Optional[Dict[str, Any]] = None,
    moa_config: Optional[dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run a complete conversation with tool calling until completion.

    Args:
        user_message (str): The user's message/question
        system_message (str): Custom system message (optional, overrides ephemeral_system_prompt if provided)
        conversation_history (List[Dict]): Previous conversation messages (optional)
        task_id (str): Unique identifier for this task to isolate VMs between concurrent tasks (optional, auto-generated if not provided)
        stream_callback: Optional callback invoked with each text delta during streaming.
            Used by the TTS pipeline to start audio generation before the full response.
            When None (default), API calls use the standard non-streaming path.
        persist_user_message: Optional clean user message to store in
            transcripts/history when user_message contains API-only
            synthetic prefixes.
        persist_user_timestamp: Optional platform event timestamp to store
            as metadata on that persisted user message.
        persist_user_display_kind: Optional presentation type for a
            synthesized user turn (``auto_continue``, ``model_switch``, …).
            Display-only: transcript surfaces render the row as a timeline
            event instead of a user bubble, while the model still receives
            the message unchanged.
        persist_user_display_metadata: Optional payload for that event
            (e.g. a delegation's task count).
                or queuing follow-up prefetch work.

    Returns:
        Dict: Complete conversation result with final response and message history
    """
    if moa_config is None:
        try:
            from hermes_cli.moa_config import decode_moa_turn

            _decoded_message, _decoded_moa_config = decode_moa_turn(user_message)
            if _decoded_moa_config is not None:
                user_message = _decoded_message
                moa_config = _decoded_moa_config
                if persist_user_message is None:
                    persist_user_message = _decoded_message
        except Exception:
            pass

    # The gateway caches agents across user turns.  Compression state is
    # per-turn: carrying a prior in-place boundary forward would make a later
    # uncompressed result look like a compacted transcript to gateway writers.
    agent._last_compaction_in_place = False
    agent._last_compression_attempt_recorded = False
    agent._last_compression_attempt_in_place = None

    # If a background memory/skill review spawned at the end of a PRIOR turn
    # (agent/background_review.py) is still running its own run_conversation()
    # when THIS turn starts, cancel it now rather than letting both make
    # outbound API calls concurrently against the same session_id/credentials.
    # That concurrency can produce doubled prompt-token accounting on this
    # turn's own calls and, because the review fork is a fully separate
    # AIAgent with no route back to THIS agent's interrupt() by default, a
    # lockup that survives a normal /stop and needs a hard Ctrl+C.
    # ``review_agent.interrupt()`` is fire-and-forget here — it just flags
    # cancellation and aborts the review's in-flight socket; it does not
    # block waiting for the review's daemon thread to exit, so it can't add
    # latency to this turn. Only ever set on the real owning agent (the
    # review fork's own copy of this attribute stays None — reviews don't
    # spawn nested reviews), so this is a no-op on every other run_conversation
    # caller (subagents, the review fork itself, etc).
    _pending_review = getattr(agent, "_background_review_agent", None)
    if _pending_review is not None:
        try:
            _pending_review.interrupt("superseded by a new live turn")
        except Exception:
            logger.debug(
                "Failed to cancel in-flight background review for a new turn",
                exc_info=True,
            )

    # Adopt any ~/.hermes/.env credential/base-url edits made since the last
    # turn — a Settings save updates .env but not this worker's client, which
    # was built at agent init (#67821). No-op when .env is unchanged.
    try:
        agent._try_refresh_env_client_credentials()
    except Exception:
        logger.debug("per-turn env credential refresh failed", exc_info=True)

    # ── Per-turn setup (the prologue) ──
    # All once-per-turn setup — stdio guarding, retry-counter resets, user
    # message sanitization, todo/nudge hydration, system-prompt restore-or-
    # build, preflight compression, the ``pre_llm_call`` plugin hook,
    # external-memory prefetch, and crash-resilience persistence — lives in
    # ``build_turn_context``.  It mutates ``agent`` exactly as the inline code
    # did and returns the locals the loop below reads back.  See
    # ``agent/turn_context.py``.
    _ctx = build_turn_context(
        agent,
        user_message,
        system_message,
        conversation_history,
        task_id,
        stream_callback,
        persist_user_message,
        persist_user_timestamp,
        persist_user_display_kind=persist_user_display_kind,
        persist_user_display_metadata=persist_user_display_metadata,
        restore_or_build_system_prompt=_restore_or_build_system_prompt,
        install_safe_stdio=_install_safe_stdio,
        sanitize_surrogates=_sanitize_surrogates,
        summarize_user_message_for_log=_summarize_user_message_for_log,
        set_session_context=set_session_context,
        set_current_write_origin=set_current_write_origin,
        ra=_ra,
        # MoA turns append per-call aggregated context to the API copy of the
        # user message, so no byte-stable api_content sidecar can be stamped.
        moa_active=bool(moa_config),
    )
    _should_review_memory = _ctx.should_review_memory
    _plugin_user_context = _ctx.plugin_user_context
    _ext_prefetch_cache = _ctx.ext_prefetch_cache

    # Commentary deduplication spans all provider continuations and tool calls
    # within one user turn, but must not suppress the same phrase next turn.
    agent._delivered_interim_texts = set()
    # A configured SessionDB append failure halts only the affected turn. A
    # cached gateway agent must recover on the next message if storage did.
    agent._incremental_persistence_failed = False
    # Cause of the most recent persistence failure this turn ('locked',
    # 'disk', or 'unknown' — see hermes_state.classify_persistence_error).
    # Reset alongside the failure flag so a lock-contention diagnosis from a
    # previous turn can never leak into this turn's user-facing explanation.
    agent._last_persistence_error_cause = None
    # Per-turn diagnostic: a failed compression-tip adoption in a previous
    # turn's flush must not be reported against this turn.
    agent._compression_adoption_failed = False

    _recovery = TurnRecovery()
    _turn_controller = TurnController()
    _continuation = TurnContinuation()
    # Main conversation loop counters (pure locals consumed by the loop below).
    api_call_count = 0
    final_response = None
    interrupted = False
    failed = False
    # Bounded transport-interruption recovery for this turn.  Turn-scoped (not
    # on TurnRetryState) because a recovery attempt breaks out to the outer
    # loop, which builds a fresh TurnRetryState — the state has to outlive
    # that.  Reset to NONE once a recovery succeeds so a genuinely separate
    # drop later in a long tool loop still gets its own bounded budget.
    _recovery.transport = TransportRecoveryState.NONE
    semantic_progress = SemanticProgressTracker()
    _recovery.compression_attempts = 0
    # One resolved per-turn compression attempt cap, shared by every site that
    # consumes ``compression_attempts``: the pre-API pressure gate, the
    # overflow/413 retry handlers, and the post-tool compaction gate. The
    # counter is a consecutive unverified/ineffective-attempt backstop: a
    # completed compaction rearms it only after a successful provider response
    # reports a prompt below the threshold.
    # Config-driven via compression.max_attempts (parsed + validated in
    # agent_init); default 3 preserves the prior hardcoded behavior for
    # objects without the attribute (older pickles / minimal stubs).
    _recovery.max_compression_attempts = getattr(agent, "max_compression_attempts", 3)
    _turn_exit_reason = "unknown"  # Diagnostic: why the loop ended
    # Last composed answer intentionally held back by a verification gate. If
    # that continuation consumes the remaining budget, this is the best
    # user-facing result available; it must not be confused with error or
    # recovery text produced by unrelated exit paths.
    # Tracks whether the pending verification candidate was already streamed
    # to the user as interim content. The finalizer uses this to set
    # ``_response_was_previewed`` ONLY when the pending candidate is actually
    # reused as the final response — not merely because any interim was
    # streamed. (#65919 review: response-loss blocker)
    # If pre-API compression fires after MoA advisors have produced guidance,
    # retain that ephemeral output and rebase it onto the compacted transcript
    # on the next loop iteration. This prevents a second advisor fan-out.
    pending_moa_prepared_request = None

    # Per-turn tally of consecutive successful credential-pool token refreshes,
    # keyed by (provider, pool-entry-id). A persistent upstream 401 lets
    # ``try_refresh_current()`` "succeed" forever on a single-entry OAuth pool,
    # so this tally caps same-entry refreshes and lets the fallback chain take
    # over instead of spinning. Reset here so each turn starts fresh. See #26080.
    agent._auth_pool_refresh_counts = {}

    # Reset the per-turn usage holder forwarded to the context engine's
    # on_turn_complete() observation hook. Set after each successful provider
    # response (see below); left as None on turns that never reach a response
    # (early failure / interrupt) so the hook receives None rather than a
    # stale prior turn's usage.
    agent._last_turn_usage = None

    # Optional opt-in runtime: if api_mode == codex_app_server, hand the
    # turn to the codex app-server subprocess (terminal/file ops/patching
    # all run inside Codex). Default Hermes path is bypassed entirely.
    # See agent/transports/codex_app_server_session.py for the adapter
    # and references/codex-app-server-runtime.md for the rationale.
    if agent.api_mode == "codex_app_server":
        return agent._run_codex_app_server_turn(
            user_message=_ctx.user_message,
            original_user_message=_ctx.original_user_message,
            messages=_ctx.messages,
            effective_task_id=_ctx.effective_task_id,
            should_review_memory=_should_review_memory,
        )

    while (api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) or agent._budget_grace_call:
        _redirect_text = agent._drain_pending_redirect()
        if _redirect_text:
            semantic_progress.reset()
            _apply_active_turn_redirect(agent, _ctx.messages, _redirect_text)
            if isinstance(_ctx.original_user_message, str):
                _ctx.original_user_message = (
                    f"{_ctx.original_user_message}\n\n"
                    f"User correction during the turn: {_redirect_text}"
                )
            agent._persist_session(_ctx.messages, _ctx.conversation_history)

        # Reset per-turn checkpoint dedup so each iteration can take one snapshot
        agent._checkpoint_mgr.new_turn()

        # Check for interrupt request (e.g., user sent new message)
        if agent._interrupt_requested:
            interrupted = True
            _turn_exit_reason = "interrupted_by_user"
            if not agent.quiet_mode:
                agent._safe_print("\n⚡ Breaking out of tool loop due to interrupt...")
            _turn_controller.move(TransitionKind.FINALIZE, TurnReason.INTERRUPT)
            break
        
        api_call_count += 1
        agent._api_call_count = api_call_count
        agent._touch_activity(f"starting API call #{api_call_count}")

        # Grace call: the budget is exhausted but we gave the model one
        # more chance.  Consume the grace flag so the loop exits after
        # this iteration regardless of outcome.
        if agent._budget_grace_call:
            agent._budget_grace_call = False
        elif not agent.iteration_budget.consume():
            _turn_exit_reason = "budget_exhausted"
            if not agent.quiet_mode:
                agent._safe_print(f"\n⚠️  Iteration budget exhausted ({agent.iteration_budget.used}/{agent.iteration_budget.max_total} iterations used)")
            _turn_controller.move(TransitionKind.FINALIZE, TurnReason.BUDGET)
            break

        # Fire step_callback for gateway hooks (agent:step event)
        if agent.step_callback is not None:
            try:
                prev_tools = []
                for _idx, _m in enumerate(reversed(_ctx.messages)):
                    if _m.get("role") == "assistant" and _m.get("tool_calls"):
                        _fwd_start = len(_ctx.messages) - _idx
                        _results_by_id = {}
                        for _tm in _ctx.messages[_fwd_start:]:
                            if _tm.get("role") != "tool":
                                break
                            _tcid = _tm.get("tool_call_id")
                            if _tcid:
                                _results_by_id[_tcid] = _tm.get("content", "")
                        prev_tools = [
                            {
                                "name": tc["function"]["name"],
                                "result": _results_by_id.get(tc.get("id")),
                                "arguments": tc["function"].get("arguments"),
                            }
                            for tc in _m["tool_calls"]
                            if isinstance(tc, dict)
                        ]
                        break
                agent.step_callback(api_call_count, prev_tools)
            except Exception as _step_err:
                logger.debug("step_callback error (iteration %s): %s", api_call_count, _step_err)

        # Track tool-calling iterations for skill nudge.
        # Counter resets whenever skill_manage is actually used.
        if (agent._skill_nudge_interval > 0
                and "skill_manage" in agent.valid_tool_names):
            agent._iters_since_skill += 1
        
        # ── Pre-API-call /steer drain ──────────────────────────────────
        # If a /steer arrived during the previous API call (while the model
        # was thinking), drain it now — before we build api_messages — so
        # the model sees the steer text on THIS iteration.  Without this,
        # steers sent during an API call only land after the NEXT tool batch,
        # which may never come if the model returns a final response.
        #
        # We scan backwards for the last tool-role message in the messages
        # list.  If found, the steer is appended there.  If not (first
        # iteration, no tools yet), the steer stays pending for the next
        # tool batch — injecting into a user message would break role
        # alternation, and there's no tool output to piggyback on.
        _pre_api_steer = agent._drain_pending_steer()
        if _pre_api_steer:
            _injected = False
            for _si in range(len(_ctx.messages) - 1, -1, -1):
                _sm = _ctx.messages[_si]
                if isinstance(_sm, dict) and _sm.get("role") == "tool":
                    from agent.prompt_builder import format_steer_marker
                    marker = format_steer_marker(_pre_api_steer)
                    existing = _sm.get("content", "")
                    if isinstance(existing, str):
                        _sm["content"] = existing + marker
                    else:
                        # Multimodal content blocks — append text block
                        try:
                            blocks = list(existing) if existing else []
                            blocks.append({"type": "text", "text": marker})
                            _sm["content"] = blocks
                        except Exception:
                            pass
                    _injected = True
                    logger.debug(
                        "Pre-API-call steer drain: injected into tool msg at index %d",
                        _si,
                    )
                    break
            if not _injected:
                # No tool message to inject into — put it back so
                # the post-tool-execution drain picks it up later.
                _lock = getattr(agent, "_pending_steer_lock", None)
                if _lock is not None:
                    with _lock:
                        if agent._pending_steer:
                            agent._pending_steer = agent._pending_steer + "\n" + _pre_api_steer
                        else:
                            agent._pending_steer = _pre_api_steer
                else:
                    existing = getattr(agent, "_pending_steer", None)
                    agent._pending_steer = (existing + "\n" + _pre_api_steer) if existing else _pre_api_steer

        # Prepare messages for API call
        # If we have an ephemeral system prompt, prepend it to the messages
        # Note: Reasoning is embedded in content via <think> tags for trajectory storage.
        # However, providers like Moonshot AI require a separate 'reasoning_content' field
        # on assistant messages with tool_calls. We handle both cases here.
        request_logger = getattr(agent, "logger", None) or logging.getLogger(__name__)
        # Per-agent validation cursor: skips re-json.loads-ing tool_call
        # arguments on history messages already validated in a previous
        # iteration. Identity-keyed (strong refs) — compression/undo/repair
        # rewriting the list breaks the prefix match and forces a re-scan
        # from the divergence point. See sanitize_tool_call_arguments.
        _sanitize_cursor = getattr(agent, "_sanitize_args_cursor", None)
        if _sanitize_cursor is None:
            _sanitize_cursor = {}
            try:
                agent._sanitize_args_cursor = _sanitize_cursor
            except Exception:
                pass
        repaired_tool_calls = agent._sanitize_tool_call_arguments(
            _ctx.messages,
            logger=request_logger,
            session_id=agent.session_id,
            cursor=_sanitize_cursor,
        )
        if repaired_tool_calls > 0:
            request_logger.info(
                "Sanitized %s corrupted tool_call arguments before request (session=%s)",
                repaired_tool_calls,
                agent.session_id or "-",
            )

        # Drop legacy ghost rows from the incomplete #73146 else branch BEFORE
        # the alternation repair below: a hidden assistant placeholder whose
        # content/api_content is the raw interrupt scaffold. Replaying that as
        # an assistant message makes the model echo it and self-replicate
        # (#81841). Dropping before repair lets repair_message_sequence fix
        # any user→user adjacency the filter creates.
        _ctx.messages = [
            msg for msg in _ctx.messages
            if not (
                msg.get("display_kind") == "hidden"
                and msg.get("role") == "assistant"
                and (
                    (
                        isinstance(msg.get("content"), str)
                        and msg["content"].strip() == _INTERRUPT_SCAFFOLD_MARKER
                    )
                    or (
                        isinstance(msg.get("api_content"), str)
                        and msg["api_content"].strip() == _INTERRUPT_SCAFFOLD_MARKER
                    )
                )
            )
        ]

        # Defensive: repair malformed role-alternation before API call.
        # Catches cases where the history got wedged into a
        # ``tool → user`` or ``user → user`` tail (e.g. after empty-
        # response scaffolding was stripped and a new user message
        # landed after an orphan tool result). Most providers return
        # empty content on malformed sequences, which would otherwise
        # retrigger the empty-retry loop indefinitely.
        # repair_message_sequence_with_cursor also recomputes the SessionDB
        # flush cursor (_last_flushed_db_idx) when repair compacts the list,
        # so the turn-end flush doesn't skip the assistant/tool chain (#44837).
        from agent.agent_runtime_helpers import repair_message_sequence_with_cursor
        repaired_seq = repair_message_sequence_with_cursor(agent, _ctx.messages)
        if repaired_seq > 0:
            request_logger.info(
                "Repaired %s message-alternation violations before request (session=%s)",
                repaired_seq,
                agent.session_id or "-",
            )

        api_messages = []
        for idx, msg in enumerate(_ctx.messages):

            # Structural clone, NOT msg.copy(): every in-place transform
            # below (canonicalize/repair, surrogate + non-ASCII sanitizers,
            # cache decoration) must be unable to reach the persisted
            # history through shared nested containers. See
            # _clone_message_for_send.
            api_msg = _clone_message_for_send(msg)

            # api_content is the persistence sidecar carrying the exact bytes
            # sent to the API for this message when they differ from the clean
            # stored content (see compose_user_api_content in turn_context).
            # It is bookkeeping, never a provider field — pop it from EVERY
            # outgoing copy.
            _api_content = api_msg.pop("api_content", None)

            # Display-only timeline metadata. Never a provider field — strip
            # from every outgoing copy so strict OpenAI-compatible backends
            # don't reject the request after a model switch or resumed typed
            # event row enters the live history.
            api_msg.pop("display_kind", None)
            api_msg.pop("display_metadata", None)

            # Durable row identity stamped by _rows_to_conversation so the
            # desktop can address a specific persisted message (reactions).
            # Bookkeeping, never a provider field — only the chat-completions
            # transport strips underscore keys, so drop it centrally here.
            api_msg.pop("_row_id", None)

            # Inject ephemeral context into the current turn's user message.
            # Sources: memory manager prefetch + plugin pre_llm_call hooks
            # with target="user_message" (the default).  Both are
            # API-call-time only — the original message in `messages` is
            # never mutated beyond the api_content stamp, so nothing leaks
            # into the clean transcript content.
            if idx == _ctx.current_turn_user_idx and msg.get("role") == "user":
                if isinstance(_api_content, str) and _api_content:
                    # Stamped by the prologue from the same composition —
                    # reuse it so the persisted sidecar and the wire cannot
                    # drift, and so every pass this turn sends identical
                    # bytes (composed from msg["content"], never from a
                    # previously-injected copy).
                    api_msg["content"] = _api_content
                else:
                    # Callers that bypass the prologue stamping: compose live.
                    _composed = compose_user_api_content(
                        api_msg.get("content", ""),
                        _ext_prefetch_cache,
                        _plugin_user_context,
                    )
                    if _composed is not None:
                        api_msg["content"] = _composed
            elif (
                isinstance(_api_content, str)
                and _api_content
                and msg.get("role") in ("user", "assistant")
            ):
                # Historical message: replay the exact bytes sent when it was
                # live, so the provider prompt-cache prefix stays byte-stable
                # instead of diverging at the injection point and
                # re-prefilling everything after it. User rows carry the
                # prefetch/plugin injection sidecar; user AND assistant rows
                # can carry a sanitize-divergence sidecar (content that
                # ``get_messages_as_conversation``'s sanitize_context/strip
                # would rewrite on reload — see the capture in
                # ``_flush_messages_to_session_db``).
                api_msg["content"] = _api_content

            # For ALL assistant messages, pass reasoning back to the API
            # This ensures multi-turn reasoning context is preserved
            agent._copy_reasoning_content_for_api(msg, api_msg)

            # Remove 'reasoning' field - it's for trajectory storage only
            # We've copied it to 'reasoning_content' for the API above
            if "reasoning" in api_msg:
                api_msg.pop("reasoning")
            # Remove finish_reason - not accepted by strict APIs (e.g. Mistral)
            if "finish_reason" in api_msg:
                api_msg.pop("finish_reason")
            # _thinking_prefill survives here intentionally: the drop pass below
            # needs it. The transport strips all underscore keys before the wire.
            # Strip length-continuation marks; not every transport drops underscore keys.
            api_msg.pop("_length_continuation_fragment", None)
            api_msg.pop("_length_continuation_nudge", None)
            # Strip Codex Responses API fields (call_id, response_item_id) for
            # strict providers like Mistral, Fireworks, etc. that reject unknown fields.
            # Uses new dicts so the internal messages list retains the fields
            # for Codex Responses compatibility.
            if agent._should_sanitize_tool_calls():
                # In MoA mode, agent.model is the virtual preset name
                # (e.g. "closed"), not the actual aggregator model.  Use
                # the resolved aggregator model so Gemini aggregators
                # correctly preserve thought_signature (extra_content).
                _sanitize_model = agent.model
                if agent.provider == "moa":
                    if moa_config:
                        _agg = moa_config.get("aggregator") or {}
                        if _agg.get("model"):
                            _sanitize_model = _agg["model"]
                    if _sanitize_model == agent.model:
                        # Virtual-provider mode: no moa_config is threaded
                        # through run_conversation — the facade resolves the
                        # preset internally. Ask the facade for the resolved
                        # aggregator slot from the previous create() instead
                        # (set before any history replay that could carry
                        # thought_signature).
                        _moa_client = getattr(agent, "client", None)
                        _agg_slot = getattr(_moa_client, "last_aggregator_slot", None)
                        if _agg_slot and _agg_slot.get("model"):
                            _sanitize_model = _agg_slot["model"]
                agent._sanitize_tool_calls_for_strict_api(api_msg, model=_sanitize_model)
            # Keep 'reasoning_details' - OpenRouter uses this for multi-turn reasoning context
            # The signature field helps maintain reasoning continuity
            api_messages.append(api_msg)

        # Build the final system message: cached prompt + ephemeral system prompt.
        # Ephemeral additions are API-call-time only (not persisted to session DB).
        # External recall context is injected into the user message, not the system
        # prompt, so the stable cache prefix remains unchanged.
        #
        # NOTE: Plugin context from pre_llm_call hooks is injected into the
        # user message (see injection block above), NOT the system prompt.
        # This is intentional — system prompt modifications break the prompt
        # cache prefix.  The system prompt is reserved for Hermes internals.
        #
        # Hermes invariant: the system prompt is built ONCE per session
        # (cached on ``_cached_system_prompt``) and replayed verbatim on
        # every turn. ``apply_anthropic_cache_control`` may split its stable
        # prefix into content blocks on the wire, but the stored string and
        # its byte-stability remain unchanged.
        effective_system = _ctx.active_system_prompt or ""
        if agent.ephemeral_system_prompt:
            effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
        if effective_system:
            api_messages = [{"role": "system", "content": effective_system}] + api_messages

        if moa_config:
            try:
                from agent.message_content import flatten_message_text as _flatten_mt
                from agent.moa_loop import _preset_temperature, aggregate_moa_context

                _moa_context = aggregate_moa_context(
                    user_prompt=(
                        _ctx.original_user_message
                        if isinstance(_ctx.original_user_message, str)
                        # Multimodal / decorated content list: extract the
                        # visible text instead of str()-ing a Python repr of
                        # the parts (which would leak base64 image payloads
                        # into the aggregator prompt).
                        else _flatten_mt(_ctx.original_user_message)
                    ),
                    api_messages=api_messages,
                    reference_models=moa_config.get("reference_models") or [],
                    aggregator=moa_config.get("aggregator") or {},
                    temperature=_preset_temperature(moa_config, "reference_temperature"),
                    aggregator_temperature=_preset_temperature(moa_config, "aggregator_temperature"),
                    reference_max_tokens=moa_config.get("reference_max_tokens"),
                    # None = no per-preset override; inherit
                    # auxiliary.moa_reference.timeout via call_llm.
                    reference_timeout=(
                        float(moa_config["reference_timeout"])
                        if moa_config.get("reference_timeout")
                        else None
                    ),
                    degraded_reference_policy=str(
                        moa_config.get("degraded_reference_policy") or "loud"
                    ),
                    agent=agent,
                )
                if _moa_context:
                    for _msg in reversed(api_messages):
                        if _msg.get("role") == "user":
                            _base = _msg.get("content", "")
                            if isinstance(_base, str):
                                _msg["content"] = _base + "\n\n" + _moa_context
                            elif isinstance(_base, list):
                                # Multimodal user turn (text + image parts):
                                # append the MoA context as a trailing text
                                # part instead of silently dropping it.
                                _msg["content"] = [
                                    *_base,
                                    {"type": "text", "text": "\n\n" + _moa_context},
                                ]
                            break
            except Exception as _moa_exc:
                logger.warning("MoA context aggregation failed: %s", _moa_exc)

        # Inject ephemeral prefill messages right after the system prompt
        # but before conversation history. Same API-call-time-only pattern.
        if agent.prefill_messages:
            sys_offset = 1 if (api_messages and api_messages[0].get("role") == "system") else 0
            for idx, pfm in enumerate(agent.prefill_messages):
                # Structural clone: the sanitizers below run over
                # api_messages in place, and a shallow copy would let them
                # write through into agent.prefill_messages' nested
                # containers (same aliasing class as the history build).
                api_messages.insert(sys_offset + idx, _clone_message_for_send(pfm))

        # Per-turn context selection hook (additive, no-op by default).
        # Lets a context engine select/replace which context enters the
        # prompt for THIS call only — retrieval, topic routing, role/branch
        # switching — distinct from compression and independent of
        # should_compress(). Request-only: persisted history is untouched, so
        # caching/sanitization below operate on whatever the engine selected.
        # Fail-open (see _apply_context_engine_selection).
        _sel_incoming = (
            _ctx.messages[_ctx.current_turn_user_idx]
            if 0 <= _ctx.current_turn_user_idx < len(_ctx.messages)
            else None
        )
        api_messages = _apply_context_engine_selection(
            agent,
            api_messages,
            _ctx.messages,
            _sel_incoming,
            logger=request_logger,
        )

        # Request-copy only: never change the durable transcript or the
        # conversation's cached system prefix. A preflight compression restart
        # leaves delivery pending; physical recovery retains the same guidance.
        if semantic_progress.guidance_pending and not agent._interrupt_requested:
            api_messages.append({"role": "user", "content": SEMANTIC_PROGRESS_NUDGE})

        # Safety net: strip orphaned tool results / add stubs for missing
        # results before sending to the API.  Runs unconditionally — not
        # gated on context_compressor — so orphans from session loading or
        # manual message manipulation are always caught.
        api_messages = agent._sanitize_api_messages(api_messages)

        # Drop thinking-only assistant turns (reasoning but no visible
        # output and no tool_calls) and merge any adjacent user messages
        # left behind. Prevents Anthropic 400s ("The final block in an
        # assistant message cannot be `thinking`.") and equivalent errors
        # from third-party Anthropic-compatible gateways that can't replay
        # a thinking-only turn. Runs on the per-call copy only — the
        # stored conversation history keeps the reasoning block for the
        # UI transcript and session persistence.
        api_messages = agent._drop_thinking_only_and_merge_users(
            api_messages,
            drop_codex_reasoning_items=agent.api_mode != "codex_responses",
        )

        # Normalize message whitespace and tool-call JSON for consistent
        # prefix matching.  Ensures bit-perfect prefixes across turns,
        # which enables KV cache reuse on local inference servers
        # (llama.cpp, vLLM, Ollama) and improves cache hit rates for
        # cloud providers.  Operates on api_messages (the API copy) so
        # the original conversation history in `messages` is untouched.
        for am in api_messages:
            if isinstance(am.get("content"), str):
                am["content"] = am["content"].strip()
        _canonicalize_api_tool_calls(api_messages)

        # Proactively strip any surrogate characters before the API call.
        # Models served via Ollama (Kimi K2.5, GLM-5, Qwen) can return
        # lone surrogates (U+D800-U+DFFF) that crash json.dumps() inside
        # the OpenAI SDK. Sanitizing here prevents the 3-retry cycle.
        _sanitize_messages_surrogates(api_messages)

        # NOTE (empty-content class fix): no send-time pad loop here.  The
        # single owner for "never send a turn strict wire validation rejects
        # as empty" is ``repair_empty_non_final_messages``, which runs inside
        # ``_sanitize_api_messages`` above — the unconditional pre-send
        # chokepoint shared with the summary path.  Its placeholder is
        # non-whitespace, so it survives the whitespace-normalization pass
        # regardless of ordering (a single-space pad here previously had to
        # be sequenced after normalization to survive, forking the concept).

        # Build the request-local cache sections only after every transcript
        # mutation. The canonical tool registry stays undecorated.
        #
        # Runs LAST, after every message mutation above. Marking earlier
        # defeats the prefix stability the mutations exist to create:
        # ``_apply_cache_marker`` rewrites ``content`` from a plain string
        # into a ``[{"type": "text", ...}]`` block, so the marked messages
        # no longer match the ``isinstance(content, str)`` test in the
        # whitespace-normalization pass and silently keep their raw
        # leading/trailing whitespace. A tool result ending in "\n" is
        # therefore sent unstripped while it sits in the last-3 window and
        # stripped once it rolls out of it — the same message, different
        # bytes on consecutive turns, which breaks the prefix match at
        # exactly the point the breakpoints were meant to protect. Marking
        # last also keeps breakpoints off messages that the orphan sweep or
        # the thinking-only drop is about to remove or merge away.
        tools_for_api = agent.tools
        if agent._use_prompt_caching and agent.provider != "moa":
            _static_system_prefix = getattr(agent, "_cached_system_prompt_static", None)
            _initial_cache_plan = build_prompt_cache_plan(
                api_messages,
                tools_for_api,
                # Clamp per-destination: a configured 1h regresses to 5m on
                # Qwen/Alibaba routes, whose context cache is 5m-only (#84733).
                cache_ttl=effective_cache_ttl(
                    agent._cache_ttl,
                    provider=agent.provider,
                    model=agent.model,
                ),
                native_anthropic=agent._use_native_cache_layout,
                static_system_prefix=(
                    _static_system_prefix
                    if isinstance(_static_system_prefix, str)
                    else None
                ),
                direct_native_tool_cache=agent._direct_native_anthropic_tool_cache_capability(),
            )
            api_messages = _initial_cache_plan.messages
            tools_for_api = _initial_cache_plan.tools

        # Build a persistent-MoA request before measuring compression pressure.
        # MoA reference output is injected into the aggregator prompt, but it
        # is deliberately ephemeral and therefore absent from ``messages``.
        # Preparing here makes the pre-API guard measure the exact prompt the
        # aggregator will receive; ``create()`` consumes this private prepared
        # request later without running the advisors a second time.
        _moa_prepared_request = None
        if agent.provider == "moa":
            _moa_completions = getattr(getattr(agent.client, "chat", None), "completions", None)
            if pending_moa_prepared_request is not None:
                _rebase_moa_request = getattr(_moa_completions, "rebase_prepared_request", None)
                if callable(_rebase_moa_request):
                    _moa_prepared_request = _rebase_moa_request(
                        pending_moa_prepared_request, api_messages
                    )
                pending_moa_prepared_request = None
            if _moa_prepared_request is None:
                _prepare_moa_request = getattr(_moa_completions, "prepare", None)
                if callable(_prepare_moa_request):
                    _moa_prepared_request = _prepare_moa_request(api_messages)
            if _moa_prepared_request is not None:
                api_messages = _moa_prepared_request["messages"]

        # One image-stripped message estimate feeds both figures. Was: a
        # str(msg) char walk (re-serialized base64 every call) + a second
        # messages walk inside estimate_request_tokens_rough. Tools added
        # separately (compression needs them: 50+ tools = 20-30K tokens).
        # total_chars is a rough (~) proxy — verbose log + hook metric only.
        approx_tokens = estimate_messages_tokens_rough(api_messages)
        request_pressure_tokens = approx_tokens + (
            _estimate_tools_tokens_rough(agent.tools) if agent.tools else 0
        )
        total_chars = approx_tokens * 4
        # Stash this request's rough estimate so update_from_response() can
        # pair it with the provider's real prompt count — the (rough, real)
        # anchor behind should_defer_preflight_to_real_usage()'s projection.
        # getattr guard: test doubles built via object.__new__ lack the method.
        _note_rough = getattr(
            agent.context_compressor, "note_request_rough_estimate", None
        )
        if callable(_note_rough):
            _note_rough(request_pressure_tokens)

        _runtime_context_error = _ollama_context_limit_error(
            agent, request_pressure_tokens
        )
        if _runtime_context_error:
            final_response = _runtime_context_error
            failed = True
            _turn_exit_reason = "ollama_runtime_context_too_small"
            append_message(_ctx.messages, {"role": "assistant", "content": final_response})
            agent._emit_status("❌ Ollama runtime context is too small for Hermes tool use")
            api_call_count -= 1
            agent._api_call_count = api_call_count
            try:
                agent.iteration_budget.refund()
            except Exception:
                pass
            _turn_controller.move(TransitionKind.FINALIZE, TurnReason.FAILURE)
            break

        # Pre-API pressure check. The turn-prologue preflight only saw the
        # incoming user message; a single turn can then grow by many large
        # tool results and leave no output budget before the NEXT call (the
        # live 271k/272k Codex failure). The post-response should_compress
        # gate at the tool-loop tail uses API-reported last_prompt_tokens,
        # which LAGS a just-appended huge tool result — so it misses this
        # case. Re-check here against the current request estimate.
        #
        # Mirror the turn-prologue preflight's guard chain exactly (see
        # turn_context.py): (1) defer when the rough estimate is known-noisy
        # relative to a recent real provider prompt that fit under threshold
        # (schema overhead / post-compaction over-count, #36718); (2) skip
        # while a same-session compression-failure cooldown is active; (3) then
        # should_compress() — reusing the canonical threshold_tokens (output
        # room already reserved by _compute_threshold_tokens) and its summary-
        # LLM cooldown + anti-thrash guards (#11529). compression_attempts is a
        # hard per-turn backstop shared with the overflow error handlers.
        _compressor = agent.context_compressor
        _preflight_threshold = int(
            getattr(_compressor, "threshold_tokens", 0) or 0
        )
        # A previous mid-turn preflight pass deliberately continued the loop so
        # API-only context and all sanitization could be rebuilt. Compare that
        # fully assembled request with the fully assembled request that caused
        # the pass. Raw ``messages`` are not equivalent here: they omit
        # api_content/plugin injections, prefills, MoA context, and ephemeral
        # system text.
        _previous_preflight_pressure = _recovery.last_preflight_pressure
        _recovery.last_preflight_pressure = None
        if (
            _previous_preflight_pressure is not None
            and request_pressure_tokens >= _preflight_threshold
            and not _compression_warrants_another_preflight_pass(
                _previous_preflight_pressure,
                request_pressure_tokens,
                _preflight_threshold,
            )
        ):
            # Stop proactive retries for this turn without consuming the
            # shared overflow-recovery budget. If the provider proves the
            # request truly does not fit, its error handler may still compact
            # with that stronger signal.
            _ctx.preflight_compression_blocked = True
            logger.warning(
                "Pre-API compression made insufficient progress: ~%s -> "
                "~%s request tokens; skipping additional preflight passes",
                f"{_previous_preflight_pressure:,}",
                f"{request_pressure_tokens:,}",
            )
        _defer_preflight = getattr(
            _compressor, "should_defer_preflight_to_real_usage", lambda _t: False
        )
        _compression_cooldown = getattr(
            _compressor, "get_active_compression_failure_cooldown", lambda: None
        )()
        if (
            agent.compression_enabled
            and len(_ctx.messages) > 1
            and _recovery.compression_attempts < _recovery.max_compression_attempts
            and not _ctx.preflight_compression_blocked
            and not _defer_preflight(request_pressure_tokens)
            and not _compression_cooldown
            and _compressor.should_compress(request_pressure_tokens)
        ):
            if _moa_prepared_request is not None:
                pending_moa_prepared_request = _moa_prepared_request
            _recovery.compression_attempts += 1
            # Compression is actually running (block cleared / was never
            # blocked) — reset the blocked-overflow warning dedup so a future
            # blocked-over-threshold turn can warn again. Mirrors the
            # turn-context preflight reset (silent-overflow fix #62625).
            # getattr guard: test doubles built via object.__new__ lack the
            # method (gateway test-double pitfall) — treat absence as no-op.
            _clear_warn = getattr(agent, "_clear_context_overflow_warn", None)
            if callable(_clear_warn):
                _clear_warn()
            logger.info(
                "Pre-API compression: ~%s request tokens >= %s threshold "
                "(context=%s, attempt=%s/%s)",
                f"{request_pressure_tokens:,}",
                f"{int(getattr(_compressor, 'threshold_tokens', 0) or 0):,}",
                f"{int(getattr(_compressor, 'context_length', 0) or 0):,}"
                if getattr(_compressor, "context_length", 0) else "unknown",
                _recovery.compression_attempts,
                _recovery.max_compression_attempts,
            )
            _pre_api_status = automatic_compaction_status_message(
                _compressor,
                phase="pre_api",
                default_message=PRE_API_COMPRESSION_STATUS_TEMPLATE.format(
                    tokens=request_pressure_tokens
                ),
                approx_tokens=request_pressure_tokens,
                threshold_tokens=int(
                    getattr(_compressor, "threshold_tokens", 0) or 0
                ),
                context_length=int(
                    getattr(_compressor, "context_length", 0) or 0
                ),
                model=agent.model,
                attempt=_recovery.compression_attempts,
                max_attempts=_recovery.max_compression_attempts,
            )
            if _pre_api_status:
                agent._emit_status(_pre_api_status)
            _recovery.last_preflight_pressure = request_pressure_tokens
            _pre_api_input = _ctx.messages
            _ctx.messages, _ctx.active_system_prompt = agent._compress_context(
                _ctx.messages,
                system_message,
                approx_tokens=request_pressure_tokens,
                task_id=_ctx.effective_task_id,
            )
            if _ctx.messages is _pre_api_input and compression_skipped_due_to_lock(agent):
                # #69870 lock-skip: another path holds this session's
                # compression lock, so this pass no-oped. That is a temporary
                # DEFER, not evidence about compressibility — refund the
                # attempt (it must not burn the shared overflow-recovery
                # budget toward compression_exhausted → gateway auto-reset,
                # #9893/#35809) and leave the insufficient-progress blocker
                # unarmed. Proceed with the current request: if it truly does
                # not fit, the provider's 413/overflow handler returns the
                # soft compression_deferred result with that stronger signal.
                _recovery.compression_attempts -= 1
                _recovery.last_preflight_pressure = None
                if pending_moa_prepared_request is _moa_prepared_request:
                    pending_moa_prepared_request = None
            else:
                # Reset retry/empty-response state so the compacted request
                # gets a fresh chance instead of inheriting stale recovery
                # counters from the pre-compaction history.
                agent._empty_content_retries = 0
                agent._thinking_prefill_retries = 0
                agent._last_content_with_tools = None
                agent._last_content_tools_all_housekeeping = False
                agent._mute_post_response = False
                # Re-baseline the flush cursor for the compaction mode that just
                # ran. Legacy session-rotation returns None (the child session has
                # not seen the compacted transcript, so the next flush writes it
                # whole); in-place compaction returns list(messages) because the
                # compacted rows are already persisted under the same session id —
                # leaving None there would re-append them, doubling the active
                # context and retriggering compression. Mirrors the post-response
                # and preflight compaction sites; see
                # conversation_history_after_compression().
                _ctx.conversation_history = conversation_history_after_compression(
                    agent, _ctx.messages, _ctx.conversation_history
                )
                # This preflight iteration never reaches the provider whether
                # we skip the turn (handoff guard below) or re-run the loop —
                # refund the consumed call/budget in BOTH cases, mirroring the
                # ollama_runtime_context_too_small early-exit above. Without
                # the refund on the break path, every skipped turn leaked one
                # iteration-budget unit for the agent's lifetime and
                # finalize_turn logged an api_call_count including a call that
                # was never made.
                api_call_count -= 1
                agent._api_call_count = api_call_count
                agent.iteration_budget.refund()
                if _should_skip_model_call_for_reference_handoff(
                    _ctx.messages, _ctx.user_message
                ):
                    # Reference-only handoff must not become the active turn
                    # after a completed assistant response (#80622).
                    logger.info(
                        "Skipping post-compaction model call: reference-only "
                        "handoff would be the sole active user turn (#80622)"
                    )
                    if not final_response:
                        final_response = _HANDOFF_SKIP_FINAL_RESPONSE
                    _turn_exit_reason = "compaction_handoff_not_actionable"
                    _turn_controller.move(TransitionKind.FINALIZE, TurnReason.HANDOFF)
                    break
                _turn_controller.move(
                    TransitionKind.REBUILD, TurnReason.COMPRESSION,
                    budget=BudgetEffect.REFUND_STEP,
                    durability=DurabilityBoundary.COMPRESSION,
                )
                continue
        elif (
            agent.compression_enabled
            and len(_ctx.messages) > 1
            and _recovery.compression_attempts < _recovery.max_compression_attempts
            and not _defer_preflight(request_pressure_tokens)
            and _compression_cooldown
        ):
            # Blocked by the summary-LLM cooldown. Surface a deduped warning
            # (only when actually over threshold — should_compress_info
            # returns a None reason below threshold) so the user isn't left
            # with a silently growing context. Mirrors the turn-context
            # preflight and the loop-compaction guards (silent-overflow fix
            # #62625).
            _block_reason = None
            try:
                _block_reason = _compressor.should_compress_info(
                    request_pressure_tokens
                )[1]
            except Exception:
                _block_reason = None
            if _block_reason:
                agent._warn_context_overflow_blocked(
                    _block_reason,
                    request_pressure_tokens,
                    int(getattr(_compressor, "threshold_tokens", 0) or 0),
                )
        
        # Thinking spinner for quiet mode (animated during API call)
        thinking_spinner = None
        
        if not agent.quiet_mode:
            agent._vprint(f"\n{agent.log_prefix}🔄 Making API call #{api_call_count}/{agent.max_iterations}...")
            agent._vprint(f"{agent.log_prefix}   📊 Request size: {len(api_messages)} messages, ~{approx_tokens:,} tokens (~{total_chars:,} chars)")
            agent._vprint(f"{agent.log_prefix}   🔧 Available tools: {len(agent.tools) if agent.tools else 0}")
        else:
            # Animated thinking spinner in quiet mode
            face = random.choice(KawaiiSpinner.get_thinking_faces())
            verb = random.choice(KawaiiSpinner.get_thinking_verbs())
            if agent.thinking_callback:
                # CLI TUI mode: use prompt_toolkit widget instead of raw spinner
                # (works in both streaming and non-streaming modes)
                agent.thinking_callback(f"{face} {verb}...")
            elif not agent._has_stream_consumers() and agent._should_start_quiet_spinner():
                # Raw KawaiiSpinner only when no streaming consumers and the
                # spinner output has a safe sink.
                spinner_type = random.choice(['brain', 'sparkle', 'pulse', 'moon', 'star'])
                thinking_spinner = KawaiiSpinner(f"{face} {verb}...", spinner_type=spinner_type, print_fn=agent._print_fn)
                thinking_spinner.start()
        
        # Log request details if verbose
        if agent.verbose_logging:
            logging.debug(f"API Request - Model: {agent.model}, Messages: {len(_ctx.messages)}, Tools: {len(agent.tools) if agent.tools else 0}")
            logging.debug(f"Last message role: {_ctx.messages[-1]['role'] if _ctx.messages else 'none'}")
            logging.debug(f"Total message size: ~{approx_tokens:,} tokens")
        
        _cycle = RequestCycle(agent._api_max_retries)
        _turn_controller.move(TransitionKind.REQUEST, TurnReason.PREPARED)
        api_start_time = time.time()
        _cycle.retry_count = 0
        _cycle.max_retries = agent._api_max_retries

        finish_reason = "stop"
        response = None  # Guard against UnboundLocalError if all retries fail
        api_kwargs = None  # Guard against UnboundLocalError in except handler
        api_request_id = f"{_ctx.turn_id}:api:{api_call_count}"
        agent._current_api_request_id = api_request_id

        _request_outcome = run_request_cycle(
            agent, _ctx=_ctx, _continuation=_continuation, _recovery=_recovery,
            _turn_controller=_turn_controller, _cycle=_cycle,
            semantic_progress=semantic_progress, system_message=system_message,
            api_call_count=api_call_count, api_messages=api_messages,
            tools_for_api=tools_for_api, approx_tokens=approx_tokens,
            total_chars=total_chars, _moa_prepared_request=_moa_prepared_request,
            thinking_spinner=thinking_spinner, api_start_time=api_start_time,
            api_request_id=api_request_id,
        )
        if isinstance(_request_outcome, dict):
            return _request_outcome
        response = _request_outcome.response
        api_kwargs = _request_outcome.api_kwargs
        api_messages = _request_outcome.api_messages
        api_duration = _request_outcome.api_duration
        interrupted = _request_outcome.interrupted
        if interrupted:
            final_response = _request_outcome.final_response

        _request_exit = _cycle.exit_reason(interrupted=interrupted)
        if _request_exit is TurnReason.REDIRECT:
            api_call_count = _cycle.apply_restart(_turn_controller, agent, api_call_count)
            # The cancelled request produced no valid assistant item. Reuse the
            # same logical iteration after the outer loop appends the displayed
            # partial context and correction to ``messages``.
            continue

        # If the API call was interrupted, skip response processing
        if interrupted:
            _turn_exit_reason = "interrupted_during_api_call"
            _turn_controller.move(TransitionKind.FINALIZE, TurnReason.INTERRUPT)
            break

        if _request_exit is TurnReason.COMPRESSION:
            api_call_count = _cycle.apply_restart(_turn_controller, agent, api_call_count)
            # Count compression restarts toward the retry limit to prevent
            # infinite loops when compression reduces messages but not enough
            # to fit the context window.
            _cycle.retry_count += 1
            if _should_skip_model_call_for_reference_handoff(
                _ctx.messages, _ctx.user_message
            ):
                logger.info(
                    "Skipping compressed-restart model call: reference-only "
                    "handoff would be the sole active user turn (#80622)"
                )
                if not final_response:
                    final_response = _HANDOFF_SKIP_FINAL_RESPONSE
                _turn_exit_reason = "compaction_handoff_not_actionable"
                _turn_controller.move(TransitionKind.FINALIZE, TurnReason.HANDOFF)
                break
            # In-loop compression rebuilt `messages` with fresh compaction
            # copies, so the pre-compression current-turn index is stale.
            # Re-anchor exactly like the prologue does: a stale index that
            # lands on a historical user message would make the live-compose
            # fallback inject this turn's prefetch into that message on the
            # wire only, diverging the next turn's replayed prefix there.
            # Ordered AFTER the handoff guard: the guard may have re-appended
            # this turn's real user ask (restore path), and the anchor must
            # land on that restored row, not on -1 / a pre-restore index.
            _ctx.current_turn_user_idx = reanchor_current_turn_user_idx(
                _ctx.messages, _ctx.user_message
            )
            agent._persist_user_message_idx = _ctx.current_turn_user_idx
            continue

        if _request_exit is TurnReason.PROVIDER_SWITCH:
            api_call_count = _cycle.apply_restart(_turn_controller, agent, api_call_count)
            # A stream stall or provider failure was escalated to the
            # fallback chain (activation sites in the request cycle select
            # this transition and return here).  Re-issue the API call against the
            # now-active fallback provider.  Refund the budget/count for the
            # stalled attempt so the fallback gets a fair turn.
            # Failover shrank the compressor's context window to the
            # fallback's; clear the preflight block so the pre-API preflight
            # re-runs against the new threshold before the first fallback
            # call (#84733). Hoisted here (the single consumer) so every
            # activation site — including ones added later — gets it.
            _ctx.preflight_compression_blocked = False
            continue

        if _request_exit in {TurnReason.LENGTH, TurnReason.TRANSPORT_PARTIAL}:
            api_call_count = _cycle.apply_restart(_turn_controller, agent, api_call_count)
            if _recovery.transport is TransportRecoveryState.PARTIAL_CONTINUATION:
                # Transport continuation: the provider never reported an
                # output cap, so the budget must not move.  Boosting here is
                # what made a network drop look like — and cost like — a
                # genuine output truncation.
                agent._ephemeral_max_output_tokens = None
                continue
            # Progressively boost the output token budget on each retry.
            # Retry 1 → 2× base, retry 2 → 4× base, retry 3 → 8× base,
            # retry 4 → 16× base, then cap at 32 768.
            # Applies to all providers via _ephemeral_max_output_tokens.
            # If the original request already used a larger provider/model
            # default budget, keep that floor so continuation retries do
            # not accidentally downshift to a much smaller cap.
            agent._ephemeral_max_output_tokens = _continuation.output_cap(
                agent, api_kwargs, _continuation.length_retries,
            )
            continue

        # Guard: if all retries exhausted without a successful response
        # (e.g. repeated context-length errors that exhausted retry_count),
        # the `response` variable is still None. Break out cleanly.
        if response is None:
            _turn_exit_reason = "all_retries_exhausted_no_response"
            print(f"{agent.log_prefix}❌ All API retries exhausted with no successful response.")
            agent._persist_session(_ctx.messages, _ctx.conversation_history)
            _turn_controller.move(TransitionKind.FINALIZE, TurnReason.FAILURE)
            break

        _turn_controller.move(TransitionKind.INTERPRET, TurnReason.RESPONSE)
        try:
            _transport = agent._get_transport()
            _normalize_kwargs = {}
            if agent.api_mode == "anthropic_messages":
                _normalize_kwargs["strip_tool_prefix"] = agent._is_anthropic_oauth
            normalized = _transport.normalize_response(response, **_normalize_kwargs)
            semantic_progress.request_completed()
            assistant_message = normalized
            finish_reason = normalized.finish_reason

            # Normalize content to string — some OpenAI-compatible servers
            # (llama-server, etc.) return content as a dict or list instead
            # of a plain string, which crashes downstream .strip() calls.
            if assistant_message.content is not None and not isinstance(assistant_message.content, str):
                raw = assistant_message.content
                if isinstance(raw, dict):
                    assistant_message.content = raw.get("text", "") or raw.get("content", "") or json.dumps(raw)
                elif isinstance(raw, list):
                    # Multimodal content list — extract text parts
                    parts = []
                    for part in raw:
                        if isinstance(part, str):
                            parts.append(part)
                        elif isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif isinstance(part, dict) and "text" in part:
                            parts.append(str(part["text"]))
                    assistant_message.content = "\n".join(parts)
                else:
                    assistant_message.content = str(raw)

            try:
                from hermes_cli.lifecycle import (
                    has_hook,
                    invoke_hook as _invoke_hook,
                )
                if has_hook("post_api_request"):
                    _assistant_tool_calls = (
                        getattr(assistant_message, "tool_calls", None) or []
                    )
                    _assistant_text = assistant_message.content or ""
                    _api_ended_at = api_start_time + api_duration
                    _invoke_hook(
                        "post_api_request",
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
                        api_duration=api_duration,
                        started_at=api_start_time,
                        ended_at=_api_ended_at,
                        finish_reason=finish_reason,
                        message_count=len(api_messages),
                        response_model=getattr(response, "model", None),
                        response=agent._api_response_payload_for_hook(
                            response,
                            assistant_message,
                            finish_reason=finish_reason,
                        ),
                        usage=agent._usage_summary_for_api_request_hook(response),
                        assistant_message=assistant_message,
                        assistant_content_chars=len(_assistant_text),
                        assistant_tool_call_count=len(_assistant_tool_calls),
                        moa_references=_moa_reference_metrics_for_hook(agent),
                    )
            except Exception:
                pass

            # Handle assistant response
            if assistant_message.content and not agent.quiet_mode:
                if agent.verbose_logging:
                    agent._vprint(f"{agent.log_prefix}🤖 Assistant: {assistant_message.content}")
                else:
                    agent._vprint(f"{agent.log_prefix}🤖 Assistant: {assistant_message.content[:100]}{'...' if len(assistant_message.content) > 100 else ''}")

            # Notify progress callback of model's thinking (used by subagent
            # delegation to relay the child's reasoning to the parent display).
            if (assistant_message.content and agent.tool_progress_callback):
                _think_text = assistant_message.content.strip()
                # Strip reasoning XML tags that shouldn't leak to parent display
                _think_text = re.sub(
                    r'</?(?:REASONING_SCRATCHPAD|think|reasoning)>', '', _think_text
                ).strip()
                # For subagents: relay first line to parent display (existing behaviour).
                # For all agents with a structured callback: emit reasoning.available event.
                first_line = _think_text.split('\n')[0][:80] if _think_text else ""
                if first_line and getattr(agent, '_delegate_depth', 0) > 0:
                    try:
                        agent.tool_progress_callback("_thinking", first_line)
                    except Exception:
                        pass
                elif _think_text:
                    try:
                        agent.tool_progress_callback("reasoning.available", "_thinking", _think_text[:500], None)
                    except Exception:
                        pass

            # Check for incomplete <REASONING_SCRATCHPAD> (opened but never closed)
            # This means the model ran out of output tokens mid-reasoning — retry up to 2 times
            if has_incomplete_scratchpad(assistant_message.content or ""):
                agent._incomplete_scratchpad_retries += 1

                agent._buffer_vprint("⚠️  Incomplete <REASONING_SCRATCHPAD> detected (opened but never closed)")

                if agent._incomplete_scratchpad_retries <= 2:
                    agent._buffer_vprint(f"🔄 Retrying API call ({agent._incomplete_scratchpad_retries}/2)...")
                    # Don't add the broken message, just retry
                    _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.SCRATCHPAD)
                    continue
                else:
                    # Max retries - discard this turn and save as partial
                    agent._flush_status_buffer()
                    agent._vprint(f"{agent.log_prefix}❌ Max retries (2) for incomplete scratchpad. Saving as partial.", force=True)
                    agent._incomplete_scratchpad_retries = 0
                    
                    rolled_back_messages = agent._get_messages_up_to_last_assistant(_ctx.messages)
                    agent._cleanup_task_resources(_ctx.effective_task_id)
                    agent._persist_session(_ctx.messages, _ctx.conversation_history)
                    
                    return _complete_direct_turn(_turn_controller, {
                        "final_response": "Incomplete REASONING_SCRATCHPAD after 2 retries",
                        "messages": rolled_back_messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Incomplete REASONING_SCRATCHPAD after 2 retries"
                    })

            # Reset incomplete scratchpad counter on clean response
            agent._incomplete_scratchpad_retries = 0

            if agent.api_mode == "codex_responses" and finish_reason == "incomplete":
                agent._codex_incomplete_retries += 1

                interim_msg = agent._build_assistant_message(assistant_message, finish_reason)
                interim_has_content = bool((interim_msg.get("content") or "").strip())
                interim_has_reasoning = bool(interim_msg.get("reasoning", "").strip()) if isinstance(interim_msg.get("reasoning"), str) else False
                interim_has_codex_reasoning = bool(interim_msg.get("codex_reasoning_items"))
                interim_has_codex_message_items = bool(interim_msg.get("codex_message_items"))

                if (
                    interim_has_content
                    or interim_has_reasoning
                    or interim_has_codex_reasoning
                    or interim_has_codex_message_items
                ):
                    last_msg = _ctx.messages[-1] if _ctx.messages else None
                    # Duplicate detection: compare only visible content
                    # (content + reasoning).  Opaque provider state
                    # (encrypted reasoning items, message item ids/phases)
                    # drifts per continuation even when the visible output
                    # is identical, so including it in the comparison defeats
                    # dedup and causes message storms (#52711).
                    last_interim_visible = (
                        agent._interim_assistant_visible_text(last_msg)
                        if isinstance(last_msg, dict)
                        else ""
                    )
                    current_interim_visible = agent._interim_assistant_visible_text(interim_msg)
                    if last_interim_visible or current_interim_visible:
                        same_visible_output = last_interim_visible == current_interim_visible
                    else:
                        # Preserve the existing reasoning-only behavior when
                        # neither response has text eligible for interim delivery.
                        same_visible_output = (
                            (last_msg.get("content") or "") == (interim_msg.get("content") or "")
                            and (last_msg.get("reasoning") or "") == (interim_msg.get("reasoning") or "")
                        ) if isinstance(last_msg, dict) else False
                    visible_duplicate = (
                        isinstance(last_msg, dict)
                        and last_msg.get("role") == "assistant"
                        and last_msg.get("finish_reason") == "incomplete"
                        and same_visible_output
                    )
                    if visible_duplicate and isinstance(last_msg, dict):
                        # Update replay state in-place so the latest provider
                        # payload is preserved without re-emitting identical
                        # user-visible commentary.
                        for _key in (
                            "content",
                            "reasoning",
                            "reasoning_content",
                            "reasoning_details",
                            "codex_reasoning_items",
                            "codex_message_items",
                        ):
                            if _key in interim_msg:
                                if _key == "codex_reasoning_items":
                                    # Merge instead of overwrite: a native
                                    # compaction checkpoint captured on the
                                    # earlier incomplete response is the only
                                    # copy — the continuation won't re-emit
                                    # it. See merge_interim_reasoning_items.
                                    from agent.native_compaction import (
                                        merge_interim_reasoning_items,
                                    )
                                    last_msg[_key] = merge_interim_reasoning_items(
                                        last_msg.get(_key), interim_msg[_key]
                                    )
                                else:
                                    last_msg[_key] = interim_msg[_key]
                    else:
                        append_message(_ctx.messages, interim_msg)
                        agent._emit_interim_assistant_message(interim_msg)

                if agent._codex_incomplete_retries < 3:
                    # When the interim message has nothing the Responses
                    # input converter will replay (no visible content, no
                    # encrypted reasoning items, no replayable message
                    # items — plain-text reasoning only), a bare retry is
                    # byte-identical to the request that just came back
                    # incomplete and fails the same way every time
                    # (observed with grok-4.20 on xai-oauth, whose
                    # reasoning items lack encrypted_content).  Append a
                    # user-role nudge so the retry actually differs and
                    # explicitly asks for the final answer.
                    interim_replayable = (
                        interim_has_content
                        or interim_has_codex_reasoning
                        or interim_has_codex_message_items
                    )
                    if not interim_replayable:
                        _last_msg = _ctx.messages[-1] if _ctx.messages else None
                        _already_nudged = (
                            isinstance(_last_msg, dict)
                            and _last_msg.get("role") == "user"
                            and _last_msg.get("content") == _CODEX_INCOMPLETE_NUDGE
                        )
                        # Alternation guard: the nudge is a user-role message,
                        # so it may only follow an assistant message. When the
                        # interim was too empty to append (no content AND no
                        # reasoning), the last message is still the prior
                        # user/tool turn — appending the nudge there would
                        # create a user→user / tool→user sequence that strict
                        # providers reject.
                        _last_is_assistant = (
                            isinstance(_last_msg, dict)
                            and _last_msg.get("role") == "assistant"
                        )
                        if not _already_nudged and _last_is_assistant:
                            append_message(_ctx.messages, {
                                "role": "user",
                                "content": _CODEX_INCOMPLETE_NUDGE,
                            })
                    if not agent.quiet_mode:
                        agent._vprint(f"{agent.log_prefix}↻ Codex response incomplete; continuing turn ({agent._codex_incomplete_retries}/3)")
                    # Surface the continuation on the live spinner/status line
                    # (CLI/TUI/Desktop) and gateway heartbeat: each of these
                    # retries can spend minutes waiting on the provider, and
                    # without a distinct notice the user only sees a generic
                    # thinking spinner ("infinite thinking", #64434).
                    agent._emit_wait_notice(
                        f"↻ model returned reasoning with no final answer — "
                        f"asking it to continue "
                        f"({agent._codex_incomplete_retries}/3)"
                    )
                    agent._session_messages = _ctx.messages
                    _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.CODEX_INCOMPLETE)
                    continue

                agent._codex_incomplete_retries = 0
                agent._persist_session(_ctx.messages, _ctx.conversation_history)
                return _complete_direct_turn(_turn_controller, {
                    "final_response": "Codex response remained incomplete after 3 continuation attempts",
                    "messages": _ctx.messages,
                    "api_calls": api_call_count,
                    "completed": False,
                    "partial": True,
                    "error": "Codex response remained incomplete after 3 continuation attempts",
                })
            elif hasattr(agent, "_codex_incomplete_retries"):
                agent._codex_incomplete_retries = 0
            
            # Check for tool calls
            if assistant_message.tool_calls:
                if not agent.quiet_mode:
                    agent._vprint(f"{agent.log_prefix}🔧 Processing {len(assistant_message.tool_calls)} tool call(s)...")
                
                if agent.verbose_logging:
                    for tc in assistant_message.tool_calls:
                        raw_args = tc.function.arguments
                        args_preview = raw_args[:200] if isinstance(raw_args, str) else repr(raw_args)[:200]
                        logging.debug("Tool call: %s with args: %s...", tc.function.name, args_preview)
                
                # Uniquify duplicate tool-call ids BEFORE any downstream
                # consumer (validation error paths, dispatch, history build,
                # Responses item-id derivation). Models that reuse one id for
                # different calls in a batch otherwise lose the later call's
                # result: the pre-API sanitizer keeps only the first
                # call/result pair per id. See _uniquify_tool_call_ids.
                agent._uniquify_tool_call_ids(assistant_message.tool_calls)

                # Validate tool call names - detect model hallucinations
                # Repair mismatched tool names before validating
                for tc in assistant_message.tool_calls:
                    if tc.function.name not in agent.valid_tool_names:
                        repaired = agent._repair_tool_call(tc.function.name)
                        if repaired:
                            print(f"{agent.log_prefix}🔧 Auto-repaired tool name: '{tc.function.name}' -> '{repaired}'")
                            tc.function.name = repaired
                invalid_tool_calls = [
                    tc.function.name for tc in assistant_message.tool_calls
                    if tc.function.name not in agent.valid_tool_names
                ]
                # Mixed batch: at least one valid call alongside the invalid
                # one(s). Degrading models (observed with gpt-5.6 at very
                # large context) emit batches like 6 named calls + 1
                # blank-name call; voiding the whole turn throws away real
                # work and, across the 3-strike budget, halts sessions that
                # were still making progress. Instead: error-result ONLY the
                # invalid calls (below, after dedup/cap guardrails) and let
                # the valid ones execute. The strike counter only advances
                # when a turn contains NO valid call, so a fully-degenerate
                # model still halts at 3 while a mostly-coherent one keeps
                # working.
                _mixed_invalid_batch = bool(invalid_tool_calls) and any(
                    tc.function.name in agent.valid_tool_names
                    for tc in assistant_message.tool_calls
                )
                if _mixed_invalid_batch:
                    agent._invalid_tool_retries = 0
                    invalid_name = invalid_tool_calls[0]
                    invalid_preview = invalid_name[:80] + "..." if len(invalid_name) > 80 else invalid_name
                    _n_valid = sum(
                        1 for tc in assistant_message.tool_calls
                        if tc.function.name in agent.valid_tool_names
                    )
                    agent._buffer_vprint(
                        f"⚠️  Unknown tool '{invalid_preview}' in batch — erroring that call, "
                        f"executing {_n_valid} valid call(s)"
                    )
                elif invalid_tool_calls:
                    # Track retries for invalid tool calls
                    agent._invalid_tool_retries += 1

                    # Return helpful error to model — model can agent-correct next turn
                    invalid_name = invalid_tool_calls[0]
                    invalid_preview = invalid_name[:80] + "..." if len(invalid_name) > 80 else invalid_name
                    agent._buffer_vprint(f"⚠️  Unknown tool '{invalid_preview}' — sending error to model for agent-correction ({agent._invalid_tool_retries}/3)")

                    if agent._invalid_tool_retries >= 3:
                        agent._flush_status_buffer()
                        agent._vprint(f"{agent.log_prefix}❌ Max retries (3) for invalid tool calls exceeded. Stopping as partial.", force=True)
                        agent._invalid_tool_retries = 0
                        _final_response = f"Model generated invalid tool call: {invalid_preview}"
                        # Prior <3 retries (or an earlier successful tool batch)
                        # leave a tool-result tail. Closing it here matches
                        # interrupt aborts (#48879 / #52592) so the next user
                        # turn is not tool→user for strict providers.
                        close_interrupted_tool_sequence(_ctx.messages, _final_response)
                        agent._persist_session(_ctx.messages, _ctx.conversation_history)
                        return _complete_direct_turn(_turn_controller, {
                            "final_response": _final_response,
                            "messages": _ctx.messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": _final_response
                        })

                    assistant_msg = agent._build_assistant_message(assistant_message, finish_reason)
                    append_message(_ctx.messages, assistant_msg)
                    for tc in assistant_message.tool_calls:
                        _tc_name = tc.function.name
                        if _tc_name not in agent.valid_tool_names:
                            # See _invalid_tool_name_error_content for the
                            # blank-name anti-priming rationale (#47967).
                            content = _invalid_tool_name_error_content(
                                _tc_name, agent.valid_tool_names
                            )
                        else:
                            content = "Skipped: another tool call in this turn used an invalid name. Please retry this tool call."
                        append_message(_ctx.messages, {
                            "role": "tool",
                            "name": tc.function.name,
                            "tool_call_id": tc.id,
                            "content": content,
                        })
                    _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.INVALID_TOOL)
                    continue
                # Reset retry counter on successful tool call validation
                agent._invalid_tool_retries = 0
                
                # Validate tool call arguments are valid JSON
                # Handle empty strings as empty objects (common model quirk)
                invalid_json_args = []
                for tc in assistant_message.tool_calls:
                    args = tc.function.arguments
                    if isinstance(args, (dict, list)):
                        tc.function.arguments = json.dumps(args)
                        continue
                    if args is not None and not isinstance(args, str):
                        tc.function.arguments = str(args)
                        args = tc.function.arguments
                    # Treat empty/whitespace strings as empty object
                    if not args or not args.strip():
                        tc.function.arguments = "{}"
                        continue
                    try:
                        json.loads(args)
                    except json.JSONDecodeError as e:
                        if (
                            _mixed_invalid_batch
                            and tc.function.name not in agent.valid_tool_names
                        ):
                            # This call never executes — it gets an
                            # invalid-name error result below. Don't let its
                            # broken args trigger the whole-turn JSON retry.
                            continue
                        invalid_json_args.append((tc.function.name, str(e)))
                
                if invalid_json_args:
                    # Check if the invalid JSON is due to truncation rather
                    # than a model formatting mistake.  Routers sometimes
                    # rewrite finish_reason from "length" to "tool_calls",
                    # hiding the truncation from the length handler above.
                    # Detect truncation: args that don't end with } or ]
                    # (after stripping whitespace) are cut off mid-stream.
                    _truncated = any(
                        not (tc.function.arguments or "").rstrip().endswith(("}", "]"))
                        for tc in assistant_message.tool_calls
                        if tc.function.name in {n for n, _ in invalid_json_args}
                    )
                    if _truncated:
                        agent._vprint(
                            f"{agent.log_prefix}⚠️  Truncated tool call arguments detected "
                            f"(finish_reason={finish_reason!r}) — refusing to execute.",
                            force=True,
                        )
                        agent._invalid_json_retries = 0
                        agent._cleanup_task_resources(_ctx.effective_task_id)
                        _final_response = "Response truncated due to output length limit"
                        # Same tool-tail close as interrupt / invalid-tool
                        # exhaustion — this path never reaches finalize_turn.
                        close_interrupted_tool_sequence(_ctx.messages, _final_response)
                        agent._persist_session(_ctx.messages, _ctx.conversation_history)
                        return _complete_direct_turn(_turn_controller, {
                            "final_response": _final_response,
                            "messages": _ctx.messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": _final_response,
                        })

                    # Track retries for invalid JSON arguments
                    agent._invalid_json_retries += 1

                    tool_name, error_msg = invalid_json_args[0]
                    agent._buffer_vprint(f"⚠️  Invalid JSON in tool call arguments for '{tool_name}': {error_msg}")

                    if agent._invalid_json_retries < 3:
                        agent._buffer_vprint(f"🔄 Retrying API call ({agent._invalid_json_retries}/3)...")
                        # Don't add anything to messages, just retry the API call
                        _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.INVALID_ARGUMENTS)
                        continue
                    else:
                        # Instead of returning partial, inject tool error results so the model can recover.
                        # Using tool results (not user messages) preserves role alternation.
                        agent._buffer_vprint("⚠️  Injecting recovery tool results for invalid JSON...")
                        agent._invalid_json_retries = 0  # Reset for next attempt
                        
                        # Append the assistant message with its (broken) tool_calls
                        recovery_assistant = agent._build_assistant_message(assistant_message, finish_reason)
                        append_message(_ctx.messages, recovery_assistant)
                        
                        # Respond with tool error results for each tool call
                        invalid_names = {name for name, _ in invalid_json_args}
                        for tc in assistant_message.tool_calls:
                            if tc.function.name in invalid_names:
                                err = next(e for n, e in invalid_json_args if n == tc.function.name)
                                tool_result = (
                                    f"Error: Invalid JSON arguments. {err}. "
                                    f"For tools with no required parameters, use an empty object: {{}}. "
                                    f"Please retry with valid JSON."
                                )
                            else:
                                tool_result = "Skipped: other tool call in this response had invalid JSON."
                            append_message(_ctx.messages, {
                                "role": "tool",
                                "name": tc.function.name,
                                "tool_call_id": tc.id,
                                "content": tool_result,
                            })
                        _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.INVALID_ARGUMENTS)
                        continue
                
                # Reset retry counter on successful JSON validation
                agent._invalid_json_retries = 0

                # ── Post-call guardrails ──────────────────────────
                assistant_message.tool_calls = agent._cap_delegate_task_calls(
                    assistant_message.tool_calls
                )
                assistant_message.tool_calls = agent._deduplicate_tool_calls(
                    assistant_message.tool_calls
                )

                # Intercept before the assistant tool-call row is appended:
                # a controlled stop must never leave unmatched durable calls.
                # A redirect/interrupt outranks any semantic decision.
                if agent._interrupt_requested:
                    if agent.clear_interrupt(preserve_redirect=True):
                        semantic_progress.reset()
                        _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.REDIRECT)
                        continue
                    interrupted = True
                    _turn_exit_reason = "interrupted_by_user"
                    _turn_controller.move(TransitionKind.FINALIZE, TurnReason.INTERRUPT)
                    break
                _semantic_actions = []
                _semantic_proposals = {}
                _semantic_invalid_only = False
                if agent._tool_guardrail_halt_decision is None:
                    if getattr(agent, "_pending_steer", None):
                        semantic_progress.reset()
                    from agent.tool_executor import semantic_action_signature
                    for tc in assistant_message.tool_calls:
                        _semantic_action = semantic_action_signature(agent, tc)
                        if _semantic_action is None:
                            # Recovery owns this member, but executable siblings
                            # must still participate in semantic convergence.
                            continue
                        _semantic_actions.append(_semantic_action)
                        _semantic_proposals[tc.id] = _semantic_action
                    _semantic_invalid_only = not _semantic_actions
                    if _semantic_invalid_only:
                        # Invalid-only calls reaching the executor still produce
                        # canonical no-effect outcomes. Mixed batches retain the
                        # executable subset and its existing convergence policy.
                        from agent.tool_guardrails import ToolCallSignature
                        for tc in assistant_message.tool_calls:
                            signature = ToolCallSignature.from_call(
                                tc.function.name, {"invalid_arguments": json.loads(tc.function.arguments or "{}")},
                            )
                            _semantic_actions.append(signature)
                            _semantic_proposals[tc.id] = signature
                    _semantic_decision = semantic_progress.before_dispatch(_semantic_actions)
                    if _semantic_decision.action == "halt":
                        _turn_exit_reason = "guardrail_halt"
                        final_response = SEMANTIC_PROGRESS_HALT
                        append_message(_ctx.messages, {"role": "assistant", "content": final_response})
                        logger.warning(
                            "semantic progress: action=halt stalled_rounds=%d cycle=%d unique_actions=%d tool_count=%d",
                            _semantic_decision.stalled_rounds, _semantic_decision.cycle,
                            _semantic_decision.unique_actions, _semantic_decision.tool_count,
                        )
                        agent._safe_print(f"\n{final_response}\n")
                        if agent.stream_delta_callback:
                            try:
                                agent.stream_delta_callback(final_response)
                                agent.stream_delta_callback(None)
                            except Exception:
                                pass
                        _turn_controller.move(TransitionKind.FINALIZE, TurnReason.GUARDRAIL)
                        break

                # Mixed-batch invalid-name handling: collect the invalid
                # calls now so the assistant message (built below) keeps
                # EVERY call the model emitted — providers require each
                # tool_call to have a matching tool result and vice versa —
                # while only the valid subset is dispatched for execution.
                _invalid_batch_calls = []
                if _mixed_invalid_batch:
                    _invalid_batch_calls = [
                        tc for tc in assistant_message.tool_calls
                        if tc.function.name not in agent.valid_tool_names
                    ]

                assistant_msg = agent._build_assistant_message(assistant_message, finish_reason)

                turn_content = assistant_message.content or ""

                # Some local tool-call templates emit a bare bracketed token
                # (for example ``[memory]``) as assistant content alongside a
                # function call. It is protocol scaffolding, not an answer.
                # Persisting or caching it as visible content lets the empty
                # post-tool fallback replay that token forever after compaction (#78148).
                if (
                    assistant_message.tool_calls
                    and _STALE_MARKER_RE.fullmatch(turn_content.strip())
                ):
                    logger.warning(
                        "Discarding bare tool-call marker from assistant content: %s",
                        turn_content,
                    )
                    turn_content = ""
                    assistant_msg["content"] = ""

                # Classify tools in this turn to determine if they are all housekeeping.
                # This classification is needed regardless of whether the turn has visible content,
                # because a substantive tool-only turn must invalidate any older housekeeping fallback.
                _HOUSEKEEPING_TOOLS = frozenset({
                    "memory", "todo", "skill_manage", "session_search",
                })
                _all_housekeeping = all(
                    tc.function.name in _HOUSEKEEPING_TOOLS
                    for tc in assistant_message.tool_calls
                )

                # If this turn has substantive tools (non-housekeeping), clear any older fallback.
                # Prevents a two-turn-old housekeeping narration from being treated as if it belonged
                # to the immediately preceding substantive tool turn.
                if assistant_message.tool_calls and not _all_housekeeping:
                    agent._last_content_with_tools = None
                    agent._last_content_tools_all_housekeeping = False
                    # Also clear the mute flag: a prior housekeeping turn may
                    # have set _mute_post_response (line ~4667), and the
                    # substantive tools in THIS turn should produce visible
                    # progress output. Without this reset, _vprint suppresses
                    # tool progress until the no-tool-call branch clears it at
                    # line ~4834 — after all tools have finished.
                    agent._mute_post_response = False

                # If this turn has both content AND tool_calls, capture the content
                # as a fallback final response. Common pattern: model delivers its
                # answer and calls memory/skill tools as a side-effect in the same
                # turn. If the follow-up turn after tools is empty, we use this.
                if turn_content and agent._has_content_after_think_block(turn_content):
                    agent._last_content_with_tools = turn_content
                    # Only mute subsequent output when EVERY tool call in
                    # this turn is post-response housekeeping (memory, todo,
                    # skill_manage, etc.).  If any substantive tool is present
                    # (search_files, read_file, write_file, terminal, ...),
                    # keep output visible so the user sees progress.
                    agent._last_content_tools_all_housekeeping = _all_housekeeping
                    if _all_housekeeping and agent._has_stream_consumers():
                        agent._mute_post_response = True
                    elif agent._should_emit_quiet_tool_messages():
                        clean = agent._strip_think_blocks(turn_content).strip()
                        if clean:
                            agent._vprint(f"  ┊ 💬 {clean}")
                
                # Pop thinking-only prefill message(s) before appending
                # (tool-call path — same rationale as the final-response path).
                _had_prefill = False
                while (
                    _ctx.messages
                    and isinstance(_ctx.messages[-1], dict)
                    and _ctx.messages[-1].get("_thinking_prefill")
                ):
                    _ctx.messages.pop()
                    _had_prefill = True

                # Reset prefill counter when tool calls follow a prefill
                # recovery.  Without this, the counter accumulates across
                # the whole conversation — a model that intermittently
                # empties (empty → prefill → tools → empty → prefill →
                # tools) burns both prefill attempts and the third empty
                # gets zero recovery.  Resetting here treats each tool-
                # call success as a fresh start.
                if _had_prefill:
                    agent._thinking_prefill_retries = 0
                    agent._empty_content_retries = 0
                # Successful tool execution — reset the post-tool nudge
                # flag so it can fire again if the model goes empty on
                # a LATER tool round.
                agent._post_tool_empty_retried = False
                # A landed tool call means any earlier dropped-tool-call stall
                # was recovered — refresh that budget too so it guards each
                # stall independently rather than capping the whole run.
                agent._dropped_toolcall_retries = 0

                previous_msg = _ctx.messages[-1] if _ctx.messages else None
                current_interim_visible = agent._interim_assistant_visible_text(assistant_msg)
                previous_interim_visible = (
                    agent._interim_assistant_visible_text(previous_msg)
                    if isinstance(previous_msg, dict)
                    else ""
                )
                duplicate_previous_interim = (
                    bool(current_interim_visible)
                    and isinstance(previous_msg, dict)
                    and previous_msg.get("role") == "assistant"
                    and previous_msg.get("finish_reason") == "incomplete"
                    and previous_interim_visible == current_interim_visible
                )
                append_message(_ctx.messages, assistant_msg)

                # Mixed batch: error-result the invalid calls and strip them
                # from the execution set. The assistant message above keeps
                # all calls (each gets a matching tool result — the invalid
                # ones get theirs here, the valid ones during execution), so
                # provider-side tool_call/result pairing stays intact.
                if _invalid_batch_calls:
                    for tc in _invalid_batch_calls:
                        append_message(_ctx.messages, {
                            "role": "tool",
                            "name": tc.function.name,
                            "tool_call_id": tc.id,
                            "content": _invalid_tool_name_error_content(
                                tc.function.name, agent.valid_tool_names
                            ),
                        })
                    assistant_message.tool_calls = [
                        tc for tc in assistant_message.tool_calls
                        if tc.function.name in agent.valid_tool_names
                    ]

                _turn_controller.move(
                    TransitionKind.TOOL_ROUND, TurnReason.TOOLS,
                    durability=DurabilityBoundary.TOOL_CALLS,
                )
                _tool_turn_persisted = None
                try:
                    # Persist the assistant tool-call turn before any tool
                    # side effects run. If a destructive tool restarts or
                    # terminates Hermes mid-turn, resume logic still sees the
                    # exact tool-call block that already executed.
                    _tool_turn_persisted = agent._flush_messages_to_session_db(
                        _ctx.messages, _ctx.conversation_history
                    )
                except Exception as exc:
                    _tool_turn_persisted = False
                    from hermes_state import classify_persistence_error
                    agent._last_persistence_error_cause = (
                        classify_persistence_error(exc)
                    )
                    logger.warning(
                        "Incremental tool-call persistence failed before execution "
                        "(session=%s): %s",
                        agent.session_id or "none",
                        exc,
                    )

                if _tool_turn_persisted is False:
                    # The canonical append failed. Do not project the row or
                    # run side-effecting tools from state that exists only in
                    # this process. Breaking also avoids retrying the same
                    # unpersisted turn until the iteration budget is exhausted.
                    # The flush may have classified the cause internally; if
                    # nothing was recorded, the cause is genuinely unknown.
                    if getattr(agent, "_last_persistence_error_cause", None) is None:
                        agent._last_persistence_error_cause = "unknown"
                    _turn_exit_reason = "session_persistence_failed"
                    final_response = ""
                    failed = True
                    _turn_controller.move(TransitionKind.FINALIZE, TurnReason.PERSISTENCE_FAILURE)
                    break

                # A UI must never observe an assistant/tool-call row that is
                # still only an ephemeral in-memory projection. Emit interim
                # commentary only after the canonical SessionDB append above.
                if not duplicate_previous_interim:
                    agent._emit_interim_assistant_message(assistant_msg)

                # Close any open streaming display (response box, reasoning
                # box) before tool execution begins.  Intermediate turns may
                # have streamed early content that opened the response box;
                # flushing here prevents it from wrapping tool feed lines.
                # Only signal the display callback — TTS (_stream_callback)
                # should NOT receive None (it uses None as end-of-stream).
                if agent.stream_delta_callback:
                    try:
                        agent.stream_delta_callback(None)
                    except Exception:
                        pass

                agent._tool_guardrails.start_semantic_round(_semantic_proposals, no_effect=_semantic_invalid_only)
                try:
                    agent._execute_tool_calls(assistant_message, _ctx.messages, _ctx.effective_task_id, api_call_count)
                finally:
                    _semantic_round_discarded = not agent._tool_guardrails.semantic_round_active
                    _semantic_results = agent._tool_guardrails.take_semantic_round()

                _tool_completion = resolve_tool_completion(
                    persistence_failed=bool(getattr(agent, "_incremental_persistence_failed", False)),
                    guardrail_halted=agent._tool_guardrail_halt_decision is not None,
                )
                if _tool_completion is TurnReason.PERSISTENCE_FAILURE:
                    # A tool result could not be made canonical. Do not send
                    # the in-memory result back to the model or project any
                    # later events from this turn.
                    _turn_exit_reason = "session_persistence_failed"
                    final_response = ""
                    failed = True
                    _turn_controller.move(TransitionKind.FINALIZE, TurnReason.PERSISTENCE_FAILURE)
                    break

                if _tool_completion is TurnReason.GUARDRAIL:
                    decision = agent._tool_guardrail_halt_decision
                    _turn_exit_reason = "guardrail_halt"
                    final_response = agent._toolguard_controlled_halt_response(decision)
                    agent._emit_status(
                        f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}"
                    )
                    append_message(_ctx.messages, {"role": "assistant", "content": final_response})
                    # Emit the halt message to the client so it's not
                    # indistinguishable from a crash.  The stream display
                    # was flushed (callback(None)) before tool execution,
                    # but the callback is still alive — fire the text
                    # through it so SSE/TUI clients see the explanation.
                    if final_response:
                        agent._safe_print(f"\n{final_response}\n")
                        if agent.stream_delta_callback:
                            try:
                                agent.stream_delta_callback(final_response)
                                agent.stream_delta_callback(None)
                            except Exception:
                                pass
                    _turn_controller.move(TransitionKind.FINALIZE, TurnReason.GUARDRAIL)
                    break

                # Reset per-turn retry counters after successful tool
                # execution so a single truncation doesn't poison the
                # entire conversation.
                # The executor correlates outcomes by call ID, not argument
                # equality: middleware may rewrite args or block a valid call.
                if (
                    not _semantic_round_discarded
                    and not agent._interrupt_requested
                    and not agent._has_pending_redirect()
                    and len(_semantic_results) == len(_semantic_actions)
                ):
                    if _semantic_actions:
                        semantic_progress.observe(SemanticRoundObservation.from_executions(_semantic_results))
                elif _semantic_round_discarded or agent._interrupt_requested or agent._has_pending_redirect():
                    # A real user redirect rebases the episode. Missing worker
                    # evidence alone must never restore a fresh loop budget.
                    semantic_progress.reset()
                _continuation.truncated_tool_retries = 0

                # Signal that a paragraph break is needed before the next
                # streamed text.  We don't emit it immediately because
                # multiple consecutive tool iterations would stack up
                # redundant blank lines.  Instead, _fire_stream_delta()
                # will prepend a single "\n\n" the next time real text
                # arrives.
                agent._stream_needs_break = True

                # Refund the iteration if the ONLY tool(s) called were
                # execute_code (programmatic tool calling).  These are
                # cheap RPC-style calls that shouldn't eat the budget.
                _tc_names = {tc.function.name for tc in assistant_message.tool_calls}
                if _tc_names == {"execute_code"}:
                    agent.iteration_budget.refund()
                
                # Use real token counts from the API response to decide
                # compression.  prompt_tokens + completion_tokens is the
                # actual context size the provider reported plus the
                # assistant turn — a tight lower bound for the next prompt.
                # Tool results appended above aren't counted yet, but the
                # threshold (default 50%) leaves ample headroom; if tool
                # results push past it, the next API call will report the
                # real total and trigger compression then.
                #
                # If last_prompt_tokens is 0 (stale after API disconnect
                # or provider returned no usage data), fall back to rough
                # estimate to avoid missing compression.  Without this,
                # a session can grow unbounded after disconnects because
                # should_compress(0) never fires.  (#2153)
                _compressor = agent.context_compressor
                if _compressor.last_prompt_tokens > 0:
                    # Only use prompt_tokens — completion/reasoning
                    # tokens don't consume context window space.
                    # Thinking models (GLM-5.1, QwQ, DeepSeek R1)
                    # inflate completion_tokens with reasoning,
                    # causing premature compression.  (#12026)
                    _real_tokens = _compressor.last_prompt_tokens
                elif _compressor.last_prompt_tokens == -1:
                    # Compression just ran and no API-reported prompt count
                    # has arrived yet. Avoid treating a schema-heavy rough
                    # post-compression estimate as real context pressure.
                    _real_tokens = 0
                else:
                    # Include tool schemas — with 50+ tools enabled
                    # these add 20-30K tokens the messages-only
                    # estimate misses, which can skip compression
                    # past the configured threshold (#14695).
                    _real_tokens = estimate_request_tokens_rough(
                        _ctx.messages, tools=agent.tools or None
                    )

                if (
                    agent.compression_enabled
                    and _recovery.compression_attempts < _recovery.max_compression_attempts
                    and _compressor.should_compress(_real_tokens)
                ):
                    _recovery.compression_attempts += 1
                    # Compression is actually running (block cleared / was
                    # never blocked) — reset the blocked-overflow warning
                    # dedup so a future blocked-over-threshold turn can warn
                    # again (silent-overflow fix #62625).
                    # getattr guard: test doubles built via object.__new__ lack the
                    # method (gateway test-double pitfall) — treat absence as no-op.
                    _clear_warn = getattr(agent, "_clear_context_overflow_warn", None)
                    if callable(_clear_warn):
                        _clear_warn()
                    agent._safe_print("  ⟳ compacting context…")
                    _post_tool_input = _ctx.messages
                    # Route the overhead-aware _real_tokens (computed above) into compression, not
                    # the bare last_prompt_tokens — which is 0 in the no-usage fallback, hiding the
                    # true request size from the engine's overflow guard (upstream PR #77169 review).
                    _ctx.messages, _ctx.active_system_prompt = agent._compress_context(
                        _ctx.messages, system_message,
                        approx_tokens=_real_tokens,
                        task_id=_ctx.effective_task_id,
                    )
                    if (
                        _ctx.messages is _post_tool_input
                        and compression_skipped_due_to_lock(agent)
                    ):
                        # #69870 lock-skip: this pass no-oped because another
                        # path holds the session's compression lock — a
                        # temporary defer, not evidence about compressibility.
                        # Refund the attempt so a lock-loser tool loop does not
                        # burn the shared per-turn budget toward
                        # compression_exhausted (#9893/#35809).
                        _recovery.compression_attempts -= 1
                    else:
                        _ctx.conversation_history = conversation_history_after_compression(
                            agent, _ctx.messages, _ctx.conversation_history
                        )
                        if _should_skip_model_call_for_reference_handoff(
                            _ctx.messages, _ctx.user_message
                        ):
                            logger.info(
                                "Skipping post-tool compaction model call: "
                                "reference-only handoff would be the sole "
                                "active user turn (#80622)"
                            )
                            if not final_response:
                                final_response = _HANDOFF_SKIP_FINAL_RESPONSE
                            _turn_exit_reason = "compaction_handoff_not_actionable"
                            _turn_controller.move(TransitionKind.FINALIZE, TurnReason.HANDOFF)
                            break
                elif agent.compression_enabled:
                    # Over threshold but compression is blocked (summary-LLM
                    # cooldown or anti-thrashing). Surface a deduped warning so
                    # the user isn't left with a silently growing context that
                    # eventually hits the hard provider limit. Mirrors the
                    # turn-context preflight guard (silent-overflow fix #62625).
                    _block_reason = None
                    _info = getattr(_compressor, "should_compress_info", None)
                    if _info is not None:
                        try:
                            _block_reason = _info(_real_tokens)[1]
                        except Exception:
                            _block_reason = None
                    if _block_reason:
                        agent._warn_context_overflow_blocked(
                            _block_reason,
                            _real_tokens,
                            int(getattr(_compressor, "threshold_tokens", 0) or 0),
                        )
                    # Proactive tool-result prune: reclaim re-sent history on
                    # large-window models long before should_compress() (≈50% of
                    # the window) would ever fire. Deterministic, no LLM call;
                    # protects the recent tail. No-op unless proactive_prune_tokens
                    # is configured and _real_tokens is above it — and even then
                    # the prune only commits when it reclaims at least
                    # proactive_prune_min_reclaim_tokens, so prompt-cache breaks
                    # stay episodic like compression's (the one sanctioned cache
                    # break) instead of firing every tool iteration. See
                    # ContextCompressor.prune_tool_results_only.
                    # getattr guard: plugin context engines predating the hook and
                    # minimal test doubles (SimpleNamespace compressors) lack the
                    # method — treat absence as a no-op.
                    _prune = getattr(_compressor, "prune_tool_results_only", None)
                    if callable(_prune):
                        try:
                            _pruned_msgs, _pruned_n = _prune(
                                _ctx.messages, current_tokens=_real_tokens
                            )
                        except Exception:
                            logger.debug(
                                "proactive tool-result prune failed; skipping",
                                exc_info=True,
                            )
                            _pruned_msgs, _pruned_n = _ctx.messages, 0
                        # Standard no-op caller contract: only commit when the
                        # engine returned a NEW list object with a non-zero count.
                        if _pruned_n and _pruned_msgs is not _ctx.messages:
                            # Do NOT rebuild conversation_history here. The compressor
                            # atomically rewrites the active transcript with the durable
                            # rearm threshold, then stamps every returned row with
                            # _DB_PERSISTED_MARKER, so the marker-based flush dedup (see
                            # _flush_messages_to_session_db) prevents duplicate writes.
                            # Calling
                            # conversation_history_after_compression (a compaction-only
                            # helper keyed on the _last_compaction_in_place flag) would be
                            # a no-op at best, and on a stale in-place flag could seed
                            # this turn's fresh, not-yet-persisted rows into history_ids
                            # and skip writing them.
                            _ctx.messages = _pruned_msgs
                
                # Save session log incrementally (so progress is visible even if interrupted)
                agent._session_messages = _ctx.messages
                
                # Touch activity before continuing so the gateway's
                # inactivity monitor never sees a stale timestamp
                # between tool completion and the start of the next
                # API call.  Without this, a tool-call result (which
                # takes ~0s to process) followed by slow post-tool
                # processing (compression, persist) and a slow
                # follow-up API call can exceed the gateway inactivity
                # timeout (HERMES_AGENT_TIMEOUT, default 1800s) and the
                # gateway kills the session before the next activity
                # touch fires (#69559, #69131).
                agent._touch_activity(f"tool results posted, continuing iteration #{api_call_count}")
                # Continue loop for next response
                _turn_controller.move(
                    TransitionKind.NEXT_STEP, TurnReason.TOOLS_COMPLETED,
                    budget=BudgetEffect.REFUND_ITERATION if _tc_names == {"execute_code"} else BudgetEffect.KEEP,
                    durability=DurabilityBoundary.TOOL_RESULTS,
                )
                continue
            
            else:
                # No tool calls - this is the final response.
                # (Dropped tool-call recovery — finish_reason=="tool_calls" with
                # an empty tool_calls array — is handled at the finalization
                # chokepoint below, after final_msg is built, so it catches
                # every path that reaches turn finalization, not just this one.)
                _text_outcome = handle_text_response(
                    agent, _ctx=_ctx, _continuation=_continuation,
                    _turn_controller=_turn_controller,
                    assistant_message=assistant_message, finish_reason=finish_reason,
                    response=response, api_messages=api_messages,
                    api_call_count=api_call_count,
                )
                if isinstance(_text_outcome, dict):
                    return _text_outcome
                if isinstance(_text_outcome, TurnTransition):
                    continue
                final_response = _text_outcome.final_response
                _turn_exit_reason = _text_outcome.exit_reason
                break
            
        except Exception as e:
            # Phase-aware error classification. The huge outer try/except spans
            # both the actual API request and all local post-processing of the
            # returned assistant message. Deterministic local bugs (e.g.
            # passing a multimodal content list into a regex helper after a
            # vision turn or context compaction) should not be retried: they
            # will fail identically on every iteration and only burn the
            # iteration budget. We classify an error as local by inspecting the
            # traceback: if the exception propagated through any of the known
            # local post-processing helpers and never entered the interruptible
            # API-call helpers, it is almost certainly a local processing bug.
            # (#66267)
            tb_module_names: set[str] = set()
            _tb = e.__traceback__
            while _tb is not None:
                _fname = os.path.splitext(os.path.basename(_tb.tb_frame.f_code.co_filename))[0]
                tb_module_names.add(_fname)
                _tb = _tb.tb_next

            _hit_local = bool(tb_module_names & _LOCAL_PROCESSING_MODULES)
            _hit_api = bool(tb_module_names & _API_CALL_MODULES)

            _is_local_processing_error = _hit_local and not _hit_api

            if _is_local_processing_error:
                error_msg = (
                    f"Error during local message processing after "
                    f"OpenAI-compatible API call #{api_call_count}: {str(e)}"
                )
            else:
                error_msg = f"Error during OpenAI-compatible API call #{api_call_count}: {str(e)}"
            try:
                print(f"❌ {error_msg}")
            except (OSError, ValueError):
                logger.error(error_msg)

            # Emit the full traceback at ERROR level so it lands in both
            # agent.log AND errors.log.  Previously this was logged at DEBUG,
            # which meant intermittent outer-loop failures were unreproducible
            # — users would see a one-line summary on screen with no way to
            # recover the call site.  logger.exception() includes the
            # traceback automatically and emits at ERROR.
            logger.exception("Outer loop error in API call #%d", api_call_count)
            
            # If an assistant message with tool_calls was already appended,
            # the API expects a role="tool" result for every tool_call_id.
            # Fill in error results for any that weren't answered yet.
            for idx in range(len(_ctx.messages) - 1, -1, -1):
                msg = _ctx.messages[idx]
                if not isinstance(msg, dict):
                    break
                if msg.get("role") == "tool":
                    continue
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    answered_ids = {
                        m["tool_call_id"]
                        for m in _ctx.messages[idx + 1:]
                        if isinstance(m, dict) and m.get("role") == "tool"
                    }
                    for tc in msg["tool_calls"]:
                        if not tc or not isinstance(tc, dict): continue
                        if tc["id"] not in answered_ids:
                            err_msg = {
                                "role": "tool",
                                "name": _ra().AIAgent._get_tool_call_name_static(tc),
                                "tool_call_id": tc["id"],
                                "content": f"Error executing tool: {error_msg}",
                            }
                            append_message(_ctx.messages, err_msg)
                break
            
            # Non-tool errors don't need a synthetic message injected.
            # The error is already printed to the user (line above), and
            # the retry loop continues.  Injecting a fake user/assistant
            # message pollutes history, burns tokens, and risks violating
            # role-alternation invariants.

            # If we're near the limit, break to avoid infinite loops.
            # Local processing errors are deterministic — stop immediately
            # rather than retrying until the budget is exhausted.
            if (
                _is_local_processing_error
                or api_call_count >= agent.max_iterations - 1
            ):
                if _is_local_processing_error:
                    _turn_exit_reason = f"local_processing_error({error_msg[:80]})"
                    final_response = f"I apologize, but I encountered an error while processing the model response: {error_msg}"
                else:
                    _turn_exit_reason = f"error_near_max_iterations({error_msg[:80]})"
                    final_response = f"I apologize, but I encountered repeated errors: {error_msg}"
                # Append as assistant so the history stays valid for
                # session resume (avoids consecutive user messages).
                append_message(_ctx.messages, {"role": "assistant", "content": final_response})
                _turn_controller.move(TransitionKind.FINALIZE, TurnReason.PROCESSING_ERROR)
                break
            _turn_controller.move(TransitionKind.NEXT_STEP, TurnReason.PROCESSING_ERROR)
    
    # Post-loop turn finalization extracted to agent/turn_finalizer.finalize_turn
    if _turn_controller.phase is not TurnPhase.FINALIZE:
        _turn_controller.move(TransitionKind.FINALIZE, TurnReason.BUDGET)
    # (god-file decomposition Phase 1 step 4). Behavior-neutral: the assembled
    # result dict is returned exactly as before.
    return _complete_finalized_turn(_turn_controller, finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=interrupted,
        failed=failed,
        messages=_ctx.messages,
        conversation_history=_ctx.conversation_history,
        effective_task_id=_ctx.effective_task_id,
        turn_id=_ctx.turn_id,
        user_message=_ctx.user_message,
        original_user_message=_ctx.original_user_message,
        _should_review_memory=_should_review_memory,
        _turn_exit_reason=_turn_exit_reason,
        _pending_verification_response=_continuation.pending_answer,
        _pending_verification_response_previewed=_continuation.pending_previewed,
    ))



__all__ = ["run_conversation"]

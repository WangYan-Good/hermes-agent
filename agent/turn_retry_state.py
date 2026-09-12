"""One-shot recovery guards for a prepared request cycle.

Auth, payload repair, credential rotation, and primary transport refresh keep
independent guards. They reset when a logical request is rebuilt, not on each
network attempt. Logical restarts belong to RequestCycle and TurnTransition.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass
class TurnRetryState:
    """Attempt recovery bookkeeping; contains no logical restart signals."""

    # ── Per-provider OAuth / credential refresh guards ───────────────────
    codex_auth_retry_attempted: bool = False
    anthropic_auth_retry_attempted: bool = False
    nous_auth_retry_attempted: bool = False
    nous_paid_entitlement_refresh_attempted: bool = False
    copilot_auth_retry_attempted: bool = False
    # Copilot surfaces a stale/degraded credential as a 400
    # ``model_not_available_for_integrator`` / ``model_not_supported`` instead
    # of a clean 401 (e.g. a raw OAuth token seeded when the token exchange
    # degraded at startup, routing the request to the restricted
    # ``copilot-language-server`` integrator). Guard a single-shot forced
    # re-exchange + client rebuild for that case, separate from the 401 guard
    # so both can fire within one attempt if needed.
    copilot_stale_cred_retry_attempted: bool = False
    vertex_auth_retry_attempted: bool = False

    # ── Format / payload recovery guards ─────────────────────────────────
    thinking_sig_retry_attempted: bool = False
    invalid_encrypted_content_retry_attempted: bool = False
    native_compaction_reject_retry_attempted: bool = False
    image_shrink_retry_attempted: bool = False
    multimodal_tool_content_retry_attempted: bool = False
    oauth_1m_beta_retry_attempted: bool = False
    llama_cpp_grammar_retry_attempted: bool = False

    # ── Transport / rate-limit recovery ──────────────────────────────────
    primary_recovery_attempted: bool = False
    has_retried_429: bool = False

    # ── Auth-failure provider failover ───────────────────────────────────
    # Set once we've escalated a persistent 401/403 (after the per-provider
    # credential-refresh attempt above failed) to the fallback chain, so we
    # don't loop on the same auth failover within one attempt.
    auth_failover_attempted: bool = False

    def __iter__(self):
        # Convenience for debugging / tests: iterate (name, value) pairs.
        for f in fields(self):
            yield f.name, getattr(self, f.name)

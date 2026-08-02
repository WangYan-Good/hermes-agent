"""Dashboard-side logic for ``providers.<id>.proxy``.

Pure functions over a config dict, plus one network probe, kept out of
``web_server.py`` so the rules can be tested without an HTTP client. The routes
there stay thin: token check, profile scope, load/save, error mapping.

SECURITY: proxy URLs carry credentials as routinely as ``extra_headers`` do.
Every value leaving this module for a browser or a log line goes through
``utils.redact_proxy_url`` first.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from utils import redact_proxy_url

# The three configured states, spelled the same way on the wire, in Python and
# in TypeScript: key absent / ``false`` / a URL string.
PROXY_MODES: tuple[str, ...] = ("inherit", "direct", "url")

# The dashboard must not hang on an unreachable proxy.
_PROBE_CONNECT_TIMEOUT = 5.0
_PROBE_TOTAL_TIMEOUT = 10.0


def _providers_block(config: Dict[str, Any]) -> Dict[str, Any]:
    providers = config.get("providers") if isinstance(config, dict) else None
    return providers if isinstance(providers, dict) else {}


def provider_proxy_editable(config: Dict[str, Any], provider_id: str) -> bool:
    """True when *provider_id* is a key we may write into ``providers:``.

    Without this guard the routes reduce to "write an arbitrary key into
    config.yaml". Accepts a built-in provider or a key the operator already
    created; notably not the synthetic ``claude-code`` catalog row, which owns
    no config entry and no inference host.
    """
    key = str(provider_id or "").strip()
    if not key:
        return False
    if key in _providers_block(config):
        return True
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        return False
    return key in PROVIDER_REGISTRY


def is_redacted_proxy_url(value: Any) -> bool:
    """True when *value* is the ``scheme://***@host`` form we hand the browser.

    Submitting it back unchanged would persist ``***`` as the username, so the
    write path rejects it and asks for the full address instead.
    """
    candidate = str(value or "").strip()
    if not candidate:
        return False
    try:
        netloc = urlparse(candidate).netloc
    except ValueError:
        return False
    if "@" not in netloc:
        return False
    return netloc.rsplit("@", 1)[0] == "***"


def read_provider_proxy_state(
    config: Dict[str, Any], provider_id: str
) -> Optional[Dict[str, Any]]:
    """What *provider_id* itself declares, for display in the dashboard.

    Returns ``None`` when the id has no editable config key.

    This is deliberately NOT ``resolve_provider_proxy``. That answers "which
    proxy should this request use" and, for a provider declaring nothing, falls
    through to a base_url reverse lookup -- so the UI would show a neighbouring
    provider's proxy as if this provider had configured it. Same words,
    different question.
    """
    if not provider_proxy_editable(config, provider_id):
        return None
    entry = _providers_block(config).get(str(provider_id).strip())
    if not isinstance(entry, dict) or "proxy" not in entry:
        return {"mode": "inherit", "url": None}

    from hermes_cli.config import _coerce_provider_proxy

    try:
        coerced = _coerce_provider_proxy(entry["proxy"], str(provider_id))
    except ValueError:
        # A hand-edited `proxy: true` must not take down the whole card. Show
        # it as a URL needing re-entry rather than raising through the route.
        return {"mode": "url", "url": None, "invalid": True}
    if coerced is None:
        return {"mode": "inherit", "url": None}
    if coerced is False:
        return {"mode": "direct", "url": None}
    return {"mode": "url", "url": redact_proxy_url(coerced)}


def _normalize_submitted_url(url: Any, provider_id: str) -> Any:
    """Validate a URL submitted for ``mode="url"``. Raises ``ValueError``.

    Returns whatever ``_coerce_provider_proxy`` makes of it -- a normalized URL
    string, or ``False`` when the operator typed a direct sentinel
    (``direct`` / ``none``) into the box. Reusing the resolver's coercion is
    what makes the dashboard accept exactly the values the agent accepts, with
    the same already-credential-safe error messages.
    """
    candidate = str(url or "").strip()
    if not candidate:
        raise ValueError(
            "A proxy URL is required. Choose 'force direct' or 'follow the "
            "environment variables' instead of leaving the address empty."
        )
    if is_redacted_proxy_url(candidate):
        raise ValueError(
            "That address is the masked form shown for display, not a usable "
            "proxy. Enter the full URL, including any credentials."
        )
    from hermes_cli.config import _coerce_provider_proxy

    return _coerce_provider_proxy(candidate, str(provider_id))


def apply_provider_proxy(
    config: Dict[str, Any],
    provider_id: str,
    mode: str,
    url: Any = None,
) -> Dict[str, Any]:
    """Apply a proxy setting to *config* in place; return the resulting state.

    Raises ``ValueError`` for an unknown provider, an unknown mode, or a
    malformed URL -- never silently downgrades a bad URL to a direct
    connection, because traffic the operator believes is proxied going direct
    is the failure this whole feature exists to prevent.

    ``url`` is read only when *mode* is ``"url"``; the other modes ignore it
    rather than rejecting it, so the frontend can keep a half-typed address in
    the box while the user flips the select.
    """
    key = str(provider_id or "").strip()
    if not provider_proxy_editable(config, key):
        raise ValueError(f"Unknown provider: {provider_id!r}.")
    if mode not in PROXY_MODES:
        raise ValueError(
            f"Unknown proxy mode {mode!r}. Expected one of: {', '.join(PROXY_MODES)}."
        )

    providers = dict(_providers_block(config))
    existing = providers.get(key)
    # Merge, never rebuild. A providers.<name> block is not owned by this
    # editor: it can carry hand-written keys the dashboard has no field for --
    # api_mode, key_env, extra_headers (which themselves carry credentials),
    # request_overrides -- and rebuilding from scratch silently dropped every
    # one of them, leaving a provider that no longer authenticated. Same
    # lesson, same fix as _write_custom_endpoint.
    entry: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}

    if mode == "inherit":
        entry.pop("proxy", None)
        if entry:
            providers[key] = entry
        else:
            # Nothing else was configured here, so don't leave an
            # `anthropic: {}` shell behind. An entry that still carries a
            # base_url/model (a user-defined endpoint) keeps its `if entry`
            # branch above and survives untouched.
            providers.pop(key, None)
    elif mode == "direct":
        entry["proxy"] = False
        providers[key] = entry
    else:
        entry["proxy"] = _normalize_submitted_url(url, key)
        providers[key] = entry

    if providers or isinstance(config.get("providers"), dict):
        config["providers"] = providers
    state = read_provider_proxy_state(config, key)
    # A custom endpoint whose only key was `proxy` is gone after an "inherit"
    # write, so it is no longer editable and reads as None. It inherits.
    return state if state is not None else {"mode": "inherit", "url": None}


def provider_probe_url(provider_id: str) -> str:
    """The URL the reachability probe dials for *provider_id*.

    Derived from the registry, never from the request -- otherwise the test
    route becomes "make the server fetch an arbitrary URL".
    """
    key = str(provider_id or "").strip()
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception as exc:  # pragma: no cover - import failure is fatal anyway
        raise ValueError(f"Provider registry unavailable: {exc}") from exc
    entry = PROVIDER_REGISTRY.get(key)
    base = str(getattr(entry, "inference_base_url", "") or "").strip()
    if not base:
        raise ValueError(
            f"No known inference host for {provider_id!r}, so there is nothing "
            "to test a proxy against."
        )
    return base.rstrip("/") + "/models"


def proxy_httpx_kwargs_for_mode(
    mode: str, url: Any = None, provider_id: str = ""
) -> Dict[str, Any]:
    """httpx kwargs for a *pending* mode, before anything is saved.

    Mirrors ``config.provider_proxy_httpx_kwargs`` exactly, but reads the
    submitted value instead of the config: ``{}`` leaves httpx's ``trust_env``
    default alone (the environment variables decide), and forced-direct needs
    ``trust_env=False`` rather than ``proxy=None`` because httpx resolves env
    proxies through ``urllib.request.getproxies()``, which on macOS also
    reports system proxy settings.
    """
    if mode not in PROXY_MODES:
        raise ValueError(
            f"Unknown proxy mode {mode!r}. Expected one of: {', '.join(PROXY_MODES)}."
        )
    if mode == "inherit":
        return {}
    if mode == "direct":
        return {"trust_env": False}
    normalized = _normalize_submitted_url(url, provider_id)
    if normalized is False:
        return {"trust_env": False}
    return {"proxy": str(normalized)}


def _safe_detail(text: Any, proxy_url: Any) -> str:
    """Scrub a submitted proxy URL out of a message before it is shown.

    httpx routinely embeds the proxy URL it failed to reach in the exception
    text, credentials and all.
    """
    detail = str(text)
    raw = str(proxy_url or "")
    if raw:
        detail = detail.replace(raw, redact_proxy_url(raw))
    return detail


def probe_provider_proxy(
    provider_id: str, mode: str, url: Any = None
) -> Dict[str, Any]:
    """Dial the provider's inference host exactly as *mode* would at runtime.

    Classified by ``kind``, not a bare boolean:

    * ``transport_error`` -- proxy down, wrong port, wrong scheme.
    * ``reachable`` -- HTTP 200 or 401. 401 is the expected "arrived, sent no
      credentials"; the probe authenticates nothing, it measures reachability.
    * ``http`` -- answered with something else; judge the code yourself.
      ``api.anthropic.com`` answers 403 on a direct connection from a blocked
      region, and "any response means success" would paint that green.

    Raises ``ValueError`` for a bad mode/URL or a provider with no probe host.
    """
    import httpx

    target = provider_probe_url(provider_id)
    kwargs = proxy_httpx_kwargs_for_mode(mode, url, provider_id)
    submitted = kwargs.get("proxy")
    timeout = httpx.Timeout(_PROBE_TOTAL_TIMEOUT, connect=_PROBE_CONNECT_TIMEOUT)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, **kwargs) as client:
            response = client.get(target)
    except httpx.RequestError as exc:
        return {
            "kind": "transport_error",
            "ok": False,
            "status": None,
            "target": target,
            "detail": _safe_detail(f"{type(exc).__name__}: {exc}", submitted),
        }
    status = int(response.status_code)
    kind = "reachable" if status in (200, 401) else "http"
    return {
        "kind": kind,
        "ok": kind == "reachable",
        "status": status,
        "target": target,
        "detail": f"HTTP {status}",
    }

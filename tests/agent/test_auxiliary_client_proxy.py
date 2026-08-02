"""Auxiliary clients resolve their proxy by base_url, not by provider name.

Auxiliary calls (compression, vision, web_extract, title generation, …) never
learn a provider name — ``_openai_http_client_kwargs`` holds only a URL, and the
auxiliary model may sit on a different provider than the primary one. Without
the reverse lookup, a proxied provider works for chat and fails on every
auxiliary task.
"""

import httpx
import pytest

from agent import auxiliary_client
from agent.process_bootstrap import build_keepalive_http_client

_PROXY_ENV_VARS = (
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy",
)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _proxy_targets(client):
    targets = []
    for transport in (client._mounts or {}).values():
        proxy_url = getattr(getattr(transport, "_pool", None), "_proxy_url", None)
        if proxy_url is not None:
            targets.append(
                f"{proxy_url.scheme.decode()}://"
                f"{proxy_url.host.decode()}:{proxy_url.port}"
            )
    return targets


def _mount_patterns(client):
    return sorted(pattern.pattern for pattern in (client._mounts or {}))


def _with_config(monkeypatch, config):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly", lambda: config)


def test_resolve_aux_proxy_reverse_lookup_via_registry(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    assert auxiliary_client._resolve_aux_proxy(
        "https://api.anthropic.com"
    ) == "http://127.0.0.1:7890"


def test_resolve_aux_proxy_reverse_lookup_via_base_url(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {
        "providers": {
            "my-gateway": {
                "base_url": "https://gateway.example.com/v1",
                "proxy": "http://127.0.0.1:7890",
            }
        }
    })
    assert auxiliary_client._resolve_aux_proxy(
        "https://gateway.example.com/v1"
    ) == "http://127.0.0.1:7890"


def test_resolve_aux_proxy_sibling_provider_stays_direct(clean_proxy_env, monkeypatch):
    """An auxiliary model on a different provider must not inherit the proxy."""
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    assert auxiliary_client._resolve_aux_proxy("https://api.deepseek.com/v1") is None


def test_resolve_aux_proxy_forced_direct(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {"providers": {"deepseek": {"proxy": False}}})
    assert auxiliary_client._resolve_aux_proxy("https://api.deepseek.com/v1") is False


def test_resolve_aux_proxy_unconfigured(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {})
    assert auxiliary_client._resolve_aux_proxy("https://api.deepseek.com/v1") is None


def test_resolve_aux_proxy_malformed_config_degrades(clean_proxy_env, monkeypatch):
    """Best-effort like _resolve_aux_verify: a bad entry must not break an aux call.

    The primary client raises on the same entry, so the operator still finds out.
    """
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "127.0.0.1:7890"}}})
    assert auxiliary_client._resolve_aux_proxy("https://api.anthropic.com") is None


def test_resolve_aux_proxy_unreadable_config_degrades(clean_proxy_env, monkeypatch):
    import hermes_cli.config as cfg

    def _boom():
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr(cfg, "load_config_readonly", _boom)
    assert auxiliary_client._resolve_aux_proxy("https://api.anthropic.com") is None


def test_openai_http_client_kwargs_uses_the_proxy(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    kwargs = auxiliary_client._openai_http_client_kwargs("https://api.anthropic.com")
    client = kwargs.get("http_client")
    assert isinstance(client, httpx.Client)
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]


def test_build_keepalive_http_client_forced_direct_keeps_mounts(clean_proxy_env, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    client = build_keepalive_http_client("https://api.deepseek.com/v1", proxy=False)
    assert isinstance(client, httpx.Client)
    assert _proxy_targets(client) == []
    assert _mount_patterns(client) == ["http://", "https://"]


def test_build_keepalive_http_client_zero_config_unchanged(clean_proxy_env):
    client = build_keepalive_http_client("https://api.deepseek.com/v1")
    assert _proxy_targets(client) == []
    assert _mount_patterns(client) == ["http://", "https://"]


def test_build_keepalive_http_client_async_mode_proxy(clean_proxy_env):
    client = build_keepalive_http_client(
        "https://api.anthropic.com", async_mode=True, proxy="http://127.0.0.1:7890",
    )
    assert isinstance(client, httpx.AsyncClient)
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]

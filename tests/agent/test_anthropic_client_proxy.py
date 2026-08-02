"""The native Anthropic SDK path honors ``providers.anthropic.proxy``.

The SDK builds its own httpx client and reads the proxy env vars itself, so the
only way to apply a per-provider setting is to hand it one. Zero-config must
inject nothing at all — that keeps the constructed client bit-for-bit what it
was before the feature existed.
"""

import httpx
import pytest

from agent import anthropic_adapter

_PROXY_ENV_VARS = (
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy",
)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _with_config(monkeypatch, config):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly", lambda: config)


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


@pytest.fixture
def captured_anthropic(monkeypatch):
    """Capture the kwargs build_anthropic_client hands to the SDK."""
    captured = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class _FakeSDK:
        Anthropic = _FakeAnthropic

    monkeypatch.setattr(anthropic_adapter, "_get_anthropic_sdk", lambda: _FakeSDK)
    return captured


def test_no_config_injects_no_http_client(clean_proxy_env, monkeypatch, captured_anthropic):
    _with_config(monkeypatch, {})
    anthropic_adapter.build_anthropic_client("sk-ant-api03-example")
    assert "http_client" not in captured_anthropic


def test_native_endpoint_resolves_by_provider_name(clean_proxy_env, monkeypatch, captured_anthropic):
    """base_url is empty for the native API — the provider is unambiguously anthropic."""
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    anthropic_adapter.build_anthropic_client("sk-ant-api03-example")
    client = captured_anthropic.get("http_client")
    assert isinstance(client, httpx.Client)
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]


def test_explicit_anthropic_base_url_resolves_by_url(clean_proxy_env, monkeypatch, captured_anthropic):
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    anthropic_adapter.build_anthropic_client(
        "sk-ant-api03-example", "https://api.anthropic.com",
    )
    client = captured_anthropic.get("http_client")
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]


def test_third_party_anthropic_endpoint_does_not_inherit(clean_proxy_env, monkeypatch, captured_anthropic):
    """An Anthropic-compatible provider is not Anthropic — it keeps its own policy."""
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    anthropic_adapter.build_anthropic_client(
        "some-minimax-key", "https://api.minimax.io/anthropic",
    )
    assert "http_client" not in captured_anthropic


def test_forced_direct_disables_trust_env(clean_proxy_env, monkeypatch, captured_anthropic):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": False}}})
    anthropic_adapter.build_anthropic_client("sk-ant-api03-example")
    client = captured_anthropic.get("http_client")
    assert isinstance(client, httpx.Client)
    assert _proxy_targets(client) == []
    # trust_env=False is what closes the macOS system-proxy leak here; the
    # keepalive builders use plain no-proxy mounts for the same reason.
    assert client.trust_env is False


def test_oauth_token_path_still_gets_the_proxy(clean_proxy_env, monkeypatch, captured_anthropic):
    """OAuth tokens take a different auth branch — the client must still be injected."""
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})
    anthropic_adapter.build_anthropic_client("sk-ant-oat01-example-token")
    assert isinstance(captured_anthropic.get("http_client"), httpx.Client)


# ─── OAuth login / refresh egress ────────────────────────────────────────────


def test_oauth_urlopen_uses_configured_proxy(clean_proxy_env, monkeypatch):
    import urllib.request

    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}})

    seen = {}

    class _FakeOpener:
        def open(self, req, timeout=None):
            seen["timeout"] = timeout
            return "response"

    def _fake_build_opener(handler):
        seen["proxies"] = handler.proxies
        return _FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", _fake_build_opener)
    assert anthropic_adapter._oauth_urlopen(object(), timeout=15) == "response"
    assert seen["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert seen["timeout"] == 15


def test_oauth_urlopen_forced_direct_ignores_env(clean_proxy_env, monkeypatch):
    import urllib.request

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": False}}})

    seen = {}

    class _FakeOpener:
        def open(self, req, timeout=None):
            return "response"

    def _fake_build_opener(handler):
        seen["proxies"] = handler.proxies
        return _FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", _fake_build_opener)
    anthropic_adapter._oauth_urlopen(object(), timeout=15)
    assert seen["proxies"] == {}


def test_oauth_urlopen_unconfigured_is_plain_urlopen(clean_proxy_env, monkeypatch):
    import urllib.request

    _with_config(monkeypatch, {})

    called = {}

    def _fake_urlopen(req, timeout=None):
        called["timeout"] = timeout
        return "response"

    def _fail_build_opener(handler):  # pragma: no cover — must not be reached
        raise AssertionError("unconfigured proxy must use plain urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(urllib.request, "build_opener", _fail_build_opener)
    assert anthropic_adapter._oauth_urlopen(object(), timeout=10) == "response"
    assert called["timeout"] == 10


def test_oauth_urlopen_rejects_socks_with_a_clear_message(clean_proxy_env, monkeypatch):
    """urllib speaks HTTP proxies only — say so instead of failing cryptically."""
    _with_config(monkeypatch, {"providers": {"anthropic": {"proxy": "socks5://127.0.0.1:7891"}}})
    with pytest.raises(ValueError) as excinfo:
        anthropic_adapter._oauth_urlopen(object(), timeout=10)
    assert "SOCKS" in str(excinfo.value)

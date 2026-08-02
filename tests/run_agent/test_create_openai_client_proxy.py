"""Per-provider proxy reaches the primary client's httpx builder.

Two regression invariants live here:

1. **Zero-config equivalence** — with no ``proxy`` key anywhere, the client is
   built exactly as it was before the feature existed (plain no-proxy mounts,
   no proxy transport). This is the anchor protecting existing deployments.
2. **The mounts invariant** — a direct client mounts a pair of plain
   transports so httpx's ``trust_env`` path cannot pull in macOS system proxy
   settings, which ``urllib.request.getproxies()`` reports without the
   ExceptionsList. Forced-direct lands on the same branch and must keep them.
"""

import httpx
import pytest

from run_agent import AIAgent

_PROXY_ENV_VARS = (
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy",
)


@pytest.fixture
def clean_proxy_env(monkeypatch):
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _proxy_targets(client):
    """Return ``scheme://host:port`` for every proxied mount on *client*."""
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


def test_configured_proxy_reaches_the_client(clean_proxy_env):
    client = AIAgent._build_keepalive_http_client(
        "https://chatgpt.com/backend-api/codex", proxy="http://127.0.0.1:7890",
    )
    assert isinstance(client, httpx.Client)
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]


def test_configured_proxy_wins_over_env(clean_proxy_env, monkeypatch):
    """Config is more specific than a global env var — a stray HTTPS_PROXY can't win."""
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    client = AIAgent._build_keepalive_http_client(
        "https://chatgpt.com/backend-api/codex", proxy="http://127.0.0.1:7890",
    )
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]


def test_socks_alias_normalized_by_the_builder(clean_proxy_env):
    pytest.importorskip("socksio")
    client = AIAgent._build_keepalive_http_client(
        "https://api.anthropic.com", proxy="socks://127.0.0.1:7891",
    )
    assert _proxy_targets(client) == ["socks5://127.0.0.1:7891"]


def test_forced_direct_ignores_global_proxy(clean_proxy_env, monkeypatch):
    """Usage pattern B: keep the global proxy, pin a provider direct."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    client = AIAgent._build_keepalive_http_client(
        "https://api.deepseek.com/v1", proxy=False,
    )
    assert _proxy_targets(client) == []
    # Mounts invariant: forced-direct uses the same branch as "no proxy".
    assert _mount_patterns(client) == ["http://", "https://"]


def test_unconfigured_uses_env_proxy(clean_proxy_env, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    client = AIAgent._build_keepalive_http_client("https://api.deepseek.com/v1")
    assert _proxy_targets(client) == ["http://127.0.0.1:7890"]


def test_unconfigured_honors_no_proxy(clean_proxy_env, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "api.deepseek.com")
    client = AIAgent._build_keepalive_http_client("https://api.deepseek.com/v1")
    assert _proxy_targets(client) == []
    # httpx contributes its own NO_PROXY bypass pattern here; what matters is
    # that our plain transports still cover both schemes.
    assert set(_mount_patterns(client)) >= {"http://", "https://"}


def test_zero_config_matches_pre_change_behavior(clean_proxy_env):
    """No proxy anywhere: plain no-proxy mounts, exactly as before the feature."""
    client = AIAgent._build_keepalive_http_client("https://api.deepseek.com/v1")
    assert _proxy_targets(client) == []
    assert _mount_patterns(client) == ["http://", "https://"]
    assert client._transport is not None


def test_create_openai_client_forwards_resolved_proxy(clean_proxy_env, monkeypatch):
    """The resolver runs in the caller; the builder only consumes its decision."""
    import agent.agent_runtime_helpers as helpers
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg,
        "load_config_readonly",
        lambda: {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}},
    )

    seen = {}

    class _FakeAgent:
        provider = "openai-codex"

        @staticmethod
        def _build_keepalive_http_client(base_url="", *, verify=True, proxy=None):
            seen["base_url"] = base_url
            seen["proxy"] = proxy
            return None

        @staticmethod
        def _client_log_context():
            return ""

    monkeypatch.setattr(helpers, "_ra", lambda: _StubRunAgent())

    class _StubRunAgent:
        class OpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class logger:
            @staticmethod
            def info(*args, **kwargs):
                pass

    helpers.create_openai_client(
        _FakeAgent(),
        {"api_key": "x", "base_url": "https://chatgpt.com/backend-api/codex"},
        reason="test",
        shared=False,
    )
    assert seen["proxy"] == "http://127.0.0.1:7890"


def test_create_openai_client_zero_config_passes_none(clean_proxy_env, monkeypatch):
    import agent.agent_runtime_helpers as helpers
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly", lambda: {})

    seen = {}

    class _FakeAgent:
        provider = "deepseek"

        @staticmethod
        def _build_keepalive_http_client(base_url="", *, verify=True, proxy=None):
            seen["proxy"] = proxy
            return None

        @staticmethod
        def _client_log_context():
            return ""

    class _StubRunAgent:
        class OpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class logger:
            @staticmethod
            def info(*args, **kwargs):
                pass

    monkeypatch.setattr(helpers, "_ra", lambda: _StubRunAgent())

    helpers.create_openai_client(
        _FakeAgent(),
        {"api_key": "x", "base_url": "https://api.deepseek.com/v1"},
        reason="test",
        shared=False,
    )
    assert seen["proxy"] is None

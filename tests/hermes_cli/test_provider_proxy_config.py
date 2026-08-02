"""Tests for ``providers.<name>.proxy`` resolution.

Mirrors ``test_custom_provider_tls.py`` for the proxy knob. The three-valued
return is the load-bearing part: ``None`` ("not configured") and ``False``
("configured to go direct") must stay distinguishable, or a deployment that
keeps a global ``HTTPS_PROXY`` and pins domestic providers direct collapses
into one that has no global proxy at all.
"""

import pytest

from hermes_cli.config import resolve_provider_proxy, resolve_provider_proxy_safe
from utils import redact_proxy_url


def test_resolves_by_provider_name():
    config = {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}}
    assert resolve_provider_proxy("openai-codex", config=config) == "http://127.0.0.1:7890"


def test_provider_name_match_is_case_insensitive():
    config = {"providers": {"OpenAI-Codex": {"proxy": "http://127.0.0.1:7890"}}}
    assert resolve_provider_proxy("openai-codex", config=config) == "http://127.0.0.1:7890"


def test_false_forces_direct():
    config = {"providers": {"deepseek": {"proxy": False}}}
    assert resolve_provider_proxy("deepseek", config=config) is False


@pytest.mark.parametrize("value", ["", "   ", "direct", "NONE", " None "])
def test_direct_sentinels_force_direct(value):
    config = {"providers": {"deepseek": {"proxy": value}}}
    assert resolve_provider_proxy("deepseek", config=config) is False


def test_unconfigured_provider_returns_none():
    config = {"providers": {"deepseek": {"base_url": "https://api.deepseek.com/v1"}}}
    assert resolve_provider_proxy("deepseek", config=config) is None


def test_null_proxy_is_treated_as_unconfigured():
    """``proxy:`` written with no value is a YAML slip, not a request to go direct."""
    config = {"providers": {"deepseek": {"proxy": None}}}
    assert resolve_provider_proxy("deepseek", config=config) is None


def test_unknown_provider_returns_none():
    config = {"providers": {"deepseek": {"proxy": False}}}
    assert resolve_provider_proxy("some-other-provider", config=config) is None


def test_no_provider_and_no_base_url_returns_none():
    config = {"providers": {"deepseek": {"proxy": False}}}
    assert resolve_provider_proxy(config=config) is None


def test_socks_alias_is_normalized():
    """httpx rejects the ``socks://`` alias WSL/Clash environments export."""
    config = {"providers": {"anthropic": {"proxy": "socks://127.0.0.1:7891"}}}
    assert resolve_provider_proxy("anthropic", config=config) == "socks5://127.0.0.1:7891"


# ─── reverse lookup (auxiliary clients hold only a URL) ──────────────────────


def test_reverse_lookup_by_configured_base_url():
    config = {
        "providers": {
            "my-gateway": {
                "base_url": "https://gateway.example.com/v1",
                "proxy": "http://127.0.0.1:7890",
            }
        }
    }
    assert resolve_provider_proxy(
        base_url="https://gateway.example.com/v1/", config=config
    ) == "http://127.0.0.1:7890"


def test_reverse_lookup_via_provider_registry_host():
    """A built-in provider's entry carries only ``proxy`` — no base_url of its own."""
    config = {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}}
    assert resolve_provider_proxy(
        base_url="https://api.anthropic.com", config=config
    ) == "http://127.0.0.1:7890"


def test_reverse_lookup_does_not_leak_across_providers():
    config = {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}}
    assert resolve_provider_proxy(
        base_url="https://api.deepseek.com/v1", config=config
    ) is None


def test_reverse_lookup_no_substring_bypass():
    """A lookalike host must not pick up another provider's proxy."""
    config = {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}}
    assert resolve_provider_proxy(
        base_url="https://api.anthropic.com.attacker.test/v1", config=config
    ) is None


def test_legacy_custom_providers_list_is_searched():
    config = {
        "custom_providers": [
            {
                "name": "my-gateway",
                "base_url": "https://gateway.example.com/v1",
                "proxy": "http://127.0.0.1:7890",
            }
        ]
    }
    assert resolve_provider_proxy("my-gateway", config=config) == "http://127.0.0.1:7890"
    assert resolve_provider_proxy(
        base_url="https://gateway.example.com/v1", config=config
    ) == "http://127.0.0.1:7890"


def test_proxy_survives_legacy_entry_normalization():
    """A legacy custom_providers entry must not lose its proxy on migration."""
    from hermes_cli.config import (
        _custom_provider_entry_to_provider_config,
        _normalize_custom_provider_entry,
    )

    entry = {
        "name": "my-gateway",
        "base_url": "https://gateway.example.com/v1",
        "proxy": False,
    }
    assert _normalize_custom_provider_entry(dict(entry))["proxy"] is False
    migrated = _custom_provider_entry_to_provider_config(dict(entry))
    assert migrated["proxy"] is False


def test_name_without_proxy_key_falls_through_to_base_url():
    config = {
        "providers": {
            "openai-codex": {"model": "gpt-5.1-codex"},
            "aux-gateway": {
                "base_url": "https://gateway.example.com/v1",
                "proxy": "http://127.0.0.1:7890",
            },
        }
    }
    assert resolve_provider_proxy(
        "openai-codex", base_url="https://gateway.example.com/v1", config=config
    ) == "http://127.0.0.1:7890"


# ─── malformed values fail fast ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1:7890",          # no scheme
        "http://127.0.0.1:78o90",  # non-numeric port
        "ftp://127.0.0.1:7890",    # unsupported scheme
        True,                      # `proxy: true` is not a URL
        ["http://127.0.0.1:7890"],
        {"url": "http://127.0.0.1:7890"},
        7890,
    ],
)
def test_malformed_proxy_raises_naming_the_provider(value):
    """Silently going direct would mean traffic the operator believes is proxied is not."""
    config = {"providers": {"openai-codex": {"proxy": value}}}
    with pytest.raises(ValueError) as excinfo:
        resolve_provider_proxy("openai-codex", config=config)
    assert "providers.openai-codex.proxy" in str(excinfo.value)


def test_safe_resolver_degrades_and_warns_once(monkeypatch, caplog):
    """Best-effort callers must not raise — but the reason must not be invisible."""
    import hermes_cli.config as cfg

    cfg._PROVIDER_NORMALIZE_WARNED.clear()
    config = {"providers": {"anthropic": {"proxy": "127.0.0.1:7890"}}}
    with caplog.at_level("WARNING"):
        assert resolve_provider_proxy_safe("anthropic", config=config) is None
        assert resolve_provider_proxy_safe("anthropic", config=config) is None
    warnings = [r for r in caplog.records if "per-provider proxy" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://127.0.0.1:7890", {"proxy": "http://127.0.0.1:7890"}),
        (False, {"trust_env": False}),
        (None, {}),
    ],
)
def test_httpx_kwargs_shape(value, expected):
    """``{}`` must mean 'untouched' — not 'proxy=None', which would disable trust_env."""
    from hermes_cli.config import provider_proxy_httpx_kwargs

    config = {"providers": {"anthropic": {} if value is None else {"proxy": value}}}
    assert provider_proxy_httpx_kwargs("anthropic", config=config) == expected


def test_safe_resolver_passes_valid_values_through():
    config = {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}}
    assert resolve_provider_proxy_safe("anthropic", config=config) == "http://127.0.0.1:7890"
    assert resolve_provider_proxy_safe(
        "deepseek", config={"providers": {"deepseek": {"proxy": False}}}
    ) is False


def test_unreadable_config_falls_back_to_env(monkeypatch):
    """A config problem must not take down inference — it degrades to the env vars."""
    import hermes_cli.config as cfg

    def _boom():
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr(cfg, "load_config_readonly", _boom)
    assert resolve_provider_proxy("openai-codex") is None


def test_non_dict_config_returns_none():
    assert resolve_provider_proxy("openai-codex", config=["not", "a", "mapping"]) is None


# ─── credentials never reach a log line ──────────────────────────────────────


def test_malformed_proxy_error_does_not_leak_credentials():
    config = {"providers": {"anthropic": {"proxy": "http://user:hunter2@proxy:99999999"}}}
    with pytest.raises(ValueError) as excinfo:
        resolve_provider_proxy("anthropic", config=config)
    assert "hunter2" not in str(excinfo.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://user:pass@proxy.example.com:3128", "http://***@proxy.example.com:3128"),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("socks5://user:pass@127.0.0.1:1080", "socks5://***@127.0.0.1:1080"),
        (False, "direct"),
        ("", ""),
        ("not-a-url", "<proxy>"),
    ],
)
def test_redact_proxy_url(raw, expected):
    assert redact_proxy_url(raw) == expected

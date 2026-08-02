"""The Codex model-discovery probes honor ``providers.openai-codex.proxy``.

Both probes hit chatgpt.com, the same host as Codex inference. They degrade
gracefully to a static catalog, so a proxy miss here is silent: /model just
shows stale context windows. Unconfigured, both must keep using the plain
module-level call so existing behavior (and the tests pinning it) is unchanged.
"""

import pytest

from agent import model_metadata


def _with_config(monkeypatch, config):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "load_config_readonly", lambda: config)


def test_probe_session_is_none_when_unconfigured(monkeypatch):
    _with_config(monkeypatch, {})
    assert model_metadata._codex_probe_session() is None


def test_probe_session_carries_the_proxy(monkeypatch):
    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}})
    session = model_metadata._codex_probe_session()
    assert session is not None
    assert session.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    session.close()


def test_probe_session_forced_direct_opts_out_of_env(monkeypatch):
    """requests merges env proxies per-request unless the session opts out."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": False}}})
    session = model_metadata._codex_probe_session()
    assert session is not None
    assert session.trust_env is False
    session.close()


def test_probe_session_malformed_config_degrades(monkeypatch):
    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "127.0.0.1:7890"}}})
    assert model_metadata._codex_probe_session() is None


def test_codex_models_fetch_passes_proxy_kwargs(monkeypatch):
    import httpx

    from hermes_cli import codex_models

    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}})

    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"models": []}

    def _fake_get(url, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    codex_models._fetch_models_from_api("token")
    assert seen.get("proxy") == "http://127.0.0.1:7890"


def test_account_usage_probes_follow_the_provider_policy(monkeypatch):
    """/usage hits chatgpt.com and api.anthropic.com — same reachability wall."""
    from agent import account_usage

    _with_config(monkeypatch, {
        "providers": {
            "openai-codex": {"proxy": "http://127.0.0.1:7890"},
            "anthropic": {"proxy": False},
        }
    })
    assert account_usage._usage_proxy_kwargs("openai-codex") == {"proxy": "http://127.0.0.1:7890"}
    assert account_usage._usage_proxy_kwargs("anthropic") == {"trust_env": False}
    assert account_usage._usage_proxy_kwargs("deepseek") == {}
    # The generic-provider credits probe only knows its endpoint URL.
    assert account_usage._usage_proxy_kwargs(
        base_url="https://chatgpt.com/backend-api/codex"
    ) == {"proxy": "http://127.0.0.1:7890"}


def test_codex_models_fetch_unconfigured_passes_no_proxy(monkeypatch):
    import httpx

    from hermes_cli import codex_models

    _with_config(monkeypatch, {})

    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"models": []}

    def _fake_get(url, **kwargs):
        seen.update(kwargs)
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    codex_models._fetch_models_from_api("token")
    assert "proxy" not in seen
    assert "trust_env" not in seen

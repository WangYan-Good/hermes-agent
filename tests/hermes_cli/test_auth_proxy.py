"""Codex login / token refresh honor ``providers.openai-codex.proxy``.

Login coverage is not optional: Codex authenticates by device code against
auth.openai.com, which is unreachable from the networks that need a proxy for
inference. Wiring inference alone leaves the feature unusable — the operator
can never log in to use it.
"""

import pytest

from hermes_cli.auth import oauth_httpx_proxy_kwargs

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


def test_kwargs_carry_the_configured_proxy(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}})
    assert oauth_httpx_proxy_kwargs("openai-codex") == {"proxy": "http://127.0.0.1:7890"}


def test_kwargs_empty_when_unconfigured(clean_proxy_env, monkeypatch):
    """Nothing configured → httpx keeps its default trust_env behavior."""
    _with_config(monkeypatch, {})
    assert oauth_httpx_proxy_kwargs("openai-codex") == {}


def test_kwargs_force_direct(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": False}}})
    assert oauth_httpx_proxy_kwargs("openai-codex") == {"trust_env": False}


def test_kwargs_malformed_config_degrades_to_env(clean_proxy_env, monkeypatch):
    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "127.0.0.1:7890"}}})
    assert oauth_httpx_proxy_kwargs("openai-codex") == {}


def test_refresh_codex_oauth_pure_passes_the_proxy(clean_proxy_env, monkeypatch):
    import hermes_cli.auth as auth

    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}})

    seen = {}

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "id_token": "",
                "expires_in": 3600,
            }

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(auth.httpx, "Client", _FakeClient)
    auth.refresh_codex_oauth_pure("old-access", "old-refresh")
    assert seen.get("proxy") == "http://127.0.0.1:7890"


def test_refresh_codex_oauth_pure_zero_config_unchanged(clean_proxy_env, monkeypatch):
    import hermes_cli.auth as auth

    _with_config(monkeypatch, {})

    seen = {}

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    class _FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(auth.httpx, "Client", _FakeClient)
    auth.refresh_codex_oauth_pure("old-access", "old-refresh")
    assert "proxy" not in seen
    assert "trust_env" not in seen


def test_device_code_login_requests_go_through_the_proxy(clean_proxy_env, monkeypatch):
    import hermes_cli.auth as auth

    _with_config(monkeypatch, {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}})

    constructed = []

    class _FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.headers = {}

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/usercode"):
                return _FakeResponse({
                    "user_code": "ABCD-1234",
                    "device_auth_id": "dev-1",
                    "interval": "0",
                })
            if url.endswith("/deviceauth/token"):
                return _FakeResponse({
                    "authorization_code": "auth-code",
                    "code_verifier": "verifier",
                })
            return _FakeResponse({
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "",
                "expires_in": 3600,
            })

    import time

    monkeypatch.setattr(auth.httpx, "Client", _FakeClient)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    auth._codex_device_code_login()

    assert constructed, "no httpx client was constructed"
    assert all(kw.get("proxy") == "http://127.0.0.1:7890" for kw in constructed)

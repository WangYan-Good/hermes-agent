"""A settings-only ``providers.<name>`` entry must not become a picker row.

``providers.openai-codex: {proxy: ...}`` configures a *built-in* provider; it
declares no endpoint and no models. Emitting a row for it would show a dead
"0 models" duplicate of the built-in whenever the built-in row hasn't been
emitted yet — i.e. before that provider is authenticated, which is exactly when
an operator is setting the proxy up.
"""

import pytest

from hermes_cli import model_switch


@pytest.fixture(autouse=True)
def _no_live_probe(monkeypatch):
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *_a, **_kw: None)


def _slugs(rows):
    return {str(row.get("slug", "")).lower() for row in rows}


def test_proxy_only_entry_is_not_listed():
    rows = model_switch.list_authenticated_providers(
        user_providers={"openai-codex": {"proxy": "http://127.0.0.1:7890"}},
        custom_providers=[],
        probe_custom_providers=False,
    )
    assert not [
        row for row in rows
        if str(row.get("slug", "")).lower() == "openai-codex"
        and row.get("source") == "user-config"
    ]


def test_real_user_endpoint_is_still_listed():
    rows = model_switch.list_authenticated_providers(
        user_providers={
            "my-gateway": {
                "base_url": "https://gateway.example.com/v1",
                "proxy": "http://127.0.0.1:7890",
                "models": {"my-model": {}},
            }
        },
        custom_providers=[],
        probe_custom_providers=False,
    )
    assert "my-gateway" in _slugs(rows)


def test_entry_with_only_a_default_model_is_still_listed():
    rows = model_switch.list_authenticated_providers(
        user_providers={"my-gateway": {"default_model": "my-model"}},
        custom_providers=[],
        probe_custom_providers=False,
    )
    assert "my-gateway" in _slugs(rows)

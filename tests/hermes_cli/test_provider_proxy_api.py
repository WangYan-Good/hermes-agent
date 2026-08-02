"""Tests for the dashboard's per-provider proxy surface.

Covers hermes_cli.provider_proxy_admin (pure, at the config-dict level) and the
routes in web_server that expose it. The cross-contamination case matters most:
the UI asks "what did this provider declare", not "which proxy would a request
use", and resolve_provider_proxy answers only the second question.
"""

import pytest


# ── Editability ──────────────────────────────────────────────────────────────


class TestProviderProxyEditable:
    def test_builtin_registry_provider_is_editable(self):
        from hermes_cli.provider_proxy_admin import provider_proxy_editable

        assert provider_proxy_editable({}, "anthropic") is True
        assert provider_proxy_editable({}, "openai-codex") is True

    def test_existing_config_key_is_editable_even_if_not_builtin(self):
        from hermes_cli.provider_proxy_admin import provider_proxy_editable

        cfg = {"providers": {"my-gateway": {"base_url": "https://gw.internal/v1"}}}
        assert provider_proxy_editable(cfg, "my-gateway") is True

    def test_unknown_id_is_rejected(self):
        from hermes_cli.provider_proxy_admin import provider_proxy_editable

        # Writing an arbitrary key into config.yaml is exactly what the
        # allowlist exists to prevent.
        assert provider_proxy_editable({}, "not-a-provider") is False
        assert provider_proxy_editable({}, "") is False

    def test_synthetic_claude_code_row_is_not_editable(self):
        from hermes_cli.provider_proxy_admin import provider_proxy_editable

        # It shows on the Accounts tab but owns no config entry and no
        # inference host, so there is nothing to configure or probe.
        assert provider_proxy_editable({}, "claude-code") is False


class TestIsRedactedProxyUrl:
    def test_detects_the_form_we_hand_the_browser(self):
        from hermes_cli.provider_proxy_admin import is_redacted_proxy_url

        assert is_redacted_proxy_url("http://***@proxy.internal:3128") is True

    def test_real_values_are_not_redacted(self):
        from hermes_cli.provider_proxy_admin import is_redacted_proxy_url

        assert is_redacted_proxy_url("http://127.0.0.1:7890") is False
        assert is_redacted_proxy_url("http://bob:pass@proxy.internal:3128") is False
        assert is_redacted_proxy_url("") is False
        assert is_redacted_proxy_url(None) is False


# ── Read ─────────────────────────────────────────────────────────────────────


class TestReadProviderProxyState:
    def test_absent_key_reads_as_inherit(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        cfg = {"providers": {"anthropic": {"base_url": "https://api.anthropic.com"}}}
        assert read_provider_proxy_state(cfg, "anthropic") == {
            "mode": "inherit",
            "url": None,
        }

    def test_provider_with_no_entry_at_all_reads_as_inherit(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        assert read_provider_proxy_state({}, "openai-codex") == {
            "mode": "inherit",
            "url": None,
        }

    def test_false_reads_as_direct(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        cfg = {"providers": {"anthropic": {"proxy": False}}}
        assert read_provider_proxy_state(cfg, "anthropic") == {
            "mode": "direct",
            "url": None,
        }

    def test_url_reads_as_url(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        cfg = {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}}
        assert read_provider_proxy_state(cfg, "openai-codex") == {
            "mode": "url",
            "url": "http://127.0.0.1:7890",
        }

    def test_credentials_never_reach_the_browser(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        cfg = {
            "providers": {
                "openai-codex": {"proxy": "http://bob:hunter2@proxy.internal:3128"}
            }
        }
        state = read_provider_proxy_state(cfg, "openai-codex")
        assert state == {"mode": "url", "url": "http://***@proxy.internal:3128"}
        assert "hunter2" not in repr(state)

    def test_declared_state_does_not_leak_across_providers(self):
        """The resolver trap this whole read path exists to avoid."""
        from hermes_cli.config import resolve_provider_proxy
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        cfg = {
            "providers": {
                "gw-a": {
                    "base_url": "https://gw.internal/v1",
                    "proxy": "http://127.0.0.1:7890",
                },
                "gw-b": {"base_url": "https://gw.internal/v1"},
            }
        }
        # The resolver reverse-matches by base_url, so a request to that URL
        # would legitimately be routed through gw-a's proxy...
        assert (
            resolve_provider_proxy(base_url="https://gw.internal/v1", config=cfg)
            == "http://127.0.0.1:7890"
        )
        # ...but gw-b itself declares nothing, and that is what the UI shows.
        assert read_provider_proxy_state(cfg, "gw-b") == {
            "mode": "inherit",
            "url": None,
        }

    def test_malformed_stored_value_renders_instead_of_raising(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        # `proxy: true` is a plausible hand-edit. One bad value must not take
        # down the entire Accounts tab.
        cfg = {"providers": {"anthropic": {"proxy": True}}}
        assert read_provider_proxy_state(cfg, "anthropic") == {
            "mode": "url",
            "url": None,
            "invalid": True,
        }

    def test_empty_yaml_value_reads_as_inherit(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        # `proxy:` with nothing after it parses as None, which the coercion
        # layer already treats as absence.
        cfg = {"providers": {"anthropic": {"proxy": None}}}
        assert read_provider_proxy_state(cfg, "anthropic") == {
            "mode": "inherit",
            "url": None,
        }

    def test_non_editable_provider_has_no_state(self):
        from hermes_cli.provider_proxy_admin import read_provider_proxy_state

        assert read_provider_proxy_state({}, "claude-code") is None


# ── Write ────────────────────────────────────────────────────────────────────


class TestApplyProviderProxy:
    def test_url_mode_writes_the_normalized_string(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {}
        state = apply_provider_proxy(cfg, "anthropic", "url", "http://127.0.0.1:7890")
        assert cfg["providers"]["anthropic"]["proxy"] == "http://127.0.0.1:7890"
        assert state == {"mode": "url", "url": "http://127.0.0.1:7890"}

    def test_url_mode_normalizes_the_socks_alias(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        # httpx rejects the bare `socks://` alias WSL/Clash environments export.
        cfg = {}
        apply_provider_proxy(cfg, "anthropic", "url", "socks://127.0.0.1:7891")
        assert cfg["providers"]["anthropic"]["proxy"] == "socks5://127.0.0.1:7891"

    def test_direct_mode_writes_false(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {}
        state = apply_provider_proxy(cfg, "anthropic", "direct", None)
        assert cfg["providers"]["anthropic"]["proxy"] is False
        assert state == {"mode": "direct", "url": None}

    def test_inherit_mode_removes_the_key(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {"providers": {"anthropic": {"proxy": "http://127.0.0.1:7890"}}}
        state = apply_provider_proxy(cfg, "anthropic", "inherit")
        # The whole entry goes: nothing else was configured, and an
        # `anthropic: {}` shell is noise the next reader has to decipher.
        assert "anthropic" not in cfg["providers"]
        assert state == {"mode": "inherit", "url": None}

    def test_inherit_mode_keeps_a_user_defined_endpoint(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {
            "providers": {
                "my-gateway": {
                    "base_url": "https://gw.internal/v1",
                    "model": "gw-large",
                    "proxy": "http://127.0.0.1:7890",
                }
            }
        }
        apply_provider_proxy(cfg, "my-gateway", "inherit")
        assert cfg["providers"]["my-gateway"] == {
            "base_url": "https://gw.internal/v1",
            "model": "gw-large",
        }

    def test_hand_written_keys_survive_a_proxy_edit(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        # Rebuilding the entry instead of merging is what silently dropped
        # api_mode/key_env/extra_headers and left providers that no longer
        # authenticated.
        cfg = {
            "providers": {
                "anthropic": {
                    "api_mode": "anthropic",
                    "key_env": "ANTHROPIC_API_KEY",
                    "extra_headers": {"CF-Access-Client-Secret": "s3cret"},
                }
            }
        }
        apply_provider_proxy(cfg, "anthropic", "url", "http://127.0.0.1:7890")
        assert cfg["providers"]["anthropic"] == {
            "api_mode": "anthropic",
            "key_env": "ANTHROPIC_API_KEY",
            "extra_headers": {"CF-Access-Client-Secret": "s3cret"},
            "proxy": "http://127.0.0.1:7890",
        }

    def test_unknown_provider_writes_nothing(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {}
        with pytest.raises(ValueError, match="Unknown provider"):
            apply_provider_proxy(cfg, "not-a-provider", "url", "http://127.0.0.1:7890")
        assert cfg == {}

    def test_unknown_mode_is_rejected(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        with pytest.raises(ValueError, match="Unknown proxy mode"):
            apply_provider_proxy({}, "anthropic", "off")

    def test_malformed_url_is_rejected_without_leaking_credentials(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {}
        with pytest.raises(ValueError) as excinfo:
            apply_provider_proxy(
                cfg, "anthropic", "url", "ftp://bob:hunter2@proxy.internal:3128"
            )
        assert "hunter2" not in str(excinfo.value)
        # Never silently downgraded to a direct connection.
        assert cfg == {}

    def test_redacted_url_submitted_back_is_rejected(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {}
        with pytest.raises(ValueError, match="masked form"):
            apply_provider_proxy(cfg, "anthropic", "url", "http://***@proxy.internal:3128")
        assert cfg == {}

    def test_empty_url_in_url_mode_is_rejected(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        with pytest.raises(ValueError, match="required"):
            apply_provider_proxy({}, "anthropic", "url", "   ")

    def test_url_is_ignored_for_the_other_modes(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        # The frontend keeps a half-typed address in the box while the user
        # flips the select; that must not fail the save.
        cfg = {}
        apply_provider_proxy(cfg, "anthropic", "direct", "http://half-typed")
        assert cfg["providers"]["anthropic"]["proxy"] is False

    def test_other_providers_are_untouched(self):
        from hermes_cli.provider_proxy_admin import apply_provider_proxy

        cfg = {"providers": {"openai-codex": {"proxy": "http://127.0.0.1:7890"}}}
        apply_provider_proxy(cfg, "anthropic", "direct")
        assert cfg["providers"]["openai-codex"] == {"proxy": "http://127.0.0.1:7890"}


# ── Probe ────────────────────────────────────────────────────────────────────


class TestProbeTarget:
    def test_target_comes_from_the_registry(self):
        from hermes_cli.provider_proxy_admin import provider_probe_url

        assert provider_probe_url("anthropic") == "https://api.anthropic.com/models"

    def test_provider_without_an_inference_host_is_rejected(self):
        from hermes_cli.provider_proxy_admin import provider_probe_url

        with pytest.raises(ValueError, match="No known inference host"):
            provider_probe_url("claude-code")


class TestProxyHttpxKwargsForMode:
    def test_inherit_leaves_trust_env_alone(self):
        from hermes_cli.provider_proxy_admin import proxy_httpx_kwargs_for_mode

        assert proxy_httpx_kwargs_for_mode("inherit") == {}

    def test_direct_disables_trust_env(self):
        from hermes_cli.provider_proxy_admin import proxy_httpx_kwargs_for_mode

        # Not `proxy=None`: httpx resolves env proxies through getproxies(),
        # which on macOS also reports system proxy settings.
        assert proxy_httpx_kwargs_for_mode("direct") == {"trust_env": False}

    def test_url_passes_the_proxy_through(self):
        from hermes_cli.provider_proxy_admin import proxy_httpx_kwargs_for_mode

        assert proxy_httpx_kwargs_for_mode("url", "http://127.0.0.1:7890") == {
            "proxy": "http://127.0.0.1:7890"
        }


class TestProbeProviderProxy:
    def _patch_httpx(self, monkeypatch, *, response=None, error=None):
        """Replace httpx.Client with a recorder, returning the captured kwargs."""
        import httpx

        captured = {}

        class _FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url):
                captured["url"] = url
                if error is not None:
                    raise error
                return response

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        return captured

    def test_200_is_reachable(self, monkeypatch):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        self._patch_httpx(monkeypatch, response=httpx.Response(200))
        result = probe_provider_proxy("anthropic", "direct")
        assert result["kind"] == "reachable"
        assert result["ok"] is True
        assert result["status"] == 200

    def test_401_is_reachable(self, monkeypatch):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        # The probe sends no credentials, so 401 is the expected success.
        self._patch_httpx(monkeypatch, response=httpx.Response(401))
        result = probe_provider_proxy("anthropic", "direct")
        assert result["kind"] == "reachable"
        assert result["ok"] is True

    def test_403_is_an_http_answer_not_a_success(self, monkeypatch):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        # api.anthropic.com answers 403 on a direct connection from a blocked
        # region. "Any response means success" would paint that green.
        self._patch_httpx(monkeypatch, response=httpx.Response(403))
        result = probe_provider_proxy("anthropic", "direct")
        assert result["kind"] == "http"
        assert result["ok"] is False
        assert result["status"] == 403

    def test_transport_error_is_classified_and_redacted(self, monkeypatch):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        raw = "http://bob:hunter2@proxy.internal:3128"
        self._patch_httpx(
            monkeypatch,
            error=httpx.ConnectError(f"failed to connect via {raw}"),
        )
        result = probe_provider_proxy("anthropic", "url", raw)
        assert result["kind"] == "transport_error"
        assert result["ok"] is False
        assert result["status"] is None
        assert "hunter2" not in result["detail"]
        assert "***@proxy.internal:3128" in result["detail"]

    def test_probe_dials_the_registry_host_through_the_submitted_proxy(
        self, monkeypatch
    ):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        captured = self._patch_httpx(monkeypatch, response=httpx.Response(200))
        probe_provider_proxy("anthropic", "url", "http://127.0.0.1:7890")
        assert captured["url"] == "https://api.anthropic.com/models"
        assert captured["proxy"] == "http://127.0.0.1:7890"

    def test_direct_mode_probe_sets_trust_env_false(self, monkeypatch):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        captured = self._patch_httpx(monkeypatch, response=httpx.Response(200))
        probe_provider_proxy("anthropic", "direct")
        assert captured["trust_env"] is False
        assert "proxy" not in captured

    def test_inherit_mode_probe_passes_no_proxy_kwargs(self, monkeypatch):
        import httpx

        from hermes_cli.provider_proxy_admin import probe_provider_proxy

        captured = self._patch_httpx(monkeypatch, response=httpx.Response(200))
        probe_provider_proxy("anthropic", "inherit")
        assert "proxy" not in captured
        assert "trust_env" not in captured


# ── Routes ───────────────────────────────────────────────────────────────────


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def _on_disk_providers():
    """The `providers:` block as actually written to config.yaml."""
    import yaml

    from hermes_cli.config import get_config_path

    path = get_config_path()
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text()) or {}).get("providers") or {}


class TestProxyRoutes:
    @pytest.fixture(autouse=True)
    def _setup(self, _isolate_hermes_home):
        self.client = _client()

    def test_oauth_list_reports_each_provider_declared_proxy(self):
        assert (
            self.client.put(
                "/api/providers/anthropic/proxy",
                json={"mode": "url", "url": "http://127.0.0.1:7890"},
            ).status_code
            == 200
        )
        providers = self.client.get("/api/providers/oauth").json()["providers"]
        by_id = {p["id"]: p for p in providers}
        assert by_id["anthropic"]["proxy"] == {
            "mode": "url",
            "url": "http://127.0.0.1:7890",
        }
        # A provider that declared nothing says so, rather than inheriting the
        # display of a neighbour that did.
        assert by_id["openai-codex"]["proxy"] == {"mode": "inherit", "url": None}

    def test_oauth_list_offers_no_editor_for_the_synthetic_row(self):
        providers = self.client.get("/api/providers/oauth").json()["providers"]
        by_id = {p["id"]: p for p in providers}
        # claude-code owns no config key and no inference host.
        assert by_id["claude-code"]["proxy"] is None

    def test_put_url_persists_to_config_yaml(self):
        response = self.client.put(
            "/api/providers/anthropic/proxy",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        assert response.status_code == 200
        assert response.json()["proxy"] == {
            "mode": "url",
            "url": "http://127.0.0.1:7890",
        }
        assert _on_disk_providers()["anthropic"]["proxy"] == "http://127.0.0.1:7890"

    def test_put_direct_persists_false(self):
        self.client.put("/api/providers/anthropic/proxy", json={"mode": "direct"})
        assert _on_disk_providers()["anthropic"]["proxy"] is False

    def test_put_inherit_removes_the_entry(self):
        self.client.put(
            "/api/providers/anthropic/proxy",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        response = self.client.put(
            "/api/providers/anthropic/proxy", json={"mode": "inherit"}
        )
        assert response.status_code == 200
        assert "anthropic" not in _on_disk_providers()

    def test_put_rejects_a_malformed_url_without_writing(self):
        response = self.client.put(
            "/api/providers/anthropic/proxy",
            json={"mode": "url", "url": "ftp://bob:hunter2@proxy.internal:3128"},
        )
        assert response.status_code == 400
        assert "hunter2" not in response.text
        assert "anthropic" not in _on_disk_providers()

    def test_put_rejects_the_redacted_value_sent_back(self):
        response = self.client.put(
            "/api/providers/anthropic/proxy",
            json={"mode": "url", "url": "http://***@proxy.internal:3128"},
        )
        assert response.status_code == 400
        assert "anthropic" not in _on_disk_providers()

    def test_put_rejects_an_unknown_provider(self):
        response = self.client.put(
            "/api/providers/not-a-provider/proxy",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        assert response.status_code == 400
        assert "not-a-provider" not in _on_disk_providers()

    def test_put_requires_a_token(self):
        from hermes_cli.web_server import _SESSION_HEADER_NAME

        del self.client.headers[_SESSION_HEADER_NAME]
        response = self.client.put(
            "/api/providers/anthropic/proxy",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        assert response.status_code == 401
        assert "anthropic" not in _on_disk_providers()

    def test_test_route_probes_the_registry_host(self, monkeypatch):
        import httpx

        captured = {}

        class _FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url):
                captured["url"] = url
                return httpx.Response(401)

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        response = self.client.post(
            "/api/providers/anthropic/proxy/test",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "reachable"
        assert response.json()["ok"] is True
        # The target is the registry's host, never anything from the request.
        assert captured["url"] == "https://api.anthropic.com/models"
        assert captured["proxy"] == "http://127.0.0.1:7890"

    def test_test_route_does_not_save(self, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx, "Client", type("C", (), {
                "__init__": lambda self, **kw: None,
                "__enter__": lambda self: self,
                "__exit__": lambda self, *e: False,
                "get": lambda self, url: httpx.Response(200),
            })
        )
        self.client.post(
            "/api/providers/anthropic/proxy/test",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        assert "anthropic" not in _on_disk_providers()

    def test_test_route_requires_a_token(self):
        from hermes_cli.web_server import _SESSION_HEADER_NAME

        del self.client.headers[_SESSION_HEADER_NAME]
        response = self.client.post(
            "/api/providers/anthropic/proxy/test", json={"mode": "direct"}
        )
        assert response.status_code == 401

    def test_test_route_rejects_a_provider_with_no_probe_host(self):
        response = self.client.post(
            "/api/providers/claude-code/proxy/test", json={"mode": "direct"}
        )
        # Not editable in the first place, so it never reaches the probe.
        assert response.status_code == 400

    def test_hand_written_keys_survive_a_dashboard_proxy_edit(self):
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        providers = cfg.setdefault("providers", {})
        providers["anthropic"] = {
            "api_mode": "anthropic",
            "extra_headers": {"CF-Access-Client-Secret": "s3cret"},
        }
        save_config(cfg)

        self.client.put(
            "/api/providers/anthropic/proxy",
            json={"mode": "url", "url": "http://127.0.0.1:7890"},
        )
        entry = _on_disk_providers()["anthropic"]
        assert entry["api_mode"] == "anthropic"
        assert entry["extra_headers"] == {"CF-Access-Client-Secret": "s3cret"}
        assert entry["proxy"] == "http://127.0.0.1:7890"

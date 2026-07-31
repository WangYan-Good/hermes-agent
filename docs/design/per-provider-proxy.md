# Per-provider proxy configuration

**Date:** 2026-07-30
**Branch:** `personal`
**Status:** design approved, pending implementation plan

## Problem

Hermes resolves proxies from environment variables only. `_get_proxy_from_env()`
reads `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, and `_get_proxy_for_base_url()`
subtracts hosts matched by `NO_PROXY` (`agent/process_bootstrap.py:112-142`).
There is no per-provider proxy setting: `_KNOWN_KEYS` in `hermes_cli/config.py:5300`
accepts `base_url`, `extra_headers`, `ssl_ca_cert`, `ssl_verify` and timeouts, but
nothing about proxies.

That forces an all-or-nothing choice on operators who need a proxy for some
providers and direct access for others. The concrete case driving this: reaching
`chatgpt.com` (Codex) and `api.anthropic.com` (Claude) requires a proxy from
mainland China, while `api.deepseek.com` and `dashscope.aliyuncs.com` must stay
direct — routing them through the proxy adds latency and couples their
availability to the proxy's.

`NO_PROXY` can express this, but only as a global deny-list keyed on hostname
suffixes. It cannot express "this provider uses proxy A, that one uses proxy B",
it silently does nothing when a hostname is spelled differently from the
configured `base_url`, and it does not support CIDR notation — a fact that
routinely surprises operators who write `192.168.1.0/24`.

## Goals

- `providers.<name>.proxy` in `config.yaml`, matched by provider name.
- Cover every model-related egress: inference clients, auxiliary clients, the
  native Anthropic SDK path, and OAuth login / token refresh.
- Per-provider configuration wins over the global environment variables.
- A provider can be forced direct even when a global proxy is set.
- Zero configuration behaves exactly as today.

## Non-goals

- Non-model egress (`web_search` / `web_fetch` tools, update checks, skill
  downloads, gateway platform clients). Those are better served by the global
  `HTTPS_PROXY`.
- Per-model proxies within a single provider.
- Proxy auto-discovery or health checking.

## Configuration surface

```yaml
providers:
  openai-codex:
    proxy: "http://127.0.0.1:7890"
  anthropic:
    proxy: "http://127.0.0.1:7890"
  deepseek:
    proxy: false          # force direct, ignoring a global HTTPS_PROXY
```

Values are interpreted in this order — the sentinels are checked before anything
is treated as a URL, so a literal `"direct"` can never be mistaken for one:

| Value | Meaning |
|---|---|
| `false`, `""`, or the strings `"direct"` / `"none"` (case-insensitive, trimmed) | Force direct. |
| Key absent, or `null` | Fall back to `HTTPS_PROXY` + `NO_PROXY`. Behavior identical to today. |
| Any other non-empty string | Use as the proxy URL. `http://`, `https://`, `socks5://`. Normalized through the existing `normalize_proxy_url` (`utils.py:487`), which rewrites the `socks://` alias httpx rejects. |

`"proxy"` joins `_KNOWN_KEYS` (`hermes_cli/config.py:5300`) so provider entries
carrying it do not warn on every config load.

Two usage patterns follow from these semantics:

- **A — no global proxy.** Set `proxy` only on `openai-codex` and `anthropic`.
  Every other provider is untouched and stays direct.
- **B — keep the global proxy.** Leave `HTTPS_PROXY` set and pin the domestic
  providers with `proxy: false`.

## Resolution

A single authority in `hermes_cli/config.py`, next to
`get_custom_provider_tls_settings`:

```python
def resolve_provider_proxy(
    provider: str | None = None,
    *,
    base_url: str | None = None,
    config: dict | None = None,
) -> str | bool | None:
    """Return a proxy URL, False (force direct), or None (fall back to env)."""
```

The three-valued return is load-bearing. `None` ("not configured") and `False`
("configured to go direct") must stay distinguishable, or usage pattern B
collapses into usage pattern A.

Lookup order:

1. `provider` names an entry with a `proxy` key → return it.
2. Otherwise identify the provider from `base_url`:
   - a `providers:` / `custom_providers:` entry whose `base_url` compares equal
     under the existing `normalize_route_base_url`, or
   - a `PROVIDER_REGISTRY` entry (`hermes_cli/auth.py:176`) whose
     `inference_base_url` host matches; its name is then looked up in `providers:`.
3. Neither → `None`.

Step 2 exists because auxiliary clients never learn their provider's name —
`_resolve_aux_verify(base_url)` (`agent/auxiliary_client.py:134`) has only a URL,
and an auxiliary model may sit on a different provider than the primary one.
Reverse lookup identifies *which provider a URL belongs to*; the configuration key
is still the provider name.

`load_config_readonly()` (`hermes_cli/config.py:7497`) is already cached at roughly
130µs per hit, so the resolver needs no cache of its own.

## Wiring

Resolution happens in the caller; the HTTP client builders only accept an
already-resolved value. `agent/process_bootstrap.py` runs during startup, and
making it import `hermes_cli.config` risks an import cycle.

```
config.yaml providers.<name>.proxy
        │
        ├─ primary client ─► create_openai_client()            agent_runtime_helpers.py:1950
        │                     resolve_provider_proxy(agent.provider)
        │                     └─► _build_keepalive_http_client(base_url, verify=, proxy=)
        │                                                       run_agent.py:4388
        │
        ├─ auxiliary ──────► _resolve_aux_proxy(base_url)       agent/auxiliary_client.py (new,
        │                     └─► build_keepalive_http_client()  alongside _resolve_aux_verify)
        │                                                       agent/process_bootstrap.py:145
        │
        ├─ native Anthropic ► build_anthropic_client()          agent/anthropic_adapter.py:727
        │                     inject an httpx client carrying the proxy
        │                     (today it passes no http_client and relies on trust_env)
        │
        └─ login / refresh ► four call sites with a statically known provider:
                              _codex_device_code_login()        hermes_cli/auth.py:7783
                              refresh_codex_oauth_pure()        hermes_cli/auth.py:3638
                              run_hermes_oauth_login_pure()     agent/anthropic_adapter.py:1434
                              refresh_anthropic_oauth_pure()    agent/anthropic_adapter.py:1036
```

Login coverage is not optional. Codex authenticates by device code against
`chatgpt.com` and Claude by pasted code against `claude.ai`; both are unreachable
without the proxy, so wiring inference alone leaves the feature unusable.

Builder semantics, applied identically in `run_agent.py:4388` and its mirror in
`agent/process_bootstrap.py:145`:

```python
def _build_keepalive_http_client(base_url="", *, verify=True, proxy=None):
    if proxy is None:
        resolved = _get_proxy_for_base_url(base_url)   # existing env path, unchanged
    elif proxy is False:
        resolved = None                                 # forced direct
    else:
        resolved = normalize_proxy_url(proxy)
```

**Invariant to preserve.** When `resolved` is `None` the current code mounts a
pair of plain no-proxy transports (`mounts={"http://": ..., "https://": ...}`).
That is deliberate: it disables httpx's default `trust_env` path so macOS system
proxy settings, which `urllib.request.getproxies()` reports without the
ExceptionsList, cannot leak in (`agent/process_bootstrap.py:155`). Forced-direct
lands on the same branch and must keep the mounts.

## Error handling

| Case | Behavior | Rationale |
|---|---|---|
| Malformed proxy URL (non-numeric port, missing scheme) | Raise, naming the provider and the key | Matches `_validate_proxy_env_urls()` (`agent/auxiliary_client.py:2648`), which already fails fast on malformed env proxies. Silently falling back to direct would mean traffic the operator believes is proxied is not — worse than a startup failure. |
| Wrong type (list, dict) | Raise, same message shape | |
| Config unreadable (corrupt file, permissions) | Return `None`, fall back to env | Mirrors `_resolve_aux_verify`'s `except: return True`. A config problem should not take down inference. |
| `socks5://` without `socksio` installed | Catch httpx's ImportError, re-raise with `pip install httpx[socks]` | httpx's native error does not mention the proxy as the cause. |
| `proxy:` present but `null` | Treat as unconfigured | A key written with no value is a common YAML slip; equating it to absence is least surprising. |

Warnings reuse `_warn_once_per_provider` (`hermes_cli/config.py:5264`) so a
malformed entry does not flood the log on every config load.

**Redaction.** Proxy URLs can carry credentials (`http://user:pass@host:port`).
The repository's standing rule for `extra_headers` is that values are secrets and
are never logged; proxy URLs get the same treatment. No reusable URL-credential
redactor exists, so this design adds a small local helper that renders
`http://***@host:port`.

## Testing

Each new test file mirrors an existing one covering the same path for TLS.

| New test | Modeled on | Covers |
|---|---|---|
| `tests/hermes_cli/test_provider_proxy_config.py` | `test_custom_provider_tls.py` | Resolver: name hit, base_url reverse lookup, `false`, unconfigured, malformed raises, redaction |
| `tests/run_agent/test_create_openai_client_proxy.py` | `test_create_openai_client_ssl_verify.py` | Proxy reaches the builder; mounts invariant |
| `tests/agent/test_auxiliary_client_proxy.py` | `test_auxiliary_client_ssl_verify.py` | Auxiliary clients resolve via reverse lookup |
| `tests/agent/test_anthropic_client_proxy.py` | new | `build_anthropic_client` injects an http_client carrying the proxy |
| `tests/hermes_cli/test_auth_proxy.py` | new | The four login/refresh sites pass the proxy (httpx mocked) |

Two regression invariants get explicit assertions:

1. **Zero-config equivalence.** With no `proxy` key anywhere, the constructed
   httpx client arguments match pre-change behavior exactly. This is the anchor
   protecting existing deployments.
2. **Usage pattern B.** Global `HTTPS_PROXY=http://127.0.0.1:7890` together with
   `deepseek.proxy: false` sends Codex and Anthropic through the proxy and
   DeepSeek direct.

The mounts invariant gets its own assertion, since it is implicit behavior that a
future refactor could drop without any test noticing.

All tests mock the network. Verifying real connectivity means running
`hermes auth add openai-codex` by hand.

## Decisions made during design

- **Match by provider name, not base_url.** The `providers:` map is already keyed
  by name, and login flows hold a provider id rather than a URL.
- **Config wins over environment.** A per-provider setting is more specific than a
  global variable. The reverse ordering would let a stray `HTTPS_PROXY` in the
  container silently defeat the whole feature.
- **Resolution in the caller, not inside `_get_proxy_for_base_url`.** Pushing
  config loading into that low-level function would avoid touching signatures, but
  it inverts the dependency direction for a startup module and makes reverse
  lookup the only path — contradicting name-based matching.
- **Rejected: httpx `mounts` routing table.** Mounts match on URL prefix rather
  than provider identity, and the bare `httpx.post` calls in `hermes_cli/auth.py`
  would ignore such a table anyway.

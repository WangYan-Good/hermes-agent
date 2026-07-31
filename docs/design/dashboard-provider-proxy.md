# Per-provider proxy in the dashboard

**Date:** 2026-07-31
**Branch:** `personal`
**Status:** design approved, pending implementation plan
**Builds on:** `docs/design/per-provider-proxy.md` (the config-level feature, implemented 2026-07-31)

## Problem

`providers.<name>.proxy` can only be set by editing `config.yaml` — through
`docker exec`, or through the dashboard's Config page in raw-YAML mode. There is
no field for it anywhere in the UI.

The Config page's form mode cannot grow one on its own: its fields are generated
by walking `DEFAULT_CONFIG` (`web_server.py:995`), and `DEFAULT_CONFIG["providers"]`
is an empty dict, so no provider key produces a field. The Models page is
organized by *model* (`ModelCard` renders a usage-analytics entry), not by
provider. The only per-provider surface in the dashboard is the Env page.

## Goals

- Set a provider's proxy from the dashboard, covering all three configured
  states.
- Verify the proxy reaches the provider *before* saving.
- Never destroy hand-written config the UI has no field for.
- Never leak proxy credentials to the browser or the logs.

## Non-goals

- API-key providers (DeepSeek, DashScope, …). The Env page groups those by
  environment-variable prefix (`DEEPSEEK_` → "DeepSeek", `EnvPage.tsx:71`) with
  no provider id attached; wiring them would mean inventing and maintaining a
  prefix → provider-id table whose mistakes write the wrong config key.
- A general per-provider settings editor (`ssl_ca_cert`, `extra_headers`,
  `api_mode`). Those stay YAML-only.
- Preserving comments in `config.yaml`. Every dashboard write goes through
  `save_config` → `atomic_yaml_write`, which re-dumps the document. Fixing that
  means rewriting the config write layer.

## Where it lives

`OAuthProvidersCard` on the Env page (`EnvPage.tsx:938`), which renders one row
per OAuth-capable provider — `openai-codex`, `anthropic`, `qwen-oauth`,
`xai-oauth`, `nous`, … — from `GET /api/providers/oauth`. Each row already
carries the provider's real `id`, which *is* the `providers:` config key. That
exact-id property is what makes the write safe, and it is what the Env page's
API-key groups lack.

Routes are named provider-agnostically (`/api/providers/{id}/proxy`) rather than
nested under `/oauth`, so an API-key provider UI can reuse them later without a
backend change. Only the read piggybacks on the OAuth payload, because the card
already fetches it and a second round-trip would buy nothing.

## Read contract

`GET /api/providers/oauth` gains one field per provider:

```json
{ "id": "openai-codex",
  "name": "OpenAI Codex",
  "status": { "logged_in": true, "...": "..." },
  "proxy": { "mode": "url", "url": "http://127.0.0.1:7890" } }
```

`mode` is `"inherit" | "direct" | "url"`, mapping the three configured states:
key absent → `inherit`, `false` → `direct`, a URL string → `url`.

**The value must be read straight from `config["providers"][id]["proxy"]`, not
through `resolve_provider_proxy()`.** The resolver answers "which proxy should
this *request* use", and when a provider carries no `proxy` key it falls through
to base_url reverse lookup — so a provider with nothing configured would display
a neighbouring provider's proxy. The UI is asking a different question: "what
did this provider itself declare". Same words, different question.

`url` is redacted when it carries credentials: `http://user:pass@host:3128` is
returned as `http://***@host:3128` (`utils.redact_proxy_url`). A URL without
credentials is returned verbatim — a bare `host:port` is not a secret, and
hiding it would make the field unusable.

## Write contract

`PUT /api/providers/{id}/proxy`, body `{ "mode": ..., "url": ... }`.

| mode | Action |
|---|---|
| `inherit` | Delete `providers.<id>.proxy`. If the entry is then empty AND has no `base_url`/`model` (i.e. it is not a user-defined endpoint), delete the entry too — no `anthropic: {}` shells left behind. |
| `direct` | Write `false`. |
| `url` | Validate through `_coerce_provider_proxy()`, then write the normalized string. |

`url` is read only when `mode` is `"url"`; it is ignored for the other two modes
rather than rejected, so the frontend can keep a half-typed address in the box
while the user flips the select.

Validation reuses the resolver's coercion so the dashboard and the agent accept
exactly the same values and produce the same (already credential-safe) error
messages. A malformed value returns 400; it is never silently downgraded to
direct.

The entry is **merged, never rebuilt** — `entry = dict(existing)`, then the
`proxy` key is set or removed. This mirrors `_write_custom_endpoint`
(`web_server.py:7725`), whose comment records what rebuilding cost: hand-written
`api_mode` / `key_env` / `extra_headers` silently dropped on an unrelated edit,
leaving a provider that no longer authenticated.

Writes run inside `_profile_scope(profile)` and end in `save_config(cfg)`, like
every other provider write.

Two guards:

- **Id allowlist.** Accept only ids present in `PROVIDER_REGISTRY` or already
  present as a `providers:` key. Without it the route is "write an arbitrary key
  into config.yaml".
- **`_require_token(request)`**, matching `DELETE /api/providers/oauth/{id}`.
  This is a config write, and the value can embed credentials.
- A submitted URL whose userinfo is `***` (i.e. the redacted form was sent back
  unchanged) is rejected with 400 asking for the full address, so `***` can
  never be persisted.

## Test contract

`POST /api/providers/{id}/proxy/test`, same body shape. It tests the *pending*
value, not the saved one — testing after saving inverts the point.

The probe target is derived from `PROVIDER_REGISTRY[id].inference_base_url` +
`/models`; it is never taken from the request, or the route becomes "make the
server fetch an arbitrary URL". No credentials are sent: this measures
reachability, not authentication.

All three modes are testable and the button stays enabled for each — the probe
dials exactly what that mode would produce at request time: `url` through the
submitted proxy, `direct` with `trust_env=False`, `inherit` through
`provider_proxy_httpx_kwargs`' unconfigured path (whatever the environment
variables resolve to). Testing `direct` is how an operator discovers that
`api.anthropic.com` answers 403 without a proxy — which is the whole reason the
`http` classification exists.

The response is classified by `kind`, not by a bare boolean:

| Outcome | `kind` | Meaning |
|---|---|---|
| Transport error (connection refused, timeout, proxy error) | `transport_error` | Proxy down, wrong port, wrong scheme. |
| HTTP 200 / 401 | `reachable` | 401 is the expected "arrived, sent no credentials". |
| Any other status | `http` | Answered, but judge the code yourself. |

The third row is not padding. `api.anthropic.com` answers **403** on a direct
connection from a blocked region — a real HTTP response. "Any response means
success" would paint a misconfigured direct setting green. `ok` is exposed as an
alias for `kind == "reachable"`.

Timeouts: 5 s connect, 10 s total — the dashboard must not hang on it. `detail`
runs through `redact_proxy_url` before it reaches the browser or a log line.
`_require_token` applies here too: the target URL is fixed, but the proxy the
server is told to dial is user input.

## Frontend

Each `OAuthProvidersCard` row gets a collapsed proxy editor. Collapsed state:

- proxy configured → a small badge, `代理 127.0.0.1:7890` (redacted form)
- forced direct → badge `直连`
- unconfigured → an unobtrusive "代理" link

The card lists every OAuth provider, so a permanently-expanded control on each
row would drown the login status that is the card's actual job.

Expanded: a three-option select (`跟随环境变量` / `强制直连` / `自定义代理`), a URL
input rendered only for the third, and `[测试] [保存]`. Save enables on change;
the test result renders inline underneath, styled per `kind` (✓ / ⚠ / ✗).

Supporting changes:

- `web/src/lib/api.ts`: `proxy` on the `OAuthProvider` interface, plus
  `setProviderProxy()` and `testProviderProxy()`.
- `web/src/lib/provider-proxy.ts` (new): the pure mapping between the three-state
  control and the wire value, plus redacted-value detection. Logic lives here
  because that is where this repo's frontend tests live.
- i18n: new keys in `types.ts`, `en.ts`, `zh.ts` only. The other 16 locales are
  partial and fall back to English by construction (`define-locale.ts`).
- After a successful save the card refetches, as it already does after
  login/disconnect, so the badge updates.
- The success toast carries two facts: the setting applies to newly built
  clients (no restart — the config cache keys on the file's mtime+size, so any
  new client picks it up), and saving re-dumps `config.yaml`, dropping comments.

## Testing

Backend — `tests/hermes_cli/test_provider_proxy_api.py` (new), run in a
container per `CLAUDE.md`:

- Read: each of the three states maps correctly; plus the cross-contamination
  regression — a provider with no `proxy` key reports `inherit` even when
  another provider configures a proxy for the same host. This pins the resolver
  trap described above.
- Write: `url` / `direct` / `inherit` each produce the right on-disk shape;
  `inherit` removes an emptied entry but leaves a user-defined endpoint entry
  with a `base_url` intact; an entry carrying `extra_headers` / `api_mode`
  survives a proxy edit unchanged; malformed URL returns 400 with no credentials
  in the message; unknown id is rejected and writes nothing; a redacted `***@`
  value is rejected; missing token is rejected.
- Test route: one case per `kind`; the probe host comes from the registry and a
  request-supplied URL is ignored; the proxy kwargs actually reach httpx;
  `detail` is redacted.

Frontend — vitest for `web/src/lib/provider-proxy.ts`. No component test: this
repo has none (`web/src/**/*.test.ts` covers `lib/` only), and inventing a
component-testing setup for one card is out of scope.

Verification: pytest, eslint, tsc, and vitest all run inside a container.

## Deployment cost

The dashboard bundle is built into the image (`Dockerfile:279`,
`cd web && npm run build` → `hermes_cli/web_dist`). Any change here — frontend or
backend — requires an image rebuild and a container recreate before it is
visible in the browser.

## Decisions made during design

- **OAuth providers only.** Their rows carry a real provider id that equals the
  config key. The Env page's API-key groups are keyed by env-var prefix, so
  covering them means a fuzzy prefix → id table (`DASHSCOPE_` → `alibaba` or
  `alibaba-coding-plan`?) whose errors write the wrong config key.
- **Three-state select over a single text box.** The config has three states and
  the editor must round-trip all of them. A text box would show a hand-written
  `proxy: false` as empty and erase it on the next save.
- **Rejected: no new endpoints, write through `PUT /api/config`.** It deep-merges
  incoming config over what is on disk (`web_server.py:7317`), and a merge cannot
  remove a key — so "go back to following the environment variables" would be
  inexpressible. That is one of the three states, so the approach fails on its
  own terms.

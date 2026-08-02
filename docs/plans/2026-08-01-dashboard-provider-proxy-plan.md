# Dashboard Per-Provider Proxy Implementation Plan

> **STATUS (2026-08-01): implemented directly, not task-by-task.** The feature
> is built and verified in-container; see the "As built" section of
> `docs/design/dashboard-provider-proxy.md` for what actually landed and the
> three deviations. This file is kept for the reasoning it records — the
> container verification commands, the baselines that distinguish a real
> failure from a pre-existing one, and the Task 1 rationale — not as a to-do
> list. Only the deployment step below is outstanding.

**Goal:** Let an operator set `providers.<id>.proxy` (inherit / direct / URL) for every OAuth provider from the dashboard's Env page, and verify the setting reaches the provider before saving.

**Architecture:** A new pure-Python module `hermes_cli/provider_proxy_admin.py` holds the read/write/probe logic against a config dict, so it is unit-testable without a TestClient. `hermes_cli/web_server.py` adds three thin routes that do token checks, profile scoping, `load_config`/`save_config`, and error mapping. On the frontend, a new pure module `web/src/lib/provider-proxy.ts` holds the three-state mapping (this repo's frontend tests only cover `lib/`), and a new `ProviderProxyEditor` component renders a collapsed badge / expanded editor inside each `OAuthProvidersCard` row.

**Tech Stack:** Python 3.13 + FastAPI/Starlette + httpx + PyYAML; React 19 + TypeScript + vitest + Tailwind + `@nous-research/ui`.

**Design source:** `docs/design/dashboard-provider-proxy.md` (approved 2026-07-31). Builds on `docs/design/per-provider-proxy.md`, whose config layer is already implemented in the working tree (uncommitted).

---

## Global Constraints

- **All verification runs in a container, never on the host** (`CLAUDE.md`). No `pytest`, `npm`, `npx`, `vitest`, `tsc`, or `eslint` on the host shell.
- **Commit messages must carry no AI attribution trailer** of any kind (`CLAUDE.md`). No `Co-Authored-By: Claude`, no `Generated with`.
- **`git commit` commits the whole index, not just what you just `git add`ed.** `.gitignore` and `Dockerfile` are already staged in this working tree and belong to the user, not to this feature. Before every commit run `git diff --cached --name-only` and confirm it lists only this feature's files. If `.gitignore` or `Dockerfile` appear, `git restore --staged .gitignore Dockerfile` before committing.
- **Branch:** `personal`. Repo root: `/mnt/main/CodeSpace/Project/hermes-agent`.
- **The config-layer work is uncommitted.** 16 modified files + 7 new test files are in the working tree from the previous task. Never `git add -A` / `git add .`; always add explicit paths.
- **Proxy URLs may embed credentials.** Never log or return a raw proxy URL — always pass it through `utils.redact_proxy_url` first.
- **Mode vocabulary is fixed:** `"inherit" | "direct" | "url"`, mapping to config states *key absent* / `false` / URL string. These exact strings appear on the wire, in Python, and in TypeScript.
- **Route names are provider-agnostic:** `/api/providers/{provider_id}/proxy` and `/api/providers/{provider_id}/proxy/test` — not nested under `/oauth`, so an API-key provider UI can reuse them later.

---

## Verification Commands

Both images already exist. `localhost/hermes-test` = `hermes-agent` + pytest, and it also carries Node 22, npm, and a populated `/opt/hermes/web/node_modules`.

**Backend (pytest):**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_provider_proxy_api.py
```

`HERMES_WRITE_SAFE_ROOT=` must be passed empty — the value baked into the image makes tests that write under `/tmp` fail.

**Frontend (vitest / tsc / eslint).** The host has no `web/node_modules` and must not grow one, so mount the repo read-only, copy the sources over the image's checkout, and use the image's `node_modules`:

```bash
docker run --rm -v /mnt/main/CodeSpace/Project/hermes-agent:/src:ro -e HOME=/tmp \
  --entrypoint sh localhost/hermes-test -c \
  "cp -r /src/web/src/. /opt/hermes/web/src/ && cd /opt/hermes/web && npx vitest run src/lib/provider-proxy.test.ts"
```

Swap the last command for `npm run typecheck` or `npx eslint <paths>` as needed. This copies `web/src` only — it does not pick up `package.json` changes, and this feature adds no dependencies.

**Baselines to compare against, so you don't misread a pre-existing failure as yours:**

- `npx eslint src/components/OAuthProvidersCard.tsx` reports **2 warnings, 0 errors** today (`react-hooks/set-state-in-effect`). Leave them; do not "fix" them in this feature.
- `npm run typecheck` is clean today.
- `tests/agent` + `tests/run_agent` run *together* produce ~244 failures on this branch and 245 on a clean HEAD — a pre-existing test-isolation problem (a torn-down temporary `HERMES_HOME` leaves logging handlers unable to open their files), not a regression. Run one directory at a time.

---

## File Structure

**Create:**

| File | Responsibility |
|---|---|
| `hermes_cli/provider_proxy_admin.py` | All dashboard-side proxy logic as pure functions over a config dict: which providers are editable, reading the declared state, applying a write, probing reachability. No FastAPI imports. |
| `tests/hermes_cli/test_provider_proxy_api.py` | Backend tests — the pure module *and* the three routes via `starlette.testclient`. |
| `web/src/lib/provider-proxy.ts` | Pure mapping between the three-state control and the wire value, plus redacted-URL detection and badge labelling. |
| `web/src/lib/provider-proxy.test.ts` | vitest for the above. |
| `web/src/components/ProviderProxyEditor.tsx` | The collapsed badge / expanded editor for one provider row. |

**Modify:**

| File | Change |
|---|---|
| `hermes_cli/web_server.py` | `list_oauth_providers` gains a `proxy` field; two new routes (`PUT .../proxy`, `POST .../proxy/test`) plus a `ProviderProxyUpdate` body model. |
| `web/src/lib/api.ts` | `proxy` on `OAuthProvider`; `setProviderProxy()` and `testProviderProxy()`. |
| `web/src/lib/api.test.ts` | Cases for the two new client methods. |
| `web/src/i18n/types.ts` / `en.ts` / `zh.ts` | New `oauth.proxy.*` keys. The other 16 locales are partial by construction (`define-locale.ts` merges over English) and are deliberately left alone. |
| `web/src/components/OAuthProvidersCard.tsx` | Render `<ProviderProxyEditor>` per row; refetch after a successful save. |
| `docs/design/dashboard-provider-proxy.md` | An "As built" section recording deviations. |

**Why a separate `provider_proxy_admin.py`:** `web_server.py` is ~16k lines. Keeping the logic out of it means the read/write/probe rules can be tested by calling functions on a dict, with only the thin HTTP contract needing a TestClient.

### Deviations from the design doc (decide once, here)

1. **`claude-code` has no proxy editor.** The OAuth catalog contains 8 ids; `PROVIDER_REGISTRY` contains 34. Every catalog id is in the registry **except `claude-code`**, which is a synthetic read-only subscription row with no `inference_base_url` — nothing to configure and nothing to probe. The read contract therefore returns `"proxy": null` for ids that fail the allowlist, and the frontend renders no editor when `proxy` is null. The design doc's allowlist guard already implies this; this makes the read side agree with it instead of offering a control that would 400 on save.
2. **A malformed on-disk `proxy` value must not 500 the Accounts tab.** `_coerce_provider_proxy` raises on e.g. `proxy: true`. The read path catches that and returns `{"mode": "url", "url": null, "invalid": true}`, so the card still renders and the operator can retype the value. Without this, one bad hand-edit takes down the whole provider list.

---

### Task 1: Read side of `provider_proxy_admin`

**Files:**
- Create: `hermes_cli/provider_proxy_admin.py`
- Test: `tests/hermes_cli/test_provider_proxy_api.py`

**Interfaces:**
- Consumes: `hermes_cli.config._coerce_provider_proxy(value, provider_key)` (existing, `config.py:5671`) — returns a normalized URL string, `False`, or `None`, and raises `ValueError` on a malformed value. `utils.redact_proxy_url(value)` (existing, `utils.py:502`) — `http://u:p@h:3128` → `http://***@h:3128`, `False` → `"direct"`. `hermes_cli.auth.PROVIDER_REGISTRY` (existing, `auth.py:176`) — dict of 34 provider ids.
- Produces:
  - `PROXY_MODES: tuple[str, ...]` == `("inherit", "direct", "url")`
  - `provider_proxy_editable(config: Dict[str, Any], provider_id: str) -> bool`
  - `is_redacted_proxy_url(value: Any) -> bool`
  - `read_provider_proxy_state(config: Dict[str, Any], provider_id: str) -> Optional[Dict[str, Any]]` — `{"mode": ..., "url": ...}`, plus `"invalid": True` for an unparseable stored value; `None` when the id is not editable.

- [ ] **Step 1: Write the failing test**

Create `tests/hermes_cli/test_provider_proxy_api.py`:

```python
"""Tests for the dashboard's per-provider proxy surface.

Covers hermes_cli.provider_proxy_admin (pure, at the config-dict level) and the
routes in web_server that expose it. The cross-contamination case matters most:
the UI asks "what did this provider declare", not "which proxy would a request
use", and resolve_provider_proxy answers only the second question.
"""

import pytest


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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker run --rm --network host \
  -v /mnt/main/CodeSpace/Project/hermes-agent:/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp -e HERMES_WRITE_SAFE_ROOT= \
  --entrypoint python3 localhost/hermes-test \
  -m pytest -q tests/hermes_cli/test_provider_proxy_api.py
```

Expected: every test errors with `ModuleNotFoundError: No module named 'hermes_cli.provider_proxy_admin'`.

- [ ] **Step 3: Write the implementation**

Create `hermes_cli/provider_proxy_admin.py`:

```python
"""Dashboard-side logic for ``providers.<id>.proxy``.

Pure functions over a config dict (plus, later, one network probe), kept out of
``web_server.py`` so the rules can be tested without an HTTP client. The routes
there stay thin: token check, profile scope, load/save, error mapping.

SECURITY: proxy URLs carry credentials as routinely as ``extra_headers`` do.
Every value leaving this module for a browser or a log line goes through
``utils.redact_proxy_url`` first.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from utils import redact_proxy_url

# The three configured states, spelled the same way on the wire, in Python and
# in TypeScript: key absent / ``false`` / a URL string.
PROXY_MODES: tuple[str, ...] = ("inherit", "direct", "url")


def _providers_block(config: Dict[str, Any]) -> Dict[str, Any]:
    providers = config.get("providers") if isinstance(config, dict) else None
    return providers if isinstance(providers, dict) else {}


def provider_proxy_editable(config: Dict[str, Any], provider_id: str) -> bool:
    """True when *provider_id* is a key we may write into ``providers:``.

    Without this guard the routes reduce to "write an arbitrary key into
    config.yaml". Accepts a built-in provider or a key the operator already
    created; notably not the synthetic ``claude-code`` catalog row, which owns
    no config entry and no inference host.
    """
    key = str(provider_id or "").strip()
    if not key:
        return False
    if key in _providers_block(config):
        return True
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        return False
    return key in PROVIDER_REGISTRY


def is_redacted_proxy_url(value: Any) -> bool:
    """True when *value* is the ``scheme://***@host`` form we hand the browser.

    Submitting it back unchanged would persist ``***`` as the username, so the
    write path rejects it and asks for the full address instead.
    """
    candidate = str(value or "").strip()
    if not candidate:
        return False
    try:
        netloc = urlparse(candidate).netloc
    except ValueError:
        return False
    if "@" not in netloc:
        return False
    return netloc.rsplit("@", 1)[0] == "***"


def read_provider_proxy_state(
    config: Dict[str, Any], provider_id: str
) -> Optional[Dict[str, Any]]:
    """What *provider_id* itself declares, for display in the dashboard.

    Returns ``None`` when the id has no editable config key.

    This is deliberately NOT ``resolve_provider_proxy``. That answers "which
    proxy should this request use" and, for a provider declaring nothing, falls
    through to a base_url reverse lookup — so the UI would show a neighbouring
    provider's proxy as if this provider had configured it. Same words,
    different question.
    """
    if not provider_proxy_editable(config, provider_id):
        return None
    entry = _providers_block(config).get(str(provider_id).strip())
    if not isinstance(entry, dict) or "proxy" not in entry:
        return {"mode": "inherit", "url": None}

    from hermes_cli.config import _coerce_provider_proxy

    try:
        coerced = _coerce_provider_proxy(entry["proxy"], str(provider_id))
    except ValueError:
        # A hand-edited `proxy: true` must not take down the whole card. Show
        # it as a URL needing re-entry rather than raising through the route.
        return {"mode": "url", "url": None, "invalid": True}
    if coerced is None:
        return {"mode": "inherit", "url": None}
    if coerced is False:
        return {"mode": "direct", "url": None}
    return {"mode": "url", "url": redact_proxy_url(coerced)}
```

- [ ] **Step 4: Run the test to verify it passes**

Same command as Step 2. Expected: `15 passed` (4 editability + 2 redaction-detection + 9 read-state cases).

- [ ] **Step 5: Commit**

```bash
cd /mnt/main/CodeSpace/Project/hermes-agent
git restore --staged .gitignore Dockerfile 2>/dev/null || true
git add hermes_cli/provider_proxy_admin.py tests/hermes_cli/test_provider_proxy_api.py
git diff --cached --name-only   # MUST list exactly those two files
git commit -m "feat(dashboard): read a provider's declared proxy state"
```

---
sidebar_position: 1
title: "Architecture"
description: "Hermes Agent internals — major subsystems, execution paths, data flow, and where to read next"
---

# Architecture

This page is the top-level map of Hermes Agent internals. Use it to orient yourself in the codebase, then dive into subsystem-specific docs for implementation details.

## System Overview

```text
┌─────────────────────────────────────────────────────────────────────┐
│                        Entry Points                                  │
│                                                                      │
│  CLI (cli.py)    Gateway (gateway/run.py)    ACP (acp_adapter/)     │
│  Batch Runner    API Server                  Python Library          │
└──────────┬──────────────┬───────────────────────┬───────────────────┘
           │              │                       │
           ▼              ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     AIAgent (run_agent.py)                          │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ Prompt       │  │ Provider     │  │ Tool         │               │
│  │ Builder      │  │ Resolution   │  │ Dispatch     │               │
│  │ (prompt_     │  │ (runtime_    │  │ (model_      │               │
│  │  builder.py) │  │  provider.py)│  │  tools.py)   │               │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘               │
│         │                 │                 │                       │
│  ┌──────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐               │
│  │ Compression  │  │ 3 API Modes  │  │ Tool Registry│               │
│  │ & Caching    │  │ chat_compl.  │  │ (registry.py)│               │
│  │              │  │ codex_resp.  │  │ 70+ tools    │               │
│  │              │  │ anthropic    │  │ 28 toolsets  │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└─────────┴─────────────────┴─────────────────┴───────────────────────┘
           │                                    │
           ▼                                    ▼
┌───────────────────┐              ┌──────────────────────┐
│ Session Storage   │              │ Tool Backends         │
│ (SQLite + FTS5)   │              │ Terminal (6 backends) │
│ hermes_state.py   │              │ Browser (5 backends)  │
│ gateway/session.py│              │ Web (4 backends)      │
└───────────────────┘              │ MCP (dynamic)         │
                                   │ File, Vision, etc.    │
                                   └──────────────────────┘
```

## Directory Structure

```text
hermes-agent/
├── run_agent.py              # AIAgent — core conversation loop (large file)
├── cli.py                    # HermesCLI — interactive terminal UI (large file)
├── model_tools.py            # Tool discovery, schema collection, dispatch
├── toolsets.py               # Tool groupings and platform presets
├── hermes_state.py           # SQLite session/state database with FTS5
├── hermes_constants.py       # HERMES_HOME, profile-aware paths
├── batch_runner.py           # Batch trajectory generation
│
├── agent/                    # Agent internals
│   ├── prompt_builder.py     # System prompt assembly
│   ├── context_engine.py     # ContextEngine ABC (pluggable)
│   ├── context_compressor.py # Default engine — lossy summarization
│   ├── prompt_caching.py     # Anthropic prompt caching
│   ├── auxiliary_client.py   # Auxiliary LLM for side tasks (vision, summarization)
│   ├── model_metadata.py     # Model context lengths, token estimation
│   ├── models_dev.py         # models.dev registry integration
│   ├── anthropic_adapter.py  # Anthropic Messages API format conversion
│   ├── display.py            # KawaiiSpinner, tool preview formatting
│   ├── skill_commands.py     # Skill slash commands
│   ├── memory_manager.py    # Memory manager orchestration
│   ├── memory_provider.py   # Memory provider ABC
│   └── trajectory.py         # Trajectory saving helpers
│
├── hermes_cli/               # CLI subcommands and setup
│   ├── main.py               # Entry point — all `hermes` subcommands (large file)
│   ├── config.py             # DEFAULT_CONFIG, OPTIONAL_ENV_VARS, migration
│   ├── commands.py           # COMMAND_REGISTRY — central slash command definitions
│   ├── auth.py               # PROVIDER_REGISTRY, credential resolution
│   ├── runtime_provider.py   # Provider → api_mode + credentials
│   ├── models.py             # Model catalog, provider model lists
│   ├── model_switch.py       # /model command logic (CLI + gateway shared)
│   ├── setup.py              # Interactive setup wizard (large file)
│   ├── skin_engine.py        # CLI theming engine
│   ├── skills_config.py      # hermes skills — enable/disable per platform
│   ├── skills_hub.py         # /skills slash command
│   ├── tools_config.py       # hermes tools — enable/disable per platform
│   ├── plugins.py            # PluginManager — discovery, loading, hooks
│   ├── callbacks.py          # Terminal callbacks (clarify, sudo, approval)
│   └── gateway.py            # hermes gateway start/stop
│
├── tools/                    # Tool implementations (one file per tool)
│   ├── registry.py           # Central tool registry
│   ├── approval.py           # Dangerous command detection
│   ├── terminal_tool.py      # Terminal orchestration
│   ├── process_registry.py   # Background process management
│   ├── file_tools.py         # read_file, write_file, patch, search_files
│   ├── web_tools.py          # web_search, web_extract
│   ├── browser_tool.py       # 10 browser automation tools
│   ├── code_execution_tool.py # execute_code sandbox
│   ├── delegate_tool.py      # Subagent delegation
│   ├── mcp_tool.py           # MCP client (large file)
│   ├── credential_files.py   # File-based credential passthrough
│   ├── env_passthrough.py    # Env var passthrough for sandboxes
│   ├── ansi_strip.py         # ANSI escape stripping
│   └── environments/         # Terminal backends (local, docker, ssh, modal, daytona, singularity)
│
├── gateway/                  # Messaging platform gateway
│   ├── run.py                # GatewayRunner — message dispatch (large file)
│   ├── session.py            # SessionStore — conversation persistence
│   ├── delivery.py           # Outbound message delivery
│   ├── pairing.py            # DM pairing authorization
│   ├── hooks.py              # Hook discovery and lifecycle events
│   ├── mirror.py             # Cross-session message mirroring
│   ├── status.py             # Token locks, profile-scoped process tracking
│   ├── builtin_hooks/        # Extension point for always-registered hooks (none shipped)
│   └── platforms/            # Built-in adapters: signal, weixin, bluebubbles,
│                             #   qqbot, whatsapp_cloud, yuanbao, webhook, api_server
│
├── plugins/platforms/        # Bundled platform plugins: telegram, discord, slack,
│                             #   whatsapp, matrix, mattermost, email, sms, dingtalk,
│                             #   feishu, wecom, homeassistant, irc, line, teams,
│                             #   google_chat, buzz, ntfy, photon, raft, simplex
│
├── acp_adapter/              # ACP server (VS Code / Zed / JetBrains)
├── cron/                     # Scheduler (jobs.py, scheduler.py)
├── plugins/memory/           # Memory provider plugins
├── plugins/context_engine/   # Context engine plugins
├── skills/                   # Bundled skills (always available)
├── optional-skills/          # Official optional skills (install explicitly)
├── website/                  # Docusaurus documentation site
└── tests/                    # Pytest suite (~25,000 tests across ~1,250 files)
```

## Data Flow

### CLI Session

```text
User input → HermesCLI.process_input()
  → AIAgent.run_conversation()
    → prompt_builder.build_system_prompt()
    → runtime_provider.resolve_runtime_provider()
    → API call (chat_completions / codex_responses / anthropic_messages)
    → tool_calls? → model_tools.handle_function_call() → loop
    → final response → display → save to SessionDB
```

### Gateway Message

```text
Platform event → Adapter.on_message() → MessageEvent
  → GatewayRunner._handle_message()
    → authorize user
    → resolve session key
    → create AIAgent with session history
    → AIAgent.run_conversation()
    → deliver response back through adapter
```

### Cron Job

```text
Scheduler tick → load due jobs from jobs.json
  → create fresh AIAgent (no history)
  → inject attached skills as context
  → run job prompt
  → deliver response to target platform
  → update job state and next_run
```

## Recommended Reading Order

If you are new to the codebase:

1. **This page** — orient yourself
2. **[Agent Loop Internals](./agent-loop.md)** — how AIAgent works
3. **[Prompt Assembly](./prompt-assembly.md)** — system prompt construction
4. **[Provider Runtime Resolution](./provider-runtime.md)** — how providers are selected
5. **[Adding Providers](./adding-providers.md)** — practical guide to adding a new provider
6. **[Tools Runtime](./tools-runtime.md)** — tool registry, dispatch, environments
7. **[Session Storage](./session-storage.md)** — SQLite schema, FTS5, session lineage
8. **[Gateway Internals](./gateway-internals.md)** — messaging platform gateway
9. **[Context Compression & Prompt Caching](./context-compression-and-caching.md)** — compression and caching
10. **[ACP Internals](./acp-internals.md)** — IDE integration

## Major Subsystems

### Agent Loop

The synchronous orchestration engine (`AIAgent` in `run_agent.py`). Handles provider selection, prompt construction, tool execution, retries, fallback, callbacks, compression, and persistence. Supports three API modes for different provider backends.

→ [Agent Loop Internals](./agent-loop.md)

### Prompt System

Prompt construction and maintenance across the conversation lifecycle:

- **`system_prompt.py` + `prompt_builder.py`** — assembles the ordered system-prompt tiers (`stable` → `context` → `volatile`): identity/tool guidance/skills, context files, then memory/profile/timestamp blocks
- **`prompt_caching.py`** — Applies Anthropic cache breakpoints for prefix caching
- **`context_compressor.py`** — Summarizes middle conversation turns when context exceeds thresholds

→ [Prompt Assembly](./prompt-assembly.md), [Context Compression & Prompt Caching](./context-compression-and-caching.md)

### Provider Resolution

A shared runtime resolver used by CLI, gateway, cron, ACP, and auxiliary calls. Maps `(provider, model)` tuples to `(api_mode, api_key, base_url)`. Handles 18+ providers, OAuth flows, credential pools, and alias resolution.

→ [Provider Runtime Resolution](./provider-runtime.md)

### Tool System

Central tool registry (`tools/registry.py`) with 70+ registered tools across ~28 toolsets. Each tool file self-registers at import time. The registry handles schema collection, dispatch, availability checking, and error wrapping. Terminal tools support 7 backends (local, Docker, SSH, Daytona, Modal, Singularity, Vercel Sandbox).

→ [Tools Runtime](./tools-runtime.md)

### Session Persistence

SQLite-based session storage with FTS5 full-text search. Sessions have lineage tracking (parent/child across compressions), per-platform isolation, and atomic writes with contention handling.

→ [Session Storage](./session-storage.md)

### Messaging Gateway

Long-running process with 25+ platform adapters (built-in + bundled plugins), unified session routing, user authorization (allowlists + DM pairing), slash command dispatch, hook system, cron ticking, and background maintenance.

→ [Gateway Internals](./gateway-internals.md)

### Plugin System

Three discovery sources: `~/.hermes/plugins/` (user), `.hermes/plugins/` (project), and pip entry points. Plugins register tools, hooks, and CLI commands through a context API. Two specialized plugin types exist: memory providers (`plugins/memory/`) and context engines (`plugins/context_engine/`). Both are single-select — only one of each can be active at a time, configured via `hermes plugins` or `config.yaml`.

→ [Plugin Guide](/developer-guide/plugins), [Memory Provider Plugin](./memory-provider-plugin.md)

### Cron

First-class agent tasks (not shell tasks). Jobs store in JSON, support multiple schedule formats, can attach skills and scripts, and deliver to any platform.

→ [Cron Internals](./cron-internals.md)

### ACP Integration

Exposes Hermes as an editor-native agent over stdio/JSON-RPC for VS Code, Zed, and JetBrains.

→ [ACP Internals](./acp-internals.md)

### Trajectories

Generates ShareGPT-format trajectories from agent sessions for training data generation.

→ [Trajectories & Training Format](./trajectory-format.md)

## Design Principles

| Principle | What it means in practice |
|-----------|--------------------------|
| **Prompt stability** | System prompt doesn't change mid-conversation. No cache-breaking mutations except explicit user actions (`/model`). |
| **Observable execution** | Every tool call is visible to the user via callbacks. Progress updates in CLI (spinner) and gateway (chat messages). |
| **Interruptible** | API calls and tool execution can be cancelled mid-flight by user input or signals. |
| **Platform-agnostic core** | One AIAgent class serves CLI, gateway, ACP, batch, and API server. Platform differences live in the entry point, not the agent. |
| **Loose coupling** | Optional subsystems (MCP, plugins, memory providers, RL environments) use registry patterns and check_fn gating, not hard dependencies. |
| **Profile isolation** | Each profile (`hermes -p <name>`) gets its own HERMES_HOME, config, memory, sessions, and gateway PID. Multiple profiles run concurrently. |

## File Dependency Chain

```text
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

This chain means tool registration happens at import time, before any agent instance is created. Any `tools/*.py` file with a top-level `registry.register()` call is auto-discovered — no manual import list needed.

## Rich chat presentation and browser attachments (UI-P5)

The experimental Web Native surface and Electron Desktop share the portable
`apps/chat-ui` package. Desktop retains its Electron/filesystem adapter and
preview rail; Web uses authenticated HTTP resources, owned object URLs, and
source-only HTML/SVG previews. The shared package owns data contracts, Markdown
processing, math/block caches, lazy code highlighting, artifact detection and
diff parsing. Dependency tests reject host imports in the shared package.
Terminal remains the default dashboard surface and `/api/pty` is unchanged.
This phase does not implement UI-P6/P7 or alter model tool schemas, prompt
caching, approvals or session recovery semantics.

### Live and durable display data

The Gateway tool-complete callback produces optional version-1 `presentation`
data from structured tool results and existing edit snapshots. It emits the
presentation with the real tool-call ID and merges it into that exact session's
persisted tool row `display_metadata`, preserving other sidecar fields. No
SessionDB migration or change to model-facing results is needed. Sensitive
output redaction still applies. Unknown tools keep their generic result view;
old history without reliable metadata does not gain a fabricated diff. Completed
turns also receive content source IDs from their durable assistant rows; these
are returned in completion events and retained across partial history pages.

Native session activation/resume still restores runtime state. Transcript rows
come from authenticated `GET /api/sessions/{id}/messages?view=display&include_compacted=true&order=latest&limit=100`;
`before_id` provides stable backward pagination without offset races. During
hydration, WS events are buffered, then reconciled with durable rows using row,
turn and real tool-call IDs. Complete-before-start and repeated completion
update the same tool. Profile, runtime, stored ID, connection generation and
request version invalidate stale reads. The backend's resolved stored ID is
adopted after session compaction. Attachment history uses persisted references;
unavailable resources stay unavailable instead of being uploaded again.

The opt-in `view=display` selects the backend's compression-only ancestor chain
through the resolved tip. It uses the existing parent walk and compression/fork
discriminators; branches own their copied history, and delegate/tool/reset links
do not pull unrelated parent messages into the display. A branch's compression
continuation can include its branch segment without crossing the original fork.
The legacy REST view still returns one resolved segment. Runtime resume/activate
and model history are unchanged; Native continues to omit the WS transcript.

Display pages retain raw row IDs, source session IDs, tool calls/results,
reasoning and display/API sidecars. SQL applies the existing per-session
compaction-copy preference (live row, then newest generation) and replayed-user
dedupe before the keyset predicate and LIMIT. Only the selected rows are fetched
and decoded, with a server cap of 500; no full-lineage conversation projection
is materialized for a page. `include_compacted` controls preserved in-place rows
independently of ancestor selection. Rows return in insertion-ID order, and
`before_id` crosses segment boundaries without duplicates or OFFSET races with
new appends. The display view rejects nonzero OFFSET. The client never infers
lineage or deduplicates turns by matching their text.

Backward paging is enabled only after the latest page for the current stored
session has hydrated successfully. A transient reconnect history failure keeps
completed display rows and reconciles current inflight/control state, with a
non-destructive retry notice. Retry reads latest without `before_id`, rebuilding
the cursor before older pages are allowed. Stored-ID rotation resets the cache;
late or concurrent requests cannot reuse a predecessor's cursor or overwrite a
newer conversation. None of these read retries resubmits a prompt.

### Attachment ownership and protocol

`tui_gateway/attachments.py` owns an in-memory draft ledger. A draft binds an
authenticated principal, profile, runtime session and owning WS transport.
Selecting the same stored session does not grant access to another draft.
Files use generated names in the existing profile `attachments/web-drafts` or
`images/web-drafts` roots, not managed-file identities.

| Operation | Contract |
| --- | --- |
| `attachment.prepare` RPC | Create a draft or register an occurrence/request; return public metadata and a five-minute upload grant. |
| `attachment.connection` RPC | Mint a one-use, one-minute handoff to authenticated HTTP for this WS owner. |
| `POST /api/chat/attachments/{draft}/recover` | Bootstrap an HttpOnly, SameSite=Strict recovery cookie, or recover after the prior owner disconnected. Rotate in-memory authority for a new owner. |
| `PUT /api/chat/attachments/{draft}/{attachment}` | Authenticate both dashboard principal and draft/runtime/upload grants; stream and atomically finalize bytes. |
| `attachment.snapshot` RPC | Query local/uploading/uploaded/submitted/failed/cancelled occurrences, including accepted turn IDs. |
| `attachment.cancel` RPC | Idempotently cancel an unclaimed occurrence; retain a tombstone and preserve claimed files. |
| `prompt.submit` with `attachment_ids` | Under the existing session lock, reject busy submissions and atomically claim exactly the specified draft attachments for one turn. |

Grants travel in headers or authenticated RPC data, never URL parameters or
browser storage. Session storage contains only non-secret draft/runtime
locators. Cookie paths honor the dashboard base path; HTTPS marks them Secure.
HTTP and WS must identify the same principal. A live owner prevents takeover.
Explicit attachment submissions never consume another owner's draft or the
legacy implicit image queue. Files become existing `@file:` references; images
enter the existing image input path. Text-only legacy submission remains valid.

The composer accepts file selection, pasted images and drops, rejects recursive
directory uploads, and retains drafts while the agent is busy. Sending requires
a manual action after the agent is idle; attachments never enter queue/steer.
Each add has a fresh occurrence ID. File/Blob objects and object URLs remain
local and are released on removal, acceptance and teardown.

| Interruption | Recovery |
| --- | --- |
| Upload response lost | Query the ledger before retrying; reuse completed bytes. |
| Refresh during upload | Recover metadata; require file reselection because the browser File is lost. |
| Refresh after upload | Recover original uploaded metadata without uploading again. |
| Explicit submit rejection before claim | Retain the draft for correction and manual send. |
| Submit ACK lost | Never replay automatically. Reconcile ledger/live/durable state; block uncertain combinations. |
| Cancel races with completion | Server tombstones and client occurrence/generation checks prevent resurrection. |
| Backend restart | Unsubmitted drafts are not guaranteed to recover; show expired/unavailable state. |

Limits are 100 MiB per ordinary file, 25 MiB per image, 10 attachments, 200 MiB
per draft and two concurrent streams. Streaming enforces the prepared byte
limit independently of Content-Length. Empty or traversal/control-character
filenames are rejected. PNG/JPEG/GIF/WebP/BMP content is decoded and bounded by
pixel/frame budgets; MIME spoofing fails. HTML/SVG/PDF remain ordinary files;
there is no PDF-to-image conversion or executable document preview.

Drafts expire after 24 idle hours; drafts supporting an active submitted turn
remain protected. The reaper revisits every profile home that has created a
browser draft, including profiles first used after process startup. Under the
same lock as upload/claim/cancel/recovery, it retains live ledger items and
removes only generated completed files older than the retention period whose
content/metadata references are absent from a successful read-only SessionDB
lookup. Claimed files survive cancellation/expiry themselves; a later sweep can
reclaim them after their durable session is deleted and no live owner remains.
Unowned temporary uploads are cleaned; symlinks and unknown names are never
followed or deleted. Unknown ownership and database failures retain files.
Authenticated resource reads constrain profile roots, resolved paths and
size, use no-sniff responses, and do not serve HTML/SVG inline.

Renderers bound tool results/diffs to pages of 200 lines or 32 KiB and fall back
to bounded text for Markdown larger than 256 KiB. Stable Markdown blocks reuse
parsing caches; collapsed content does not load code highlighters. HTML/SVG
artifacts render as escaped source in Web. External links reject executable
schemes and embedded credentials; media fetches never forward dashboard
credentials to an external origin.

The regression suites cover the ledger, RPC/HTTP ownership, true HTTP plus WS
submission with a controlled model worker, durable presentation and pagination,
Native hydration races, resource cleanup, XSS and package boundaries. Browser
validation also exercises refresh after upload and matching live/history
artifacts and generated images. Controlled workers verify the transport and
persistence contracts; they do not validate an external model provider.

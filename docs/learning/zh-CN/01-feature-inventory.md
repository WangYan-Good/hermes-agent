# Hermes Agent 功能清单

> 盘点基线：`develop@1a9a9c1fe`，日期 2026-08-08。返回[学习中心](README.md)。

## 阅读说明

本清单按“用户能够完成的一类事情”划分功能族，而不是按文件数、类名或每个工具参数划分。这样既能覆盖完整产品面，也不会把频繁变化的模型、工具 Schema、平台或技能枚举冻结在教程中。

每一行包含七类信息：

| 列 | 如何阅读 |
|---|---|
| 功能族 | 稳定的能力名称；一族内可能包含多个命令或工具。 |
| 能力与场景 | 用户能解决什么问题。 |
| 用户入口/前置条件 | 从哪里启用，需要什么配置、凭据或运行环境。 |
| 工作原理 | 能力在系统中的关键数据流，而不是营销描述。 |
| 关键源码 | 最先应该阅读的入口；复杂功能通常还会继续分层。 |
| 深入阅读 | 参数、平台差异和操作细节的单一事实来源。 |
| 边界 | 部署和可用性约束。 |

边界标签统一为：

- **核心**：默认代码路径的一部分，具体工具是否可见仍受 Toolset 和平台约束。
- **内置可选**：仓库内提供，但需要用户显式安装、启用或选择。
- **插件提供**：通过 Plugin 扩展面发现，不应理解成永久核心 Schema。
- **凭据门控**：只有合法 API key、OAuth 或服务配置存在时才可用。
- **平台限定**：只在特定操作系统、客户端或消息平台上有意义。
- **实验性**：接口或运营语义仍可能调整，生产使用前应阅读专项文档。

当前动态参考入口包括[CLI 命令](../../../website/docs/reference/cli-commands.md)、[斜杠命令](../../../website/docs/reference/slash-commands.md)、[工具](../../../website/docs/reference/tools-reference.md)和[工具集](../../../website/docs/reference/toolsets-reference.md)。数量只表示某次盘点快照，不构成行为契约。

## 1. 交互入口与客户端

同一个 `AIAgent` 被多个交互面复用，但每个交互面负责自己的输入、呈现、审批和传输。尤其需要区分：Dashboard 的主聊天是嵌入的真实 TUI；Electron Desktop 则是独立 React 聊天面。

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| 经典交互式 CLI | 在终端连续对话，使用斜杠命令、自动补全、工具进度、图片粘贴和审批。 | 运行 `hermes`；需完成模型 Provider 配置。 | `HermesCLI` 维护交互状态，把用户轮次交给 `AIAgent`，再用 Rich/prompt_toolkit 渲染结果。 | [`cli.py`](../../../cli.py)、[`hermes_cli/main.py`](../../../hermes_cli/main.py) | [CLI 使用](../../../website/docs/user-guide/cli.md)、[斜杠命令](../../../website/docs/reference/slash-commands.md) | 核心；终端能力受本机环境影响。 |
| Ink TUI | 使用 React/Ink 获得完整终端应用、流式转录、活动面板、选择器和交互提示。 | `hermes --tui` 或相应配置；需要 Node 构建产物。 | Node 端通过 stdio JSON-RPC 连接 Python `tui_gateway`，Python 端持有 Agent、Session 和工具。 | [`ui-tui/src/app.tsx`](../../../ui-tui/src/app.tsx)、[`tui_gateway/server.py`](../../../tui_gateway/server.py) | [TUI](../../../website/docs/user-guide/tui.md) | 核心客户端；Node + Python 双进程。 |
| Web Dashboard | 在浏览器中使用真实 TUI，并配合会话、配置、日志和辅助面板。 | `hermes dashboard`；非 loopback 绑定必须配置认证。 | `/chat` 用 xterm.js 连接 `/api/pty`，服务端通过 PTY 启动 `hermes --tui`；结构化侧栏不替代主聊天。 | [`hermes_cli/pty_bridge.py`](../../../hermes_cli/pty_bridge.py)、[`web/src/pages/ChatPage.tsx`](../../../web/src/pages/ChatPage.tsx) | [Web Dashboard](../../../website/docs/user-guide/features/web-dashboard.md) | 核心客户端；POSIX PTY，公开部署有认证边界。 |
| Electron Desktop | 提供独立的桌面聊天、文件、终端、预览、会话和插件 UI。 | 安装桌面应用；后端运行 `hermes serve`，旧运行时才回退到 `dashboard --no-open`。 | Electron/React 通过共享 WebSocket JSON-RPC 客户端连接 headless Python 后端，不依赖 Dashboard 前端运行。 | [`apps/desktop/`](../../../apps/desktop/)、[`apps/shared/`](../../../apps/shared/)、[`tui_gateway/ws.py`](../../../tui_gateway/ws.py) | [Desktop](../../../website/docs/user-guide/desktop.md)、[Desktop Plugin SDK](../../../website/docs/developer-guide/desktop-plugin-sdk.md) | 平台限定；独立聊天面，不嵌入 TUI。 |
| 消息网关（Gateway） | 从 Telegram、Discord、Slack、WhatsApp 等渠道持续聊天和接收自动化结果。 | `hermes gateway setup` 后运行或安装 Gateway 服务。 | 平台 Adapter 把事件归一化为会话输入，执行权限与配对检查，再调用 `AIAgent` 并由平台发送结果。 | [`gateway/run.py`](../../../gateway/run.py)、[`gateway/session.py`](../../../gateway/session.py) | [消息 Gateway](../../../website/docs/user-guide/messaging/index.md)、[Gateway 原理](../../../website/docs/developer-guide/gateway-internals.md) | 核心编排 + 平台插件；各渠道能力不同。 |
| OpenAI 兼容 API Server | 让 Open WebUI、LobeChat、LibreChat 或自建前端以 OpenAI 格式调用 Hermes。 | 启用 API Server/Gateway 配置并设置访问控制。 | HTTP 请求被转换为 Hermes 会话轮次，仍经过相同 Agent、工具和持久化链路，再映射为兼容响应。 | [`gateway/platforms/api_server.py`](../../../gateway/platforms/api_server.py) | [API Server](../../../website/docs/user-guide/features/api-server.md) | 核心入口；公开绑定需鉴权和网络隔离。 |
| Headless `hermes serve` | 为 Desktop 或外部客户端只启动 JSON-RPC/WS/API 后端，不构建或暴露 SPA。 | 运行 `hermes serve`。 | 与 Dashboard 复用服务器启动代码，但设置 headless 标志禁用前端挂载。 | [`hermes_cli/subcommands/dashboard.py`](../../../hermes_cli/subcommands/dashboard.py)、[`hermes_cli/web_server.py`](../../../hermes_cli/web_server.py) | [`hermes serve` 命令](../../../website/docs/reference/cli-commands.md#hermes-serve) | 核心；不是另一个聊天实现。 |
| ACP 编辑器集成 | 在 VS Code、Zed、JetBrains 等 ACP Host 内查看对话、工具、文件差异和终端动作。 | `hermes acp` 或编辑器配置；Host 需支持 ACP。 | ACP Adapter 把协议 Session、事件和审批映射到 `AIAgent` 与 Hermes 工具。 | [`acp_adapter/server.py`](../../../acp_adapter/server.py)、[`acp_adapter/session.py`](../../../acp_adapter/session.py) | [ACP](../../../website/docs/user-guide/features/acp.md)、[ACP Internals](../../../website/docs/developer-guide/acp-internals.md) | 核心入口；能力取决于 ACP Host。 |
| Python 程序化集成 | 从脚本或服务直接调用 `AIAgent.chat()` / `run_conversation()`。 | 安装 Python 包并提供模型配置；测试应使用临时 `HERMES_HOME`。 | 调用者绕过交互 UI，但复用 Prompt、Provider、工具和 Session 运行时。 | [`run_agent.py`](../../../run_agent.py) | [Python Library](../../../website/docs/guides/python-library.md)、[Programmatic Integration](../../../website/docs/developer-guide/programmatic-integration.md) | 核心 API；构造参数较多，应优先复用配置解析。 |
| 批处理与轨迹生成 | 对大量 Prompt 并行运行 Agent，用于评测、数据生成和训练轨迹。 | 批处理 CLI/配置；需要控制并发、成本和输出路径。 | `batch_runner` 为各输入创建隔离运行，汇总 ShareGPT/轨迹格式结果。 | [`batch_runner.py`](../../../batch_runner.py)、[`trajectory_compressor.py`](../../../trajectory_compressor.py) | [Batch Processing](../../../website/docs/user-guide/features/batch-processing.md)、[Trajectory Format](../../../website/docs/developer-guide/trajectory-format.md) | 核心入口；高资源/高成本场景。 |

## 2. 智能体核心运行时

智能体循环（Agent Loop）的核心不是“调用一次模型”，而是持续维护合法消息序列，在预算和中断边界内重复“模型决策 → 工具执行 → 结果回注”，直到得到最终回复。

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| `AIAgent` 对话接口 | 为所有客户端提供统一的同步聊天和完整会话接口。 | `chat()` 用于简单集成；`run_conversation()` 返回完整消息与元数据。 | 初始化时固定模型、工具、会话和回调依赖；每轮复用同一 Agent 状态。 | [`run_agent.py`](../../../run_agent.py) | [Agent Loop](../../../website/docs/developer-guide/agent-loop.md) | 核心窄腰；构造器不是扩展所有能力的入口。 |
| 多 API 模式 | 连接 Chat Completions、Codex Responses/App Server、Anthropic 等不同协议。 | 由 Provider 插件和 `api_mode` 解析；通常通过 Setup/Model 选择。 | Transport/Adapter 把协议差异映射到统一的文本、推理和工具调用语义。 | [`agent/transports/`](../../../agent/transports/)、[`agent/codex_runtime.py`](../../../agent/codex_runtime.py) | [API Modes](../../../website/docs/developer-guide/agent-loop.md#api-modes) | 核心适配；并非每个 Provider 支持同样能力。 |
| Provider 与模型解析 | 从 Profile 配置、OAuth、插件目录和模型选择解析实际调用后端。 | `hermes setup`、`hermes model`、配置文件或调用参数。 | Runtime Provider 把用户选择解析为 base URL、认证、模型、API 模式和能力元数据。 | [`hermes_cli/runtime_provider.py`](../../../hermes_cli/runtime_provider.py)、[`hermes_cli/provider_catalog.py`](../../../hermes_cli/provider_catalog.py) | [Provider Runtime](../../../website/docs/developer-guide/provider-runtime.md)、[配置模型](../../../website/docs/user-guide/configuring-models.md) | 核心编排 + Provider 插件；凭据门控。 |
| 模型路由与辅助模型 | 为主对话、视觉、压缩等工作选择不同模型，并按规则排序或限制 Provider。 | `config.yaml` 中路由/辅助模型配置。 | 主模型决定 Agent 轮次；辅助模型只承担指定子任务，不改变主模型上下文窗口语义。 | [`agent/model_metadata.py`](../../../agent/model_metadata.py)、[`agent/context_compressor.py`](../../../agent/context_compressor.py) | [Provider Routing](../../../website/docs/user-guide/features/provider-routing.md) | 核心；路由能力取决于 Provider。 |
| Fallback Provider | 主调用出现可恢复错误时切换备用 Provider/模型。 | `hermes fallback` 或配置回退链。 | 错误分类决定是否尝试链中下一项；辅助任务可以有独立回退。 | [`hermes_cli/fallback_config.py`](../../../hermes_cli/fallback_config.py)、[`run_agent.py`](../../../run_agent.py) | [Fallback Providers](../../../website/docs/user-guide/features/fallback-providers.md) | 内置可选；不能掩盖不可恢复的用户或安全错误。 |
| 提示词组装 | 把 SOUL、系统约束、平台提示、上下文文件、记忆和工具提示组成稳定前缀。 | 会话启动时自动发生；可用 `hermes prompt-size` 检查。 | Prompt Builder 按固定层次装配内容；会话中避免重建历史系统前缀，以保护缓存。 | [`agent/prompt_builder.py`](../../../agent/prompt_builder.py)、[`agent/system_prompt.py`](../../../agent/system_prompt.py) | [Prompt Assembly](../../../website/docs/developer-guide/prompt-assembly.md) | 核心；缓存不变量。 |
| 工具调用循环 | 让模型调用文件、终端、Web、浏览器等结构化能力。 | 当前 Toolset 中工具可见且门控通过。 | assistant 返回 tool call，Dispatcher 调用 Handler，将 tool result 追加到消息，再进行下一次模型调用。 | [`model_tools.py`](../../../model_tools.py)、[`run_agent.py`](../../../run_agent.py) | [Tools Runtime](../../../website/docs/developer-guide/tools-runtime.md) | 核心；严格遵守协议角色顺序。 |
| 顺序/并发工具执行 | 在安全和依赖允许时降低多工具调用延迟。 | 由工具属性和运行时策略决定。 | 独立调用可以并发；涉及共享终端、文件或 Agent 状态的调用保持顺序并汇总结果。 | [`model_tools.py`](../../../model_tools.py) | [Tool Execution](../../../website/docs/developer-guide/agent-loop.md#tool-execution) | 核心；并发不改变结果消息的协议合法性。 |
| 流式文本、推理与进度 | 向 CLI、TUI、Desktop 和消息平台逐步展示模型文本、reasoning 与工具活动。 | 客户端需实现相应 Callback/Event。 | Agent 将 Provider stream 归一化为单写者事件，客户端分别渲染；最终消息仍统一持久化。 | [`agent/stream_single_writer.py`](../../../agent/stream_single_writer.py)、[`agent/stream_diag.py`](../../../agent/stream_diag.py) | [Agent callbacks](../../../website/docs/developer-guide/agent-loop.md#callback-surfaces) | 核心；平台可能只支持编辑式伪流。 |
| 中断、停止与重试 | 停止长调用或后台动作，并在合法消息边界恢复/重试。 | `/stop`、客户端取消、Gateway 命令。 | 中断标志和可中断传输终止当前工作；运行时补齐必要状态，避免留下非法半轮。 | [`agent/interrupt_compat.py`](../../../agent/interrupt_compat.py)、[`tools/interrupt.py`](../../../tools/interrupt.py) | [Interruptible API Calls](../../../website/docs/developer-guide/agent-loop.md#interruptible-api-calls) | 核心；外部服务是否立即取消取决于 Provider。 |
| 迭代预算与宽限调用 | 限制主 Agent 与子智能体共享的工具迭代，避免失控循环。 | `max_iterations`、Iteration Budget 或平台配置。 | 每次工具轮消耗预算；预算耗尽时允许受控的最终宽限调用生成面向用户的收尾。 | [`agent/iteration_budget.py`](../../../agent/iteration_budget.py)、[`run_agent.py`](../../../run_agent.py) | [Budget and Fallback](../../../website/docs/developer-guide/agent-loop.md#budget-and-fallback-behavior) | 核心；预算不是 token 配额。 |
| Goal、Todo 与交付模式 | 为长任务保存目标、阶段任务和交付约束。 | `/goal`、Todo 工具、Deliverable Mode/客户端 UI。 | 目标和任务状态与会话关联；模型通过受控工具更新，而不是依赖自然语言记忆。 | [`hermes_cli/goals.py`](../../../hermes_cli/goals.py)、[`tools/todo_tool.py`](../../../tools/todo_tool.py) | [Goals](../../../website/docs/user-guide/features/goals.md)、[Deliverable Mode](../../../website/docs/user-guide/features/deliverable-mode.md) | 核心/客户端呈现；各入口 UI 不同。 |
| 用量、价格与账户限制 | 展示 token、成本、上下文占用和部分 Provider 实时额度。 | `/usage`、`hermes insights`、状态面板。 | 本地聚合会话用量并按模型定价估算；支持的 Provider 可额外查询账户限制。 | [`agent/usage_pricing.py`](../../../agent/usage_pricing.py)、[`agent/account_usage.py`](../../../agent/account_usage.py) | [Slash Commands](../../../website/docs/reference/slash-commands.md#interactive-cli-slash-commands) | 核心；成本为估算，实时额度受 Provider 支持限制。 |

## 3. 上下文、记忆与持久化

这组能力最容易被混为一谈。简化后的职责是：Session 保存发生过什么；Context Files 告诉 Agent 当前项目规则；Memory 保存跨会话仍有价值的信息；压缩摘要让长会话继续运行；Checkpoint 保存文件系统恢复点。

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| 项目上下文文件 | 自动加载 `.hermes.md`、`AGENTS.md`、`CLAUDE.md`、`SOUL.md` 等项目/身份说明。 | 在工作目录或 Hermes Home 放置支持的文件。 | Prompt 组装时按优先级发现并加入稳定上下文，子目录规则可随工作目录变化。 | [`agent/coding_context.py`](../../../agent/coding_context.py)、[`agent/prompt_builder.py`](../../../agent/prompt_builder.py) | [Context Files](../../../website/docs/user-guide/features/context-files.md) | 核心；内容会占用前缀 token。 |
| `@` 上下文引用 | 在单轮消息中注入文件、目录、Git diff 或 URL 内容。 | 在支持的客户端输入 `@...`。 | 客户端/Agent 展开引用并把结果附到当前用户消息，不永久改变系统提示词。 | [`agent/context_references.py`](../../../agent/context_references.py) | [Context References](../../../website/docs/user-guide/features/context-references.md) | 核心；大内容受输出和上下文限制。 |
| SessionDB 会话存储 | 持久保存消息、元数据、模型选择、事件、用量和来源。 | 默认位于 Profile 对应的 Hermes Home。 | SQLite 事务把对话轮与元数据写入稳定 Session ID，客户端可恢复和继续。 | [`hermes_state.py`](../../../hermes_state.py)、[`hermes_state_schema.py`](../../../hermes_state_schema.py) | [Sessions](../../../website/docs/user-guide/sessions.md)、[Session Storage](../../../website/docs/developer-guide/session-storage.md) | 核心；Profile 间默认隔离。 |
| FTS5 会话搜索 | 按文本发现过去 Session，并在命中附近滚动读取真实消息。 | `session_search` 工具、`hermes sessions` 或客户端搜索。 | FTS5 索引定位消息，读取原始数据库记录，不调用 LLM 做检索。 | [`hermes_state_search.py`](../../../hermes_state_search.py)、[`tools/session_search_tool.py`](../../../tools/session_search_tool.py) | [Session Search Tool](../../../website/docs/reference/tools-reference.md#session_search-toolset) | 核心；搜索范围受归档和来源权限约束。 |
| 会话生命周期管理 | 列出、恢复、命名、导出、归档、清理、统计和优化会话。 | `/sessions`、`hermes sessions ...`、Desktop/Dashboard。 | CLI/客户端调用 SessionDB 查询和迁移接口；归档为软隐藏，Prune 才删除匹配数据。 | [`hermes_cli/sessions_cmd.py`](../../../hermes_cli/sessions_cmd.py)、[`hermes_cli/session_export.py`](../../../hermes_cli/session_export.py) | [CLI Commands · sessions](../../../website/docs/reference/cli-commands.md#hermes-sessions) | 核心；删除和修复操作应先备份。 |
| SessionDB 修复与恢复 | 修复 Schema 异常或把受损数据库恢复到独立干净文件。 | `hermes sessions repair/recover/optimize-storage`。 | Repair 在备份后修正已知结构；Recover 不覆盖原库，重建可读记录和索引。 | [`hermes_cli/session_recovery.py`](../../../hermes_cli/session_recovery.py) | [Session Commands](../../../website/docs/reference/cli-commands.md#hermes-sessions) | 核心运维；涉及磁盘写入。 |
| 内置长期记忆 | 跨会话保存用户偏好、项目事实和环境知识。 | `memory` 工具；内容写入 `MEMORY.md` / `USER.md`。 | 受控记忆 Handler 更新有限文本文件；新会话在 Prompt 构建时加载。 | [`agent/memory_manager.py`](../../../agent/memory_manager.py)、[`tools/memory_tool.py`](../../../tools/memory_tool.py) | [Memory](../../../website/docs/user-guide/features/memory.md) | 核心；应保存稳定事实，不保存整段日志或秘密。 |
| 外部 Memory Provider | 使用 Honcho、Mem0、OpenViking 等后端进行用户建模或语义记忆。 | `hermes memory setup` 或 Provider 插件配置。 | Memory Provider 接口在会话前后执行检索/写回；内置文件记忆仍是独立层。 | [`agent/memory_provider.py`](../../../agent/memory_provider.py)、[`plugins/memory/`](../../../plugins/memory/) | [Memory Providers](../../../website/docs/user-guide/features/memory-providers.md)、[Plugin Guide](../../../website/docs/developer-guide/memory-provider-plugin.md) | 插件提供、凭据门控。 |
| 上下文压缩 | 在接近阈值时总结旧中段，保留系统/首段与近期尾部，延长会话。 | 自动触发或 `/compress`；由 `compression` 配置控制。 | 先裁剪旧工具大输出，再计算边界、生成摘要并校验消息序列；默认在同一 Session ID 内压缩。 | [`agent/context_compressor.py`](../../../agent/context_compressor.py) | [Compression & Caching](../../../website/docs/developer-guide/context-compression-and-caching.md) | 核心；摘要有信息损失，原记录软归档可搜索。 |
| Prompt Caching | 复用长会话稳定前缀，降低支持 Provider 的延迟与输入成本。 | 支持的 Anthropic/OpenRouter/Nous 等路径自动使用。 | 系统提示词与历史前缀保持稳定；缓存标记由 Provider Adapter 转换，压缩是允许重建前缀的边界。 | [`agent/prompt_caching.py`](../../../agent/prompt_caching.py)、[`agent/prompt_builder.py`](../../../agent/prompt_builder.py) | [Context Compression and Caching](../../../website/docs/developer-guide/context-compression-and-caching.md) | Provider 限定；缓存命中由远端决定。 |
| 轨迹保存与压缩 | 保存完整 Agent 交互，供调试、评测、训练或分享。 | `save_trajectories`、批处理或相关导出命令。 | 消息、推理、工具调用和结果被序列化；压缩器可去除冗余大输出。 | [`trajectory_compressor.py`](../../../trajectory_compressor.py)、[`agent/trajectory.py`](../../../agent/trajectory.py) | [Trajectory Format](../../../website/docs/developer-guide/trajectory-format.md) | 内置可选；可能包含敏感上下文，分享前应脱敏。 |
| 文件系统 Checkpoint | 在 Agent 修改工作区前创建快照，并通过 `/rollback` 恢复。 | 默认集成文件写入路径；`/rollback`、`hermes checkpoints`。 | Checkpoint Store 按项目保存内容寻址快照；恢复只影响文件系统，不回退消息历史。 | [`tools/checkpoint_manager.py`](../../../tools/checkpoint_manager.py)、[`hermes_cli/checkpoints.py`](../../../hermes_cli/checkpoints.py) | [Checkpoints and Rollback](../../../website/docs/user-guide/checkpoints-and-rollback.md) | 核心安全网；大仓库受容量与清理策略约束。 |
| Projects 工作区 | 在 Desktop/CLI 中管理命名工作区、多个文件夹、主目录和 Kanban 绑定。 | `hermes project ...` 或 Desktop Project UI。 | Project 元数据把聊天工作目录绑定到一组路径；切换时同步 Session 的 workspace。 | [`hermes_cli/projects_cmd.py`](../../../hermes_cli/projects_cmd.py)、[`tools/project_tools.py`](../../../tools/project_tools.py) | [CLI Commands](../../../website/docs/reference/cli-commands.md) | 主要面向 Desktop；不等于 Profile。 |
| Profiles 隔离 | 为不同身份、团队、模型和 Gateway 保存独立配置、凭据、会话与技能状态。 | `hermes profile ...` 或命令前使用 Profile 别名。 | Profile 解析到独立 Hermes Home；克隆只在创建时复制，不做运行时继承。 | [`hermes_constants.py`](../../../hermes_constants.py)、[`hermes_cli/subcommands/profile.py`](../../../hermes_cli/subcommands/profile.py) | [Profiles](../../../website/docs/user-guide/profiles.md)、[Profile Routing](../../../docs/profile-routing.md) | 核心；隔离是有意设计。 |

## 4. 工具与执行环境

工具不是“项目中所有 Python 函数”。只有注册了结构化 Schema、Handler 和 Toolset 的能力才会进入模型可调用面；`check_fn` 还能在服务未配置时把工具从 Schema 中完全移除。

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| Tool Registry 与自动发现 | 注册、发现、校验和分发内置工具。 | Agent 初始化时自动运行；开发者在工具模块顶层调用 `registry.register()`。 | Discovery 只导入含顶层注册调用的模块；Registry 保存 Schema、Handler、Toolset、门控与结果上限。 | [`tools/registry.py`](../../../tools/registry.py)、[`model_tools.py`](../../../model_tools.py) | [Tools Runtime](../../../website/docs/developer-guide/tools-runtime.md) | 核心；新增核心工具会增加每轮 Schema 成本。 |
| Toolset 选择 | 按 CLI、Gateway、子智能体或任务场景限制模型可见工具。 | `hermes tools`、配置或 Agent 构造参数。 | 默认集合、启用/禁用项和平台规则解析为工具名；随后再执行 `check_fn`。 | [`toolsets.py`](../../../toolsets.py)、[`toolset_distributions.py`](../../../toolset_distributions.py) | [Toolsets Reference](../../../website/docs/reference/toolsets-reference.md) | 核心；不是权限系统的替代品。 |
| 服务/凭据门控 | 仅在依赖或配置满足时暴露结构化工具。 | 配置对应服务、安装依赖或完成 OAuth。 | `check_fn` 在生成 Schema 前返回可用性；失败工具不会占用模型上下文，也不会诱导无效调用。 | [`tools/registry.py`](../../../tools/registry.py) | [Tool Availability](../../../website/docs/developer-guide/tools-runtime.md#tool-availability-checking-check_fn) | 核心机制；具体能力多为凭据门控。 |
| 文件工具 | 读取文本/文档、完整写入、模糊 Patch、内容和文件名搜索。 | `file` Toolset；需工作目录可读写。 | Handler 做路径安全检查、格式提取、大小限制和语法检查；写操作接入审批与 Checkpoint。 | [`tools/file_tools.py`](../../../tools/file_tools.py)、[`tools/path_security.py`](../../../tools/path_security.py) | [Tools Reference · file](../../../website/docs/reference/tools-reference.md#file-toolset) | 核心；受沙箱、审批和文件大小限制。 |
| Terminal 与后台进程 | 执行 Shell、启动长期服务、轮询日志、写 stdin 和终止进程。 | `terminal` Toolset；选择本地或远程环境。 | Terminal 创建持久 Session；后台命令登记到 Process Registry，结果按上限回传或落盘。 | [`tools/terminal_tool.py`](../../../tools/terminal_tool.py)、[`tools/process_registry.py`](../../../tools/process_registry.py) | [Code Execution](../../../website/docs/user-guide/features/code-execution.md)、[Tools Reference · terminal](../../../website/docs/reference/tools-reference.md#terminal-toolset) | 核心；危险模式进入审批。 |
| 远程/隔离执行环境 | 在 Local、Docker、SSH、Modal、Daytona、Singularity 或 Vercel Sandbox 中运行命令与同步文件。 | 选择相应 Backend 并配置运行服务。 | 统一 Environment 接口实现命令、文件同步、工作目录和生命周期；工具 Schema 不随 Backend 改变。 | [`tools/environments/`](../../../tools/environments/) | [Terminal Environments](../../../website/docs/developer-guide/tools-runtime.md#terminalruntime-environments) | 内置可选/凭据门控；安全保证由 Backend 决定。 |
| Web 搜索与抽取 | 搜索最新资料，抽取网页或 PDF 的清洁正文。 | `web` Toolset；配置 Exa、Parallel、Firecrawl、Tavily 等 Provider。 | Web Provider 插件执行搜索/抽取，统一结果格式并把超大正文保存到磁盘。 | [`tools/web_tools.py`](../../../tools/web_tools.py)、[`agent/web_search_provider.py`](../../../agent/web_search_provider.py) | [Web Search](../../../website/docs/user-guide/features/web-search.md)、[Web Provider Plugin](../../../website/docs/developer-guide/web-search-provider-plugin.md) | 插件提供、凭据门控；网络内容不可信。 |
| 浏览器自动化 | 导航、快照、点击、输入、滚动、截图、Console 和图片发现。 | `browser` Toolset；选择 Browserbase、Browser Use、CDP、本地 Chromium 等后端。 | Browser Supervisor 管理 Session；高层工具基于可访问性引用操作，视觉截图用于补充文本树。 | [`tools/browser_tool.py`](../../../tools/browser_tool.py)、[`tools/browser_supervisor.py`](../../../tools/browser_supervisor.py) | [Browser](../../../website/docs/user-guide/features/browser.md)、[Browser Supervisor](../../../website/docs/developer-guide/browser-supervisor.md) | 内置可选/插件提供；站点策略与登录状态受限。 |
| CDP 与原生 Dialog | 调用 Chrome DevTools Protocol 逃生口，处理 alert/confirm/prompt。 | 配置 CDP Endpoint；先建立浏览器 Session。 | CDP 工具把结构化命令直接送到浏览器；Dialog 工具消费 Snapshot 暴露的待处理对话框。 | [`tools/browser_cdp_tool.py`](../../../tools/browser_cdp_tool.py)、[`tools/browser_dialog_tool.py`](../../../tools/browser_dialog_tool.py) | [Tools Reference · browser](../../../website/docs/reference/tools-reference.md#browser-toolset-cdp-gated-tools) | 凭据/环境门控；低层接口风险更高。 |
| Vision 与图片输入 | 分析本地或远程图片，在 CLI 中粘贴图片并让主模型或辅助模型理解。 | `vision` Toolset；模型需原生视觉或配置辅助视觉模型。 | 原生多模态时把像素作为工具结果送回主模型；文本模型回退到辅助视觉描述。 | [`tools/vision_tools.py`](../../../tools/vision_tools.py) | [Vision](../../../website/docs/user-guide/features/vision.md) | 模型/客户端限定；远程图片受 URL 安全检查。 |
| 视频分析 | 提取字幕、场景、关键时间点和视觉描述。 | `video` Toolset；依赖可访问的视频或 URL。 | Handler 获取媒体并组合转写、抽帧与视觉分析，返回结构化摘要。 | [`tools/vision_tools.py`](../../../tools/vision_tools.py) | [Tools Reference · video](../../../website/docs/reference/tools-reference.md#video-toolset) | 内置可选；媒体大小和格式受限。 |
| 图像生成 | 文生图或基于输入图进行编辑/风格参考。 | `image_gen` Toolset；启用 FAL、OpenAI、Codex、xAI、Krea 等插件和凭据。 | 统一 `image_generate` Schema 路由到当前配置的 Image Provider，模型不在每次调用中自行切换后端。 | [`tools/image_generation_tool.py`](../../../tools/image_generation_tool.py)、[`agent/image_gen_provider.py`](../../../agent/image_gen_provider.py) | [Image Generation](../../../website/docs/user-guide/features/image-generation.md)、[Image Plugin](../../../website/docs/developer-guide/image-gen-provider-plugin.md) | 插件提供、凭据门控。 |
| 视频生成与编辑 | 文生视频、图生视频，以及部分 Provider 的编辑和延长。 | `video_gen` Toolset；启用 FAL/xAI 等 Video Provider。 | 通用 Schema 路由到当前 Video Provider；Provider 特有动作保留独立门控工具。 | [`tools/video_generation_tool.py`](../../../tools/video_generation_tool.py)、[`agent/video_gen_provider.py`](../../../agent/video_gen_provider.py) | [Video Provider Plugin](../../../website/docs/developer-guide/video-gen-provider-plugin.md) | 插件提供、凭据门控、高成本。 |
| 转写、TTS 与 Voice Mode | 转写语音消息、生成语音回复、在 CLI/消息平台进行连续语音交互。 | 配置转写/TTS Provider；Voice Mode 需要麦克风/播放设备。 | Provider Adapter 统一音频输入输出；Gateway 按平台发送 voice bubble 或附件。 | [`tools/transcription_tools.py`](../../../tools/transcription_tools.py)、[`tools/tts_tool.py`](../../../tools/tts_tool.py)、[`tools/voice_mode.py`](../../../tools/voice_mode.py) | [Voice Mode](../../../website/docs/user-guide/features/voice-mode.md)、[TTS](../../../website/docs/user-guide/features/tts.md) | 内置可选、凭据/平台限定。 |
| Wake Word | 用本地热词监听触发 CLI、TUI 或 Desktop 语音会话。 | 安装语音依赖并启用 Wake Word。 | 本地监听器检测唤醒短语后发出客户端事件，不把持续音频发送给主模型。 | [`tools/wake_word.py`](../../../tools/wake_word.py) | [Wake Word](../../../website/docs/user-guide/features/wake-word.md) | 平台限定；麦克风权限和本地模型依赖。 |
| Computer Use | 后台控制桌面应用，执行截图、点击、拖拽、滚动、输入和窗口聚焦。 | 安装 `cua-driver` 并启用 `computer_use` Toolset。 | Hermes 通过 CUA Backend 发送动作和获取截图/AX/SOM，不抢占用户物理指针。 | [`tools/computer_use/`](../../../tools/computer_use/)、[`tools/computer_use_tool.py`](../../../tools/computer_use_tool.py) | [Computer Use](../../../website/docs/user-guide/features/computer-use.md) | 内置可选、平台限定；高权限操作需要谨慎。 |
| Python 代码执行 RPC | 用一段 Python 组合 3 个以上工具调用、过滤大结果或进行条件分支。 | `code_execution` Toolset；脚本只能通过受控 RPC 调 Hermes 工具。 | 子进程执行脚本，工具调用经 RPC 回到主进程的相同 Registry/审批边界。 | [`tools/code_execution_tool.py`](../../../tools/code_execution_tool.py) | [Code Execution](../../../website/docs/user-guide/features/code-execution.md) | 核心可选；不是绕过工具权限的任意 Python。 |
| 子智能体委派 | 把独立工作流交给隔离上下文和终端的子 Agent，可批量并行。 | `delegation` Toolset；受并发、嵌套和共享预算限制。 | 父 Agent 传递明确目标；子 Agent 使用独立消息历史，只把最终摘要返回父上下文。 | [`tools/delegate_tool.py`](../../../tools/delegate_tool.py)、[`agent/delegation_context.py`](../../../agent/delegation_context.py) | [Delegation](../../../website/docs/user-guide/features/delegation.md)、[Lifecycle API](../../../website/docs/developer-guide/subagent-lifecycle-api.md) | 核心可选；不自动共享未声明上下文。 |
| Cron 工具 | 在对话中创建、查看、更新、暂停、恢复、运行和删除定时任务。 | `cronjob` Toolset；Gateway/Scheduler 负责持续 Tick 和投递。 | 工具写入持久 Job 定义；到期时在新 Session 中执行并投递目标渠道。 | [`tools/cronjob_tools.py`](../../../tools/cronjob_tools.py)、[`cron/`](../../../cron/) | [Cron](../../../website/docs/user-guide/features/cron.md)、[Cron Internals](../../../website/docs/developer-guide/cron-internals.md) | 核心可选；新会话不继承当前聊天隐式上下文。 |
| Session Search 工具 | 发现、浏览和读取过去真实会话内容。 | `session_search` Toolset。 | Handler 查询 SessionDB/FTS5，并按 Session 权限与分页规则返回原消息。 | [`tools/session_search_tool.py`](../../../tools/session_search_tool.py) | [Tools Reference · session_search](../../../website/docs/reference/tools-reference.md#session_search-toolset) | 核心；只读。 |
| Todo、Clarify 与审批交互 | 维护任务清单，在信息不足时结构化提问，确认危险命令或写操作。 | 对应 Toolset；客户端需实现 Prompt/Approval UI。 | Agent 级工具更新会话状态或暂停执行等待用户；批准作用域由用户明确选择，可限于具体请求，也可扩展到会话模式或显式永久 Allowlist。 | [`tools/todo_tool.py`](../../../tools/todo_tool.py)、[`tools/clarify_tool.py`](../../../tools/clarify_tool.py)、[`tools/approval.py`](../../../tools/approval.py) | [Tools Reference](../../../website/docs/reference/tools-reference.md) | 核心；审批是安全边界。 |
| Projects 与 Desktop Pane 工具 | 创建/切换 Project，读取终端/预览，打开预览并聚焦桌面 Pane。 | Desktop 或支持的 Project 运行时。 | 工具通过 Desktop UI Bridge 发事件；非 Desktop 环境由门控隐藏无意义 Schema。 | [`tools/project_tools.py`](../../../tools/project_tools.py)、[`tools/desktop_ui.py`](../../../tools/desktop_ui.py) | [Tools Reference · project/terminal](../../../website/docs/reference/tools-reference.md) | 平台限定。 |
| Home Assistant、Feishu、Discord 等集成工具 | 控制智能家居、读写飞书文档/评论、管理 Discord、Spotify 播放等。 | 配置相应服务、Token/OAuth，并启用 Toolset/插件。 | 各工具保留统一注册与审批边界，`check_fn` 在服务未配置时隐藏 Schema。 | [`tools/homeassistant_tool.py`](../../../tools/homeassistant_tool.py)、[`tools/feishu_drive_tool.py`](../../../tools/feishu_drive_tool.py)、[`tools/discord_tool.py`](../../../tools/discord_tool.py) | [完整工具参考](../../../website/docs/reference/tools-reference.md) | 凭据门控、平台/插件限定。 |

## 5. 自动化、协作与后台任务

这组能力的共同点是任务可能脱离当前聊天调用栈，因此必须显式管理新会话、并发预算、任务状态、投递和恢复。

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| Cron 调度 | 创建、编辑、暂停、恢复、立即运行和删除周期任务，并投递结果。 | `cronjob` 工具、`hermes cron ...` 或 `/cron`。 | Job 持久化后由 Scheduler Tick；每次运行创建新 Session，只注入 Prompt、配置和显式附加 Skills。 | [`cron/`](../../../cron/)、[`tools/cronjob_tools.py`](../../../tools/cronjob_tools.py) | [Cron](../../../website/docs/user-guide/features/cron.md)、[Internals](../../../website/docs/developer-guide/cron-internals.md) | 核心可选；需持续运行 Scheduler/Gateway。 |
| Automation Blueprints | 从参数化模板创建常见自动化。 | 自动化命令或 Dashboard。 | Blueprint 解析为标准 Cron/Hook/投递配置，不建立旁路执行引擎。 | [`tools/blueprints.py`](../../../tools/blueprints.py) | [Blueprints](../../../website/docs/guides/automation-blueprints.md) | 内置可选。 |
| Batch | 并发处理大量输入并生成结构化结果/轨迹。 | Batch Runner 配置。 | 每项输入使用隔离上下文，Runner 管理 Worker、失败和输出合并。 | [`batch_runner.py`](../../../batch_runner.py) | [Batch](../../../website/docs/user-guide/features/batch-processing.md) | 核心；注意成本和限流。 |
| 子智能体委派 | 把独立子任务交给隔离 Agent，可批量并行。 | `delegate_task`。 | 子 Agent 有独立消息历史、Terminal 和 Toolset，只把最终摘要交回父上下文，并共享总预算。 | [`tools/delegate_tool.py`](../../../tools/delegate_tool.py)、[`tools/async_delegation.py`](../../../tools/async_delegation.py) | [Delegation](../../../website/docs/user-guide/features/delegation.md) | 核心可选；并发/嵌套有限制。 |
| Mixture of Agents | 汇集多个模型/Agent 候选，再由聚合模型综合。 | `hermes moa`。 | Orchestrator 并行生成候选并执行聚合轮。 | [`hermes_cli/moa_cmd.py`](../../../hermes_cli/moa_cmd.py) | [MoA](../../../website/docs/user-guide/features/mixture-of-agents.md) | 内置可选；增加调用量。 |
| Kanban | 用任务、依赖、阻塞、评论、心跳和附件编排多 Agent。 | Kanban Plugin/Profile。 | Board 是持久状态机；Dispatcher 分派 Ready Task，Worker 通过工具回报状态。 | [`plugins/kanban/`](../../../plugins/kanban/)、[`tools/kanban_tools.py`](../../../tools/kanban_tools.py) | [Kanban](../../../website/docs/user-guide/features/kanban.md) | 插件提供。 |
| Background Session | 在独立会话运行 Prompt，不阻塞当前聊天。 | `/background <prompt>`。 | 客户端/Gateway 新建 Session，完成后通知或投递。 | [`gateway/session.py`](../../../gateway/session.py) | [Slash Commands](../../../website/docs/reference/slash-commands.md) | 核心；不共享当前轮隐式上下文。 |
| Hooks 与 Middleware | 在生命周期边界执行日志、过滤、告警、指标或 Guardrail。 | `hermes hooks ...`、配置或 Plugin。 | Runner 按命名事件执行受信任 Hook/Middleware，并控制 Allowlist、超时和结果传播。 | [`gateway/hooks.py`](../../../gateway/hooks.py)、[`docs/middleware/README.md`](../../../docs/middleware/README.md) | [Hooks](../../../website/docs/user-guide/features/hooks.md) | 内置可选/插件提供；属于特权代码。 |
| Webhook | 接收外部事件、过滤/转换 Payload、触发 Agent 和投递结果。 | `hermes webhook ...`。 | HTTP 入口鉴权后创建标准 Session；脚本以 stdin/stdout JSON 做转换或抑制。 | [`gateway/platforms/webhook.py`](../../../gateway/platforms/webhook.py) | [Webhooks](../../../website/docs/user-guide/messaging/webhooks.md) | 核心入口；公开部署需鉴权。 |
| Heartbeat/Watchdog | 区分长任务仍活跃与 Worker/Event Loop 卡死。 | Kanban Heartbeat、Gateway systemd Watchdog。 | Worker 写活性事件；Gateway 仅在事件循环及时调度时向 systemd 续租。 | [`gateway/shutdown_watchdog.py`](../../../gateway/shutdown_watchdog.py) | [Heartbeat](../../../website/docs/user-guide/features/heartbeat.md) | 部署限定。 |

## 6. 消息渠道、语音与媒体

Gateway 统一 Session、权限、命令和 Agent 调用，平台 Adapter 映射消息、线程、媒体、Reaction、Typing 和编辑式 Streaming。能力并不对称，以[平台矩阵](../../../website/docs/user-guide/messaging/index.md#platform-comparison)为准。

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| 每聊天/线程会话 | 为不同用户、频道和 Thread 保持独立上下文。 | 配置平台 Bot/Connector。 | Platform Origin 映射到 Session；管理员才可跨 Origin 浏览。 | [`gateway/session.py`](../../../gateway/session.py) | [Sessions](../../../website/docs/user-guide/messaging/index.md#session-management) | 核心；Thread 模型按平台不同。 |
| Typing、Streaming、Reaction | 展示进度、逐步编辑回复或作 Emoji 反馈。 | 平台 API 支持。 | 统一事件降级成 Typing、Edit、Chunk 或最终发送。 | [`gateway/delivery.py`](../../../gateway/delivery.py) | [Platform Matrix](../../../website/docs/user-guide/messaging/index.md#platform-comparison) | 平台限定。 |
| 图片、文件和语音 | 接收附件作上下文，发送生成媒体或 TTS。 | 媒体权限及相关 Provider。 | Adapter 使用受控缓存；出站按平台选择 Voice Bubble、Attachment 或 Link。 | [`gateway/platforms/media_cache.py`](../../../gateway/platforms/media_cache.py) | [Voice](../../../website/docs/user-guide/features/voice-mode.md) | 平台/大小/格式限定。 |
| Discord Voice | 加入语音频道，转写谈话并播放回复。 | Discord、Transcription、TTS 配置。 | 音频帧转为 Session 输入，TTS 结果送回频道。 | [`plugins/platforms/discord/`](../../../plugins/platforms/discord/) | [Discord](../../../website/docs/user-guide/messaging/discord.md) | 插件和平台限定。 |
| Intentional Silence | 无需回复时抑制投递但保留合法 Assistant Turn。 | 最终内容是支持的 Silence Token。 | Gateway 仅跳过 Delivery；Silence 消息仍进入 Session，保持角色交替。 | [`gateway/delivery.py`](../../../gateway/delivery.py) | [Silence Tokens](../../../website/docs/user-guide/messaging/index.md#intentional-silence-tokens) | 整条回复必须只含 Token。 |
| Pairing/Allowlist/权限 | 控制谁能联系 Bot、谁能执行管理员命令。 | `hermes pairing ...` 和平台策略。 | Origin/用户映射访问级别，命令和跨来源 Session 操作在分发前检查 Scope。 | [`gateway/pairing.py`](../../../gateway/pairing.py) | [Security](../../../website/docs/user-guide/security.md) | 核心安全边界。 |
| Delivery Ledger | 崩溃后重发未确认回复，不重新运行 Agent。 | Gateway 默认策略。 | 持久化未开始/发送中/已送达状态；歧义重发显式标记可能重复，并限制次数/年龄。 | [`gateway/delivery_ledger.py`](../../../gateway/delivery_ledger.py) | [Reliability](../../../website/docs/user-guide/messaging/index.md#delivery-reliability) | 诚实的 at-least-once。 |
| Hermes Relay | 由外部 Connector 持有平台凭据，握手协商能力。 | Relay 配置；实验性。 | Descriptor 动态声明媒体、审批、线程和 Streaming，WebSocket 转发统一事件。 | [`gateway/relay/`](../../../gateway/relay/) | [Relay](../../../website/docs/user-guide/messaging/relay.md) | 实验性；不是聊天平台。 |

### 完整渠道索引

- **协作与社区：** [Telegram](../../../website/docs/user-guide/messaging/telegram.md)、[Discord](../../../website/docs/user-guide/messaging/discord.md)、[Slack](../../../website/docs/user-guide/messaging/slack.md)、[Google Chat](../../../website/docs/user-guide/messaging/google_chat.md)、[Mattermost](../../../website/docs/user-guide/messaging/mattermost.md)、[Matrix](../../../website/docs/user-guide/messaging/matrix.md)、[Microsoft Teams](../../../website/docs/user-guide/messaging/teams.md)。
- **移动/个人通信：** [WhatsApp](../../../website/docs/user-guide/messaging/whatsapp.md)、[WhatsApp Cloud](../../../website/docs/user-guide/messaging/whatsapp-cloud.md)、[Signal](../../../website/docs/user-guide/messaging/signal.md)、[LINE](../../../website/docs/user-guide/messaging/line.md)、[SMS](../../../website/docs/user-guide/messaging/sms.md)、[Email](../../../website/docs/user-guide/messaging/email.md)、[BlueBubbles](../../../website/docs/user-guide/messaging/bluebubbles.md)、[Photon](../../../website/docs/user-guide/messaging/photon.md)、[SimpleX](../../../website/docs/user-guide/messaging/simplex.md)。
- **中国企业与社交：** [DingTalk](../../../website/docs/user-guide/messaging/dingtalk.md)、[Feishu](../../../website/docs/user-guide/messaging/feishu.md)、[WeCom](../../../website/docs/user-guide/messaging/wecom.md)、[WeCom Callback](../../../website/docs/user-guide/messaging/wecom-callback.md)、[Weixin](../../../website/docs/user-guide/messaging/weixin.md)、[QQ](../../../website/docs/user-guide/messaging/qqbot.md)、[Yuanbao](../../../website/docs/user-guide/messaging/yuanbao.md)。
- **家庭、通知、开放协议：** [Home Assistant](../../../website/docs/user-guide/messaging/homeassistant.md)、[ntfy](../../../website/docs/user-guide/messaging/ntfy.md)、[IRC](../../../website/docs/user-guide/messaging/irc.md)、[Raft](../../../website/docs/user-guide/messaging/raft.md)、[Buzz](../../../website/docs/user-guide/messaging/buzz.md)、[A2A](../../../website/docs/user-guide/messaging/a2a.md)。
- **入口与专项管线：** [Webhooks](../../../website/docs/user-guide/messaging/webhooks.md)、[Open WebUI](../../../website/docs/user-guide/messaging/open-webui.md)、[Relay](../../../website/docs/user-guide/messaging/relay.md)、[MS Graph Webhook](../../../website/docs/user-guide/messaging/msgraph-webhook.md)、[Teams Meetings](../../../website/docs/user-guide/messaging/teams-meetings.md)。

## 7. 技能、插件与 MCP 扩展体系

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| Skills | 按需加载流程、脚本、参考资料和模板，形成程序性记忆。 | `/<skill>`、`skill_view`、`hermes skills ...`。 | 启动只暴露名称/描述，选中后把完整 `SKILL.md` 注入当前用户轮，减少常驻 Token。 | [`agent/skill_commands.py`](../../../agent/skill_commands.py)、[`tools/skills_tool.py`](../../../tools/skills_tool.py) | [Skills](../../../website/docs/user-guide/features/skills.md) | 核心扩展；不是原生工具。 |
| Skill 供应链 | 搜索、安装、审计、更新、发布、Diff、卸载、Tap 和 Snapshot。 | `hermes skills ...`。 | Provenance/Manifest 记录来源与本地修改，安装前审计，写入走审批。 | [`hermes_cli/subcommands/skills.py`](../../../hermes_cli/subcommands/skills.py) | [CLI Skills](../../../website/docs/reference/cli-commands.md#hermes-skills) | 远程 Skill 视为不可信。 |
| Bundles 与 Curator | 一次加载多个 Skills；后台审查、改进、归档和回滚 Skills。 | `/bundles`、`/curator`。 | Bundle 只在当前任务加载；Curator 先备份再提议/应用变更。 | [`hermes_cli/curator.py`](../../../hermes_cli/curator.py) | [Curator](../../../website/docs/user-guide/features/curator.md) | 内置可选。 |
| General Plugin | 注册工具、Hooks、配置或生命周期能力。 | `hermes plugins ...`。 | Loader 读取 Manifest，经稳定接口注册；禁用/卸载后恢复核心状态。 | [`plugins/`](../../../plugins/) | [Plugins](../../../website/docs/developer-guide/plugins/index.md) | 第三方产品应独立发布。 |
| Provider Plugins | 扩展模型、记忆、上下文、浏览器、Web、图像、视频、Cron。 | 启用插件并配置凭据。 | Provider 实现稳定接口，由 Orchestrator 选择实例。 | [`plugins/model-providers/`](../../../plugins/model-providers/)、[`plugins/context_engine/`](../../../plugins/context_engine/) | [Plugin Guides](../../../website/docs/developer-guide/plugins/index.md) | 插件/凭据门控。 |
| Platform/Auth/Observability Plugins | 增加渠道、Dashboard 认证和可选观测后端。 | 启用相应插件。 | Platform Registry/Auth Hook/Reporter 接入统一 Gateway 事件；外发遥测需 Opt-in。 | [`plugins/platforms/`](../../../plugins/platforms/)、[`plugins/dashboard_auth/`](../../../plugins/dashboard_auth/)、[`plugins/observability/`](../../../plugins/observability/) | [Built-in Plugins](../../../website/docs/user-guide/features/built-in-plugins.md) | 插件提供。 |
| MCP Client | 连接 stdio/HTTP Server，动态发现外部 Tools/Resources/Prompts。 | `hermes mcp add/login/test/...`。 | Client 协商能力并把远程 Tool 动态注册到 Registry，处理冲突和 Schema 清理。 | [`tools/mcp_tool.py`](../../../tools/mcp_tool.py) | [MCP](../../../website/docs/user-guide/features/mcp.md) | 远端服务不可信。 |
| MCP OAuth/过滤/Sampling | 登录远端、限制工具面、允许 Server 请求 Host 模型采样。 | Server 支持相应能力。 | OAuth 保存授权；过滤在注册前；Sampling 重入受控 Host 接口。 | [`tools/mcp_oauth_manager.py`](../../../tools/mcp_oauth_manager.py) | [MCP Guide](../../../website/docs/guides/use-mcp-with-hermes.md) | 最小化 Scope 和工具面。 |
| MCP Catalog/Serve | 从目录安装 MCP，或把 Hermes 能力作为 MCP Server 暴露。 | `hermes mcp catalog/install/serve`。 | Catalog 管理模板；Serve 通过标准 Transport 暴露选定能力。 | [`mcp_serve.py`](../../../mcp_serve.py) | [MCP CLI](../../../website/docs/reference/cli-commands.md#hermes-mcp) | 需认证/网络边界。 |
| Desktop Plugin SDK/Plugin LLM Access | 增加桌面辅助 UI；让插件通过 Host 统一请求模型。 | Desktop SDK 或 Plugin API。 | UI Contribution 不复制主聊天；LLM Access 复用 Provider、用量和安全策略。 | [`apps/desktop/`](../../../apps/desktop/) | [Desktop SDK](../../../website/docs/developer-guide/desktop-plugin-sdk.md)、[LLM Access](../../../website/docs/developer-guide/plugin-llm-access.md) | 平台/插件限定。 |

### Footprint Ladder

按永久成本选择扩展层级：扩展现有实现 → CLI + Skill → `check_fn` 门控工具 → 独立 Plugin → MCP Server/Catalog → 仅在普遍且不可替代时新增核心工具。

## 8. 配置、身份与模型运行

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| 配置/密钥分离 | 管理行为设置和凭据，支持检查与迁移。 | `config.yaml`；`.env` 只放 API key、Token、Password。 | Loader 合并默认与 Profile 配置；Secret Resolver 只提供秘密。 | [`hermes_cli/config.py`](../../../hermes_cli/config.py) | [Configuration](../../../website/docs/user-guide/configuration.md) | 禁止用新环境变量承载非秘密行为配置。 |
| Setup/Tools UI | 配置模型、Gateway、Web、Browser、TTS、Image 等。 | `hermes setup`、`hermes tools`、`hermes gateway setup`。 | Wizard 写入标准配置/凭据位置并做健康检查。 | [`hermes_cli/setup.py`](../../../hermes_cli/setup.py) | [Quickstart](../../../website/docs/getting-started/quickstart.md) | 部分安装需要网络。 |
| Profiles/多 Gateway | 隔离配置、密钥、会话、Skills 和服务进程。 | `hermes profile ...`、Profile Alias。 | 每个 Profile 有独立 Hermes Home；Clone 只在创建时复制，不动态继承。 | [`hermes_cli/profiles.py`](../../../hermes_cli/profiles.py)、[`hermes_cli/gateway.py`](../../../hermes_cli/gateway.py) | [Profiles](../../../website/docs/user-guide/profiles.md) | 核心隔离边界。 |
| Model Catalog/Provider | 浏览模型能力并选择推理后端。 | `hermes model`、Setup。 | Catalog 合并 Plugin 元数据和认证状态，Runtime Resolver 生成实际 API 配置。 | [`hermes_cli/provider_catalog.py`](../../../hermes_cli/provider_catalog.py) | [Models](../../../website/docs/reference/model-catalog.md) | 动态/凭据门控。 |
| Routing/Proxy/Fallback | 按策略路由模型，使用订阅代理和回退链。 | `config.yaml`、`hermes fallback`。 | Resolver 过滤/排序 Route；只有可恢复 Provider 错误进入下一 Fallback。 | [`hermes_cli/runtime_provider.py`](../../../hermes_cli/runtime_provider.py)、[`hermes_cli/fallback_config.py`](../../../hermes_cli/fallback_config.py) | [Routing](../../../website/docs/user-guide/features/provider-routing.md)、[Fallback](../../../website/docs/user-guide/features/fallback-providers.md) | 内置可选。 |
| Credential Pools/OAuth | 多凭据轮换和 Portal/Codex/xAI 等登录。 | `hermes auth ...`、`hermes login`。 | Pool 跟踪健康/冷却；OAuth 材料由 Auth 层刷新，不写入 Session。 | [`agent/credential_pool.py`](../../../agent/credential_pool.py) | [Credential Pools](../../../website/docs/user-guide/features/credential-pools.md) | 凭据门控。 |
| SOUL/Personality | 定义长期身份风格或 Session Overlay。 | `SOUL.md`、`/personality`。 | SOUL 稳定进入系统 Prompt；Personality 作为会话选择持久化。 | [`agent/system_prompt.py`](../../../agent/system_prompt.py) | [Personality](../../../website/docs/user-guide/features/personality.md) | 不覆盖安全约束。 |
| Skins/Pets | 定制终端视觉、Spinner、品牌和宠物。 | `/skin`、`hermes pets ...`。 | 只影响显示层，不改变模型或权限。 | [`hermes_cli/skin_engine.py`](../../../hermes_cli/skin_engine.py)、[`hermes_cli/pets.py`](../../../hermes_cli/pets.py) | [Skins](../../../website/docs/user-guide/features/skins.md)、[Pets](../../../website/docs/user-guide/features/pets.md) | 客户端限定。 |
| Skills Sync/Projects | 同步 Opt-in Skills；管理命名工作区和多目录。 | `hermes sync ...`、`hermes project ...`。 | Manifest 协调 Pull/Push；Project 元数据绑定 Workspace。 | [`hermes_cli/subcommands/sync.py`](../../../hermes_cli/subcommands/sync.py)、[`hermes_cli/projects_cmd.py`](../../../hermes_cli/projects_cmd.py) | [CLI Reference](../../../website/docs/reference/cli-commands.md) | 只同步显式选择内容。 |

## 9. 安全、可靠性与运维

| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
| 命令/写审批 | 危险 Shell、文件和 Skill 写入前确认具体请求/Diff。 | Approval UI、`/approve`、Skills Pending。 | Threat Pattern/Write Gate 先暂停；审批可绑定具体请求、会话命令模式或显式永久 Allowlist，Checkpoint 提供恢复。 | [`tools/approval.py`](../../../tools/approval.py)、[`tools/write_approval.py`](../../../tools/write_approval.py) | [Approval Flow](../../../website/docs/developer-guide/tools-runtime.md#the-dangerous_patterns-approval-flow) | 核心安全边界。 |
| Pairing/Scope | 限制远程用户、群聊和管理员命令。 | Pairing、Allowlist、Admin 配置。 | Origin Identity 映射访问级别，分发前检查权限。 | [`gateway/pairing.py`](../../../gateway/pairing.py) | [Security](../../../website/docs/user-guide/security.md) | 核心安全边界。 |
| Sandbox/远程环境 | 限制文件、进程和网络影响范围。 | Docker/SSH/Modal 等 Environment。 | 统一 Environment API 定向执行，Host 侧仍做路径和审批检查。 | [`tools/environments/`](../../../tools/environments/) | [Docker](../../../website/docs/user-guide/docker.md) | 强度取决于 Backend。 |
| Egress/Iron Proxy | 限制目标和降低 SSRF/外泄。 | `hermes proxy ...`、Egress 配置。 | URL/DNS/IP 先过策略，受管代理执行连接。 | [`hermes_cli/proxy_cli.py`](../../../hermes_cli/proxy_cli.py)、[`tools/url_safety.py`](../../../tools/url_safety.py) | [Egress](../../../website/docs/user-guide/egress/index.md) | 不可绕过。 |
| Secret Sources | 从 Bitwarden、1Password 或命令 Provider 获取秘密。 | `hermes secrets ...`。 | Resolver 运行时按名获取秘密；行为设置仍来自 `config.yaml`。 | [`hermes_cli/secrets_cli.py`](../../../hermes_cli/secrets_cli.py) | [Secrets](../../../website/docs/user-guide/secrets/index.md) | Provider 是特权代码。 |
| 安全审计 | 检查 URL、路径、Skill AST、配置和安全公告。 | Skill Audit、`hermes security ...`。 | 规范化和静态规则在执行/安装前报告证据。 | [`tools/skills_ast_audit.py`](../../../tools/skills_ast_audit.py)、[`hermes_cli/security_audit.py`](../../../hermes_cli/security_audit.py) | [Security](../../../website/docs/user-guide/security.md) | 不替代人工审阅。 |
| Logs/Debug/Doctor | 筛选多类日志，检查安装并生成默认脱敏诊断包。 | `hermes logs`、`hermes debug`、`hermes doctor`、`hermes status`、`hermes dump`。 | Profile-aware Logger + 只读诊断采集；上传前 Secret Redaction。 | [`hermes_logging.py`](../../../hermes_logging.py) | [CLI Reference](../../../website/docs/reference/cli-commands.md) | 日志可能含敏感上下文。 |
| Prompt Size | 分析系统 Prompt、Memory、Skills 和 Tool Schema Token。 | `hermes prompt-size`。 | 复用真实 Prompt Builder/Tokenizer 输出来源 Breakdown。 | [`hermes_cli/prompt_size.py`](../../../hermes_cli/prompt_size.py) | [Prompt Size](../../../website/docs/reference/cli-commands.md#hermes-prompt-size) | 只读估算。 |
| Monitoring/Observability | 查看健康、Metrics、Trace；可接外部后端。 | `hermes monitoring ...`、Observability Plugin。 | 核心暴露本地事件；只有 Opt-in 插件才外发。 | [`plugins/observability/`](../../../plugins/observability/) | [Monitoring](../../../docs/observability/monitoring.md) | 外发需显式选择。 |
| Backup/Import/Migration | 备份恢复 Hermes Home，从旧实例、OpenClaw、Claude Code/Codex 导入。 | `hermes backup`、`hermes import`、`hermes claw migrate`、`hermes import-agent`。 | 先 Dry Run/冲突分析和备份，再有选择应用；秘密迁移需额外开关。 | [`hermes_cli/backup.py`](../../../hermes_cli/backup.py)、[`hermes_cli/migrate.py`](../../../hermes_cli/migrate.py) | [Migration](../../../website/docs/user-guide/migration.md) | 备份可能含秘密。 |
| Update | 检查/安装更新，升级前备份并协调 Gateway。 | `/update`、`hermes update ...`。 | Update Lock 防并发；备份后更新并通过 IPC 报告进度。 | [`hermes_cli/update_cmd.py`](../../../hermes_cli/update_cmd.py) | [Updating](../../../website/docs/getting-started/updating.md) | 修改安装环境。 |
| DB/Checkpoint 维护 | 修复恢复 DB、优化 FTS/VACUUM，清理 Checkpoint。 | `hermes sessions ...`、`hermes checkpoints ...`。 | 先备份或输出独立恢复库，再执行维护/GC。 | [`hermes_cli/session_recovery.py`](../../../hermes_cli/session_recovery.py) | [CLI Reference](../../../website/docs/reference/cli-commands.md) | 破坏性清理需确认。 |
| Gateway 服务/Watchdog | 安装、启停、检查系统服务和 Event Loop 活性。 | `hermes gateway install/start/status/...`。 | 生成 Profile-aware Service；可选 systemd Watchdog 按事件循环健康续租。 | [`hermes_cli/subcommands/gateway.py`](../../../hermes_cli/subcommands/gateway.py) | [Gateway Commands](../../../website/docs/user-guide/messaging/index.md#gateway-commands) | 平台限定。 |
| Dashboard Auth | 保护非 loopback HTTP、REST、PTY、WebSocket。 | Auth Provider/`dashboard register`。 | HTTP 与 WS 共用 Auth Policy；公开绑定没有 Insecure Bypass。 | [`plugins/dashboard_auth/`](../../../plugins/dashboard_auth/) | [Dashboard](../../../website/docs/user-guide/features/web-dashboard.md) | 公开部署强制认证。 |

## 10. 动态目录与完整索引

| 动态目录 | 单一事实来源 | 说明 |
|---|---|---|
| CLI/斜杠命令 | [CLI](../../../website/docs/reference/cli-commands.md)、[Slash](../../../website/docs/reference/slash-commands.md)、[`commands.py`](../../../hermes_cli/commands.py) | 中央 Registry 派生多客户端行为。 |
| Tools/Toolsets | [Tools](../../../website/docs/reference/tools-reference.md)、[Toolsets](../../../website/docs/reference/toolsets-reference.md) | 可见性受平台和 `check_fn` 影响。 |
| Models/Providers | [Model Catalog](../../../website/docs/reference/model-catalog.md)、[`plugins/model-providers/`](../../../plugins/model-providers/) | 认证与插件决定实际可用项。 |
| MCP | [MCP Config](../../../website/docs/reference/mcp-config-reference.md)、`hermes mcp catalog` | 连接时动态发现。 |
| Skills | [Built-in](../../../website/docs/reference/skills-catalog.md)、[Optional](../../../website/docs/reference/optional-skills-catalog.md)、[`skills/`](../../../skills/)、[`optional-skills/`](../../../optional-skills/) | 安装和平台配置决定集合。 |
| Platforms/Plugins | [Messaging Matrix](../../../website/docs/user-guide/messaging/index.md#platform-comparison)、[Built-in Plugins](../../../website/docs/user-guide/features/built-in-plugins.md)、[`plugins/`](../../../plugins/) | 不冻结数量。 |

## 功能如何组合

| 目标 | 能力链 | 关键边界 |
|---|---|---|
| 个人编码助手 | CLI/TUI → Context/`@` → File/Terminal → Approval → Checkpoint → Session/Memory | 长期事实才写 Memory。 |
| 定时简报 | Web → Skill → Cron → 新 Session → Gateway Delivery | 明确时区、来源和附加 Skills。 |
| 团队 Bot | Profile → Platform → Pairing → Thread Session → Logs/Backup | 远程入口不可成为公共 Shell。 |
| 桌面工作台 | Desktop → JSON-RPC → Projects → File/Terminal/Preview → SessionDB | Pane 工具仍经过后端权限。 |
| 第三方扩展 | Footprint Ladder → Plugin/MCP → Gate → E2E Load Test → 独立发布 | 卸载后核心恢复。 |

## 盘点边界

- 覆盖用户可感知功能族，不复制每个参数、JSON Schema、模型名或 Skill 正文。
- 插件/实验能力不代表每个安装默认存在。
- 文档不能替代真实凭据、平台和网络环境的端到端验证。
- 与当前注册表冲突时，以代码和自动生成参考页为准，并更新文档而非增加快照测试。

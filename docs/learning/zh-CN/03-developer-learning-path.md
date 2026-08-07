# 二次开发者与贡献者学习路线

> 目标是从“能调用”进阶到“能选择正确扩展点并维护核心不变量”。返回[学习中心](README.md)，能力入口见[功能清单](01-feature-inventory.md)。

## 路线总览

| 等级 | 主线 | 可验证产出 |
|---|---|---|
| L0 | 从每个入口追到 `AIAgent` | 一张入口—核心调用图 |
| L1 | 运行最小 Python 集成 | 临时 Hermes Home 中的一轮/多轮结果 |
| L2 | Prompt、Provider、Tool、Session 主链 | 一次工具轮次的消息/持久化追踪 |
| L3 | Skill、Plugin、MCP、Provider、Platform 扩展 | 一份有依据的 Footprint 决策和真实加载测试 |
| L4 | 缓存、角色、安全、可靠性和贡献规范 | 一份不变量检查表与 E2E 证据 |

不要从 `run_agent.py` 第一行读到最后一行。先追踪一条具体数据流，再围绕边界文件展开。推荐搭配[架构总览](../../../website/docs/developer-guide/architecture.md)和[核心原理](04-core-principles-and-labs.md)。

## 开发环境与阅读方法

使用仓库支持的环境，而不是自行拼装另一套依赖：

```bash
uv sync
uv run python -c "from run_agent import AIAgent; print(AIAgent.__name__)"
```

若 checkout 已提供 `.venv`，项目测试脚本会优先使用它。阅读与实验遵守四条规则：

1. 用 `rg` 从符号/命令入口追调用者，不以目录名猜运行链路。
2. Bug 修复先在当前 `develop` 复现，并指出表现的具体行。
3. 用 `git log -p -S '<symbol>'` 阅读原提交意图，避免修复一个有意边界。
4. 任何会写真实用户状态的实验都使用临时 Hermes Home；不把秘密写进脚本或测试 Fixture。

## L0：从入口追到 AIAgent

### 先修知识

- 熟悉 Python 模块、函数调用、同步/异步边界和基本 TypeScript/React 阅读。
- 已阅读根目录 [`AGENTS.md`](../../../AGENTS.md) 的架构原则。
- 能使用 `rg`、`git log`、`git diff` 和测试命令。

### 学习目标

- 找到 CLI、Gateway、TUI Gateway、API Server、ACP、Batch 和 Desktop 后端入口。
- 解释哪些组件持有 `AIAgent`，哪些只负责传输或渲染。
- 画出“用户输入 → Agent → Provider/Tool → 持久化/展示”的调用图。

### 核心原理

Hermes 采用多入口、单核心：入口层解决协议、UI、权限和 Session Origin；`AIAgent` 解决 Prompt、模型、工具循环和预算。复用核心不等于所有 UI 相同：Dashboard 通过 PTY 嵌入 TUI，Desktop 通过 WebSocket JSON-RPC 使用独立 React 聊天面。

阅读时先区分三类边界：

- **进程入口：** 解析参数、启动服务或客户端。
- **Adapter/Transport：** 把外部协议转换为 Hermes 事件。
- **Agent Core：** 保持消息、调用模型和工具。

### 源码阅读顺序

1. [`pyproject.toml`](../../../pyproject.toml) 的 `hermes = "hermes_cli.main:main"`。
2. [`hermes_cli/main.py`](../../../hermes_cli/main.py) 的顶层子命令注册与 CLI 选择。
3. [`cli.py`](../../../cli.py) 的 `HermesCLI` 到 `AIAgent`。
4. [`gateway/run.py`](../../../gateway/run.py) 与 [`gateway/session.py`](../../../gateway/session.py)。
5. [`tui_gateway/server.py`](../../../tui_gateway/server.py) 的 JSON-RPC 方法和事件。
6. [`gateway/platforms/api_server.py`](../../../gateway/platforms/api_server.py) 与 [`acp_adapter/server.py`](../../../acp_adapter/server.py)。
7. [`batch_runner.py`](../../../batch_runner.py)。
8. 最后定位 [`run_agent.py`](../../../run_agent.py) 的 `AIAgent.__init__`、`chat()`、`run_conversation()`。

### 动手实验

1. 使用以下检索建立入口表：

   ```bash
   rg -n 'AIAgent\(' cli.py gateway tui_gateway acp_adapter batch_runner.py
   rg -n 'run_conversation\(|\.chat\(' cli.py gateway tui_gateway acp_adapter batch_runner.py
   ```

2. 对每个命中记录：谁构造 Agent、谁提供 Session ID、谁实现 Streaming/Approval、谁写 SessionDB。
3. 从 Desktop 的 [`apps/desktop/electron/`](../../../apps/desktop/electron/) 追到 `hermes serve`，再从 Renderer 追到 [`apps/shared/`](../../../apps/shared/) Gateway Client。
4. 画出至少两条完整链：经典 CLI 和 Gateway Message；标注它们在哪一层汇合。

### 验收标准

- 调用图至少包含入口、Session、`AIAgent`、Provider、Tool Registry 和持久化。
- 能准确说明 Dashboard 主聊天与 Desktop 主聊天的不同传输和 UI 所有权。
- 能指出批处理为什么应为每项输入创建隔离 Agent，而不是共享可变实例。
- 面对新入口需求时，能先寻找现有 Adapter/Transport，而不是复制 Agent Loop。

### 常见误区

- 把 `hermes_cli/main.py` 当成 Agent 实现。
- 在 Dashboard React 中重写 TUI Transcript/Composer。
- 只搜索类定义，不搜索实例化和回调注入位置。
- 看到同步 `AIAgent` 就在任意异步 Loop 中直接阻塞调用。

### 下一步

完成调用图后进入 L1，用最小程序化调用验证自己对构造参数和状态隔离的理解。

## L1：运行最小程序化调用

### 先修知识

- 已完成 L0 调用图。
- 有一个可合法调用的模型 Provider；凭据通过现有安全方式提供。
- 理解环境变量 `HERMES_HOME` 的真实用途，不改写 Shell 的 `$HOME`。

### 学习目标

- 使用 `AIAgent.chat()` 和 `run_conversation()`。
- 控制 Toolsets、Memory、Context Files 和输出噪声。
- 维护合法多轮 History，并为并发任务创建独立实例。

### 核心原理

`chat()` 是“给字符串、取最终字符串”的便利接口；`run_conversation()` 返回 `final_response` 和完整 `messages`，适合 UI、评测和调试。`AIAgent` 持有会话、工具 Session、迭代计数等可变状态，不应跨线程/任务共享。

`quiet_mode=True` 是嵌入场景的输出边界；`enabled_toolsets` 是最小授权，`disabled_toolsets` 是从较大集合中剔除。`skip_memory`、`skip_context_files` 用来建立可复现实验，不应被误解为生产默认值。

### 源码阅读顺序

1. [Python Library Guide](../../../website/docs/guides/python-library.md) 的 Basic Usage 和 Full Conversation Control。
2. [`run_agent.py`](../../../run_agent.py) 中 `chat()` 如何调用 `run_conversation()`。
3. `AIAgent.__init__` 中 `model`、`api_mode`、`session_id`、Toolset、Memory/Context 开关。
4. [`agent/iteration_budget.py`](../../../agent/iteration_budget.py) 和 [`agent/turn_context.py`](../../../agent/turn_context.py)。

### 动手实验

1. 创建临时状态目录，不污染真实 Profile：

   ```bash
   HERMES_TEST_HOME="$(mktemp -d)"
   export HERMES_TEST_HOME
   ```

2. 保存以下脚本为练习目录中的 `library_smoke.py`，把模型名替换为已配置 Provider 的有效模型；凭据仍通过安全环境/登录提供：

   ```python
   from run_agent import AIAgent

   agent = AIAgent(
       model="your-provider/your-model",
       quiet_mode=True,
       enabled_toolsets=[],
       skip_context_files=True,
       skip_memory=True,
   )

   result = agent.run_conversation("只回复：library-ok")
   print(result["final_response"])
   print([message["role"] for message in result["messages"]])
   ```

3. 运行时只为该进程设置 Hermes Home：

   ```bash
   HERMES_HOME="$HERMES_TEST_HOME" uv run python library_smoke.py
   ```

4. 用同一 `agent` 连续执行两轮，并把第一轮 `messages` 作为第二轮 `conversation_history`；验证原列表未被就地修改。
5. 创建两个不同 `AIAgent` 实例并赋不同 `session_id`，串行运行；不要用一个实例做线程并发。

### 验收标准

- 输出包含最终文本和一组合法 Role 序列。
- 练习后真实 Hermes Home 没有新增测试 Session、Memory 或轨迹。
- 能说明 `model`、Provider、`api_mode`、Toolsets、Session ID 分别解决什么问题。
- 能解释为什么 `quiet_mode=True` 不是静默错误，为什么每个并发任务需要新 Agent。

### 常见误区

- 使用不存在的“pip 发布版”假设；当前指南要求从 checkout 用 `uv sync`。
- 把 API key 写进 Python 文件。
- 给同一可变 Agent 实例并发发送请求。
- 把 `enabled_toolsets=[]` 理解为系统永远无工具；这是本实验的最小隔离。

### 下一步

保留脚本作为 Smoke Test，进入 L2，开启一个无副作用工具并追踪完整 Tool Call 和 SessionDB 链路。

## L2：掌握提示词、模型、工具和会话主链路

### 先修知识

- 已完成 L1，并能打印完整 `messages`。
- 熟悉 OpenAI 风格的 system/user/assistant/tool 消息和 Tool Call ID。
- 能使用临时 SQLite 数据库做只读检查。

### 学习目标

- 按真实运行顺序理解 Prompt、Provider、Agent Loop、Tool Dispatch 和 SessionDB。
- 追踪一次“用户消息 → 模型工具调用 → 工具结果 → 最终回复”。
- 理解压缩、缓存和角色交替为何是运行契约。

### 核心原理

主链路不是文件依赖树，而是一条状态机：Prompt Builder 产生稳定前缀；Runtime Provider 选择 API/Model；`run_conversation()` 发送消息；模型返回 Tool Call；Dispatcher 根据 Registry 调 Handler；Tool Result 进入下一次模型请求；最终 Assistant Turn 与元数据写入 SessionDB。

Tool Schema 是每次模型调用的上下文成本。Toolset 先限制类别，`check_fn` 再隐藏未配置服务。SessionDB 保存真实历史，Memory 和 Context Files 不替代它；压缩只重写活跃消息视图，原内容可软归档搜索。

### 源码阅读顺序

1. [`agent/prompt_builder.py`](../../../agent/prompt_builder.py)
2. [`hermes_cli/runtime_provider.py`](../../../hermes_cli/runtime_provider.py)
3. [`run_agent.py`](../../../run_agent.py)
4. [`model_tools.py`](../../../model_tools.py)
5. [`tools/registry.py`](../../../tools/registry.py)
6. [`toolsets.py`](../../../toolsets.py)
7. [`hermes_state.py`](../../../hermes_state.py) 与 `hermes_state_*` 拆分模块
8. [`agent/context_compressor.py`](../../../agent/context_compressor.py)

配套阅读：[Prompt Assembly](../../../website/docs/developer-guide/prompt-assembly.md)、[Agent Loop](../../../website/docs/developer-guide/agent-loop.md)、[Tools Runtime](../../../website/docs/developer-guide/tools-runtime.md)、[Session Storage](../../../website/docs/developer-guide/session-storage.md)。

### 动手实验

1. 在临时 Hermes Home 中启用只读或无副作用工具，例如 Session Search 的 Browse，运行一轮明确要求调用该工具的 Prompt。
2. 打印每条消息的 `role`、Tool Call ID 和 Tool Result ID，不打印含秘密的正文。
3. 在日志中按临时 `session_id` 搜索 Tool Start/Complete 和模型轮次。
4. 用 SQLite 只读连接检查该 Session 的消息 Role、顺序和 Active 状态；不要手工更新数据库。
5. 运行 `hermes prompt-size --json`（使用相同临时 Profile 配置）比较启用/禁用一个大 Toolset 后的 Schema Token 变化。
6. 阅读一个带 `check_fn` 的工具：分别在前置条件缺失/满足时获取 Tool Definitions，证明 Schema 可见性变化。
7. 构造接近压缩阈值的测试只能使用专用 Fixture/测试套件，不用真实昂贵对话堆 Token；检查压缩后的角色序列仍合法。

### 验收标准

- 能从消息列表指出模型调用次数、工具调用次数和最终 Assistant Turn。
- Tool Call ID 与 Tool Result ID 一一对应，没有连续同角色违规。
- 能从 Registry 解释某工具为何可见/不可见，而不是只看模块能否 Import。
- 能指出 SessionDB、Memory、Context 和压缩摘要各自的数据所有权。
- 至少运行一条真实加载路径测试，不只依赖 Mock。

### 常见误区

- 把 Tool Handler 单元测试通过当作工具已经被 Agent 发现。
- 用枚举数量断言模型/工具/配置版本。
- 压缩时只看 Token 变少，不检查 Role 和 Tool Call 配对。
- 把辅助压缩模型的上下文窗口当成主模型压缩触发阈值。

### 下一步

进入 L3。对每个新需求先走 Footprint Ladder，只有选定扩展面后才写代码。

## L3：选择正确的扩展点

### 先修知识

- 已完成 L2 主链路追踪。
- 能区分模型需要“知识指导”还是“结构化行动能力”。
- 已阅读 Plugin、Skill、MCP 和 Platform/Provider 专项文档。

### 学习目标

- 用 Footprint Ladder 为需求选择最小永久表面。
- 能实现/评审 CLI + Skill、门控 Tool、Plugin 或 MCP 的边界。
- 为 Provider/Platform 扩展设计真实加载 E2E，而不是只 Mock Adapter。

### 核心原理

扩展层级从低成本到高成本：

| 层级 | 适用条件 | 永久成本 |
|---|---|---|
| 扩展现有代码 | 已有能力的变体 | 无新表面 |
| CLI + Skill | 配置/状态/基础设施可由命令表达 | 不增加模型 Tool Schema |
| `check_fn` 门控 Tool | 需要结构化参数/结果，且只在服务存在时有意义 | 未配置时零 Schema |
| 独立 Plugin | 小众、第三方或用户特有能力 | 核心外维护 |
| MCP Server | 结构化工具且可供多个 MCP Host 复用 | 动态连接 |
| 核心 Tool | 几乎人人需要且无法由终端/文件/MCP 完成 | 每轮都付 Schema 成本 |

当三个以上扩展竞争同一类别，应先设计 ABC + Orchestrator，把现有内置实现包装成第一个 Provider，再让竞争实现变成 Plugins。

### 源码阅读顺序

1. [Extending CLI](../../../website/docs/developer-guide/extending-the-cli.md) 与 [`hermes_cli/commands.py`](../../../hermes_cli/commands.py)。
2. [Creating Skills](../../../website/docs/developer-guide/creating-skills.md)。
3. [Adding Tools](../../../website/docs/developer-guide/adding-tools.md) 和 Registry 的 `check_fn`。
4. [Plugin System](../../../website/docs/developer-guide/plugins/index.md)。
5. [MCP](../../../website/docs/user-guide/features/mcp.md)。
6. [Adding Providers](../../../website/docs/developer-guide/adding-providers.md)、[Memory Provider](../../../website/docs/developer-guide/memory-provider-plugin.md)、[Context Engine](../../../website/docs/developer-guide/context-engine-plugin.md)。
7. [Adding Platform Adapters](../../../website/docs/developer-guide/adding-platform-adapters.md)。
8. [Desktop Plugin SDK](../../../website/docs/developer-guide/desktop-plugin-sdk.md)。

### 动手实验

先为三个需求作设计决策，不写核心代码：

1. “管理一个订阅和本地配置”选择 CLI + Skill，因为动作可通过命令、文件和现有认证表达。
2. “只有 Home Assistant 已配置时，让模型结构化调用设备动作”选择 `check_fn` 门控 Tool，因为需要动态 Schema 且未配置时应零成本。
3. “连接某第三方 SaaS”选择独立 Plugin Repo 或 MCP Server，而不是在核心 `plugins/` 下长期维护厂商产品。

随后选择其中一个低风险方案实现 Spike：

- 为 CLI + Skill 验证命令注册、Alias、帮助、配置写入和 Skill Slash 加载；或
- 为 Plugin/MCP 验证 Manifest/Handshake、结构化输入输出、服务缺失门控、卸载恢复。

E2E 必须使用临时 Hermes Home，从真实 Loader/Registry 入口启动，证明能力实际可见且核心文件无需专用分支。

### 验收标准

- 每个需求都有“为什么不用更低/更高一层”的明确理由。
- 未配置服务时，门控 Tool 不出现在模型 Schema 中。
- Plugin/MCP 卸载或断开后，核心 Toolset 和 Prompt 恢复。
- 第三方产品扩展没有修改核心文件来注册特例。
- 测试覆盖真实安装/发现/配置传播链路。

### 常见误区

- 只因为结构化参数方便就新增核心 Tool。
- 用环境变量公开非秘密 Timeout/Threshold/Feature Flag。
- 为未来可能的消费者增加 Hook。
- Plugin 直接修改核心文件或把厂商实现提交进核心树。
- Instructional Skill Loader 增加分页，诱导模型只读第一页。

### 下一步

进入 L4，把 Spike 放到缓存、角色、安全、并发和可靠性不变量下审查，再决定是否形成正式贡献。

## L4：维护缓存、安全与可靠性不变量

### 先修知识

- 已完成 L3 扩展决策和一个真实加载 Spike。
- 能运行相关单元测试和端到端测试。
- 能阅读原始 Commit Intent 和当前 `develop` 行为。

### 学习目标

- 维护系统 Prompt 字节稳定和严格消息角色交替。
- 保护 Tool Schema 面积、审批、Sandbox、Egress 和 Secret 边界。
- 正确处理压缩、并发、中断、Session 和 Delivery 失败模型。
- 形成可合并的复现、测试和贡献证据。

### 核心原理

四组不变量共同定义核心质量：

1. **缓存：** 长会话复用稳定前缀；除压缩边界外不重建系统 Prompt。
2. **协议：** 不产生连续同 Role；Tool Call/Result 配对；不中途注入合成 User Turn。
3. **安全：** 审批不被“修复”移除；路径、网络和秘密使用最小权限。
4. **可靠性：** 共享预算、取消和并发有所有者；Session/Delivery 状态能在崩溃后解释。

行为契约应断言关系，例如“未配置服务时工具不可见”“每个 Tool Result 有对应 Call”，而不是冻结当前模型数、工具数或配置版本。

### 源码阅读顺序

1. [`AGENTS.md`](../../../AGENTS.md) 的 Contribution Rubric、Footprint Ladder 和 Premise Verification。
2. [Context Compression and Caching](../../../website/docs/developer-guide/context-compression-and-caching.md)。
3. [Agent Loop · Alternation](../../../website/docs/developer-guide/agent-loop.md#message-alternation-rules)。
4. [Tools Runtime · Approval/Concurrency](../../../website/docs/developer-guide/tools-runtime.md)。
5. [Session Storage](../../../website/docs/developer-guide/session-storage.md) 与 [Gateway Internals](../../../website/docs/developer-guide/gateway-internals.md)。
6. [Egress Internals](../../../website/docs/developer-guide/egress-internals.md)。
7. [`tests/`](../../../tests/) 中相邻真实路径测试。

### 动手实验

1. 对 Spike 前后连续两轮的系统 Prompt 做 SHA-256；除明确压缩外，内容应字节相同。
2. 打印所有 Message Role、Tool Call ID、Tool Result ID，写一个关系断言而非 Snapshot。
3. 在服务未配置、凭据过期、调用超时和用户拒绝审批四种路径下运行测试，确认错误分类不同。
4. 模拟中断发生在模型等待、Tool 执行和最终 Delivery 三个边界；检查清理所有者和可恢复状态。
5. 对涉及配置传播、文件/网络 I/O、Provider/Platform 加载的变更运行临时 Hermes Home E2E。
6. 使用 `git log -p -S '<changed-symbol>' -- <path>` 写一段“原意—现象—修复作用行”的说明。

### 验收标准

- 能提供 Prompt 哈希、Role/Tool 配对和真实加载测试结果。
- 能指出 Bug 在当前 `develop` 的具体表现行，修复确实改变该路径行为。
- 安全修复保留原功能目的，没有通过删除功能获得“安全”。
- 没有新增非秘密 `HERMES_*` 配置或无消费者 Hook。
- 测试断言行为关系，不冻结易变枚举。
- 外部贡献能通过 Cherry-pick/Rebase 保留作者署名。

### 常见误区

- 仅凭合理叙述接受 Bug 前提，不复现实际路径。
- 用 Mock 证明配置传播或 Loader 正常。
- 在循环中动态替换 Toolset/系统 Prompt，破坏缓存。
- 将发送中崩溃描述为 exactly-once。
- 因修复困难而缩小/删除功能本身。

### 下一步

按“贡献前检查表”完成自审，再执行项目对应测试、全局格式检查和代码评审流程。

## 贡献前检查表

- [ ] 已在当前 `develop` 复现，记录输入、环境、表现和具体代码行。
- [ ] 已用 `git log -p -S` 阅读相关符号的原提交意图。
- [ ] 已选择 Footprint Ladder 中最低可行层级。
- [ ] 未在会话中重建系统 Prompt 或产生非法 Role 顺序。
- [ ] 未新增非秘密 `HERMES_*` 环境变量。
- [ ] 未新增无真实消费者的 Hook/Callback。
- [ ] 第三方产品扩展位于独立 Plugin Repo 或 MCP，不进入核心树。
- [ ] 测试断言行为契约，不冻结 Model/Tool/Config 枚举数量。
- [ ] 配置、权限、Provider、文件或网络路径已用临时 Hermes Home 做 E2E。
- [ ] 危险写入、Sandbox、Egress 和 Secret 边界未被绕过。
- [ ] 已运行与风险成比例的测试并保存新鲜输出。
- [ ] 外部作者贡献通过 Git 历史保留署名。

# Hermes 核心原理与最小实验

> 本篇按运行数据流解释系统，而不是按目录树介绍文件。实验默认使用临时 Hermes Home、无副作用任务和测试凭据。返回[学习中心](README.md)。

## 1. 一次请求的完整旅程

一次请求经历九个边界：

```text
① 用户输入/外部事件
        ↓
② CLI、TUI、Desktop、ACP、API 或 Platform Adapter
        ↓
③ Session Origin、权限、工作目录、Profile 解析
        ↓
④ Prompt Assembly：稳定系统前缀 + 合法历史 + 当前 User Turn
        ↓
⑤ Provider Runtime：Route、Model、API Mode、Credential
        ↓
⑥ Model Response ──最终文本──────────────┐
        │                                  │
        └─Tool Call → Registry/Gate/Handler│
                       ↓                   │
                  Tool Result ──回到④/⑤   │
                                           ↓
⑦ Final Assistant Turn、Usage、事件持久化
        ↓
⑧ Streaming/Render 或 Gateway Delivery Ledger
        ↓
⑨ 用户看到结果；Session 可恢复、搜索和继续
```

入口层决定“谁在什么地方说了什么”，Agent Core 决定“模型如何思考和行动”，Provider/Tool 决定“调用哪个外部能力”，持久化/投递决定“结果能否解释和恢复”。将这些职责混在一起，会产生三类典型 Bug：UI 复制 Agent 状态、Provider 特例污染循环、外部发送失败导致重跑有副作用的任务。

关键源码导航：

- 入口与核心：[`cli.py`](../../../cli.py)、[`gateway/run.py`](../../../gateway/run.py)、[`tui_gateway/server.py`](../../../tui_gateway/server.py)、[`run_agent.py`](../../../run_agent.py)
- Prompt/Provider：[`agent/prompt_builder.py`](../../../agent/prompt_builder.py)、[`hermes_cli/runtime_provider.py`](../../../hermes_cli/runtime_provider.py)
- Tool：[`model_tools.py`](../../../model_tools.py)、[`tools/registry.py`](../../../tools/registry.py)、[`toolsets.py`](../../../toolsets.py)
- State/Delivery：[`hermes_state.py`](../../../hermes_state.py)、[`gateway/delivery_ledger.py`](../../../gateway/delivery_ledger.py)

### 最小观察实验

在临时 Profile 完成一轮只读工具调用，记录而不修改以下字段：入口、Profile、Session ID、消息 Role、Model/API Mode、可见工具名、Tool Call ID、Tool Result ID、最终用量和投递状态。用这份记录画出自己的九阶段图；任何找不到所有者的状态都是需要继续阅读的边界。

## 2. Prompt Assembly 与缓存不变量

系统 Prompt 不是一段固定字符串，而是按稳定顺序组装的前缀。典型组成包括：SOUL/身份、核心约束、平台提示、项目 Context Files、Memory、工具/Skill 提示和当前环境信息。具体顺序以[Prompt Assembly](../../../website/docs/developer-guide/prompt-assembly.md)为准。

对长会话而言，远端 Provider 的 Prefix Cache 近似依赖：

```text
cache_key ≈ hash(system_prompt_bytes + earlier_message_prefix)
```

只要中途改变系统 Prompt、重排旧消息或替换 Tool Schema，后续请求就无法复用之前的前缀。结果不是单纯“慢一点”，而是长会话每轮重复支付大量输入 Token。因此 Hermes 把“会话生命周期内系统 Prompt 字节稳定”视为架构约束。

动态能力如何进入而不破坏前缀：

- Skill 只在需要时作为当前用户轮内容注入，而不是永久塞进系统 Prompt。
- Tool Result 作为协议规定的新消息追加，不回写过去。
- Personality/Model 等会话级切换通过有定义的状态边界处理。
- Context Compression 是允许重建活跃消息前缀的少数边界，并产生明确事件。

稳定不等于永不变化。新 Session、用户明确切换身份/模型、压缩和配置重载可以形成新边界；错误做法是在 Agent Loop 中为了某次工具调用临时改写系统消息，然后下一轮再改回来。

### 实验：比较连续两轮系统前缀

使用 `run_conversation()` 保存两轮 `messages`，只对 Role 为 `system` 的 Content 做 SHA-256：

```python
import hashlib

def system_hash(messages: list[dict]) -> str:
    payload = "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

在没有压缩、配置重载或明确切换的连续两轮中，Hash 应相同。若不同，先 Diff 内容和来源，再判断是否为合法边界；不要为了让测试通过而忽略字段。

## 3. 消息角色交替与工具调用协议

模型 API 不接受任意聊天记录。Hermes 必须维持合法序列，并让 Tool Result 能找到对应的 Assistant Tool Call。

普通轮次：

```text
system → user → assistant
```

带工具轮次（抽象表示）：

```text
system
user
assistant(tool_call id=call_1)
tool(tool_call_id=call_1)
assistant(final text)
```

项目约束强调“不要出现连续同 Role，不要在循环中插入合成 User 消息”。底层 API 对多个 Tool Result、Reasoning 和 Provider 格式可能有适配细节，但上层不应通过随意拼消息来修复它们。

三个不变量：

1. 每个 Tool Result 必须引用当前历史中未消费的 Tool Call ID。
2. Tool Handler 异常也要转换成结构化 Tool Result，模型才能解释失败并继续。
3. 中断、压缩、重试和 Session 恢复后仍要验证 Role/Call 配对，而不仅是“列表非空”。

### 实验：打印角色与调用配对

把临时测试的 `messages` 保存为 JSON 后运行一个只读校验器：

```python
import json
from pathlib import Path

messages = json.loads(Path("messages.json").read_text())
open_calls: set[str] = set()

for index, message in enumerate(messages):
    role = message["role"]
    print(index, role)
    for call in message.get("tool_calls", []) or []:
        open_calls.add(call["id"])
    if role == "tool":
        call_id = message["tool_call_id"]
        assert call_id in open_calls, (index, call_id)
        open_calls.remove(call_id)

assert not open_calls, open_calls
```

再检查相邻 Role 是否符合当前 API Adapter 的契约。不要用完整消息 Snapshot；用“Call 与 Result 的关系”做断言，模型文字变化不会破坏测试。

## 4. Provider Runtime 与 API 模式

Provider Runtime 把“用户选择一个模型”解析为一组实际运行参数：Provider、Base URL、Credential、Model Slug、API Mode、上下文窗口和能力元数据。模型名本身不足以决定调用方式。

| 层 | 职责 | 常见变化 |
|---|---|---|
| Provider Plugin | 登录、模型目录、Endpoint 和厂商能力 | OpenRouter、Anthropic、Codex、Vertex 等 |
| Runtime Resolver | 合并 Profile、显式参数、路由、Proxy、Fallback | 当前 Route |
| API Mode/Transport | 将协议流转换为统一文本、Reasoning、Tool Call | Chat Completions、Anthropic、Codex Responses/App Server |
| Main Model | 运行 Agent Loop、决定工具和最终回复 | 当前 Session 的核心模型 |
| Auxiliary Model | 视觉、压缩、审批等指定子任务 | 不拥有主对话上下文窗口 |
| Fallback | 可恢复 Provider 错误后的备用 Route | 不处理用户拒绝/安全错误 |

压缩阈值根据**主模型**上下文窗口计算。辅助压缩模型能否容纳摘要输入是另一个问题，不能用它的窗口替换触发阈值。

Codex App Server 是特殊例子：真实线程上下文由外部 Runtime 持有，Hermes 本地重写消息镜像不能缩小远端线程，因此压缩必须走 App Server 自己的机制。它说明了为什么 Adapter 需要保留协议真实所有权，而不是强行把所有后端做成一样。

### 实验：解析而不调用

1. 在测试 Profile 运行 `hermes model` 和 `hermes status`，记录 Provider、Model、API Mode。
2. 阅读 [`hermes_cli/runtime_provider.py`](../../../hermes_cli/runtime_provider.py) 中该 Provider 的 Resolution Path。
3. 临时配置一个 Fallback，但不制造付费调用；画出主 Route 失败时的顺序。
4. 分别列出 Main、Vision、Compression Model，确认每个模型的职责与上下文窗口没有混用。

验收不是“能列出模型”，而是能解释某次请求最终为何选择这个 Endpoint/API Mode/Credential。

## 5. Tool Registry、Toolsets 与能力门控

工具进入模型上下文需要经过一条过滤管线：

```text
tools/*.py 顶层 registry.register()
        ↓ AST 自动发现并 Import
ToolEntry(name, toolset, schema, handler, check_fn, ...)
        ↓ Toolset 解析（平台默认 + enabled/disabled）
候选工具名
        ↓ check_fn（凭据、Binary、Service）
当前请求可见 Schemas
        ↓ Model Tool Call
handle_function_call → Hook → registry.dispatch → Handler
        ↓
结构化结果或结构化错误
```

为什么要 AST 检查顶层 `registry.register()`：只 Import 真正声明工具的模块，避免把 `tools/` 中每个 Helper 都作为有副作用模块加载。为什么要 Toolsets：用户/平台先选择能力类别。为什么还要 `check_fn`：即使类别启用，服务未配置时也不应把无效 Schema 发给模型。

`check_fn` 的关键语义：

- 返回 False：工具完全不出现在本次 Definitions 中。
- 抛异常：Fail Safe，当作不可用。
- 多个工具共享 Check 时，本次 Definitions 构建缓存结果。
- 它是可用性门，不替代调用时权限、审批或参数校验。

Tool Schema 越多，每轮请求越贵，模型选择也越困难。这是“核心工具门槛高”的直接原因。

### 实验：门控前后工具面

选择 Home Assistant 或另一个明确带 `check_fn` 的服务：

1. 用临时 Profile 且不配置该服务，运行 `hermes tools --summary` 和 `hermes prompt-size --json`，保存可见 Tool/Schema Token。
2. 在隔离测试服务中配置合法凭据，重新启动新的 Agent，再次保存结果。
3. 比较 Definitions：只有该服务相关工具应新增；无关 Toolset 不变化。
4. 移除测试配置并新建 Agent，工具应消失。

验收必须证明“模型 Schema 中是否存在”，仅证明模块能 Import 或 Handler 能直接调用不够。

## 6. 会话、记忆、搜索与上下文压缩

| 状态 | 所有者 | 生命周期 | 主要用途 |
|---|---|---|---|
| SessionDB | SQLite/`hermes_state*` | 对话长期保存 | 消息、元数据、用量、事件、恢复 |
| FTS5 | SessionDB Search | 随数据库维护 | 无 LLM 的真实消息检索 |
| Context Files | 项目目录/Hermes Home | 随目录或文件变化 | 项目规则、身份、构建说明 |
| `MEMORY.md`/`USER.md` | Memory Manager | 跨 Session | 稳定偏好和长期事实 |
| External Memory | Memory Provider Plugin | Provider 定义 | 用户模型、语义检索、外部知识 |
| Compression Summary | 活跃 Session | 达到阈值后 | 用摘要替换旧中段以继续对话 |
| Trajectory | 文件/数据管线 | 显式保存 | 调试、评测、训练数据 |
| Checkpoint | 文件系统 Store | 修改前/保留策略 | 工作区回滚，不回退聊天/外部副作用 |

默认 In-place Compression 保持一个稳定 Session ID：旧活跃消息被软归档，摘要和保留尾部成为活跃视图。Legacy Rotation 才创建带 Parent Link 的新 Session。消费者应观察 Compression Event/Mode，而不是仅比较 Session ID 是否变化。

压缩的四个阶段：

1. 清理受保护尾部之外的大型旧 Tool Result，先做廉价降噪。
2. 保护 System/首轮并从尾部按 Token Budget 确定近期边界。
3. 用辅助模型总结中段，保留事实、决策、未完成工作和关键路径。
4. 组装并验证新活跃消息，持久化 Compression Event 和归档状态。

摘要必然有损，因此不能把它当审计日志；原消息搜索和 Trajectory 才承担取证/复盘用途。

### 实验：临时 Session 生命周期

1. 设置 `HERMES_TEST_HOME="$(mktemp -d)"`，仅用 `HERMES_HOME="$HERMES_TEST_HOME"` 启动练习。
2. 创建包含唯一短语的两轮 Session，运行 `hermes sessions list` 和 Session Search 验证可检索。
3. 归档该 Session，分别测试默认 Browse 与包含归档项的查询边界。
4. 使用测试 Fixture 触发压缩，记录前后 Role、Active/Compacted 状态和 Session ID。
5. 确认 In-place 模式 ID 不变、旧消息仍可搜索；切勿在真实用户数据库上手工修改状态。

## 7. 多入口复用同一核心

| 入口 | 前端/协议所有者 | 到 Python Core 的路径 | 特殊职责 |
|---|---|---|---|
| Classic CLI | Rich + prompt_toolkit | 进程内调用 | Slash、Spinner、Clipboard、Approval |
| Ink TUI | Node React/Ink | stdio JSON-RPC → `tui_gateway` | Transcript、Composer、Prompt UI |
| Dashboard Chat | xterm.js | WebSocket PTY → `hermes --tui` | 浏览器承载真实 TUI |
| Electron Desktop | React + assistant-ui | WebSocket JSON-RPC → `hermes serve` | 独立 Chat、Pane、Project、Plugin UI |
| Gateway Platform | Platform Adapter | Event → Session → `AIAgent` | Pairing、Thread、Media、Delivery |
| API Server | OpenAI-compatible HTTP | Request → Session → `AIAgent` | 协议映射和鉴权 |
| ACP | ACP Adapter | ACP Session/Event → `AIAgent` | IDE Diff、Tool、Terminal 呈现 |
| Batch | Python Runner | 每项输入新 Agent | 并发、轨迹和结构化输出 |

“复用同一核心”意味着 Provider、Tool 和 Session 语义一致；不意味着所有客户端共享同一个进程内状态或 UI Store。Desktop 不依赖 Dashboard 前端；Dashboard 不应复制 TUI 主聊天；Gateway 的 Session Origin 也不等于 CLI 当前 Session。

### 实验：同一命令的三条路径

选择 `/status` 或一个只读 Slash Command：

1. 在 Classic CLI 追 `COMMAND_REGISTRY → resolve_command() → HermesCLI.process_command()`。
2. 在 Gateway 追 `GATEWAY_KNOWN_COMMANDS → dispatch`。
3. 在 Desktop 追客户端 Curated Command → `slash.exec` → `command.dispatch` Fallback。

记录哪些字段来自统一后端、哪些由客户端本地渲染。技能/Quick Commands 属于动态扩展，Desktop Curated UI 可以隐藏噪声，但不能阻断合法扩展命令执行。

## 8. 自动化、并发和可靠投递

### Cron

Cron Job 保存 Prompt、Schedule、Timezone、Skills 和 Delivery Target。到期后创建新 Session，因此结果可审计，但不会神奇继承创建 Job 时的聊天历史。

### Delegation 与 Kanban

Delegation 是父—子上下文关系：父给 Goal/Toolset/Budget，子返回摘要。Kanban 是持久任务图：Task 有 State、Dependency、Comment、Heartbeat、Attachment，可跨进程/Worker 恢复。普通并行子任务不需要 Kanban；需要人工阻塞、依赖和长期调度时才使用 Board。

### Hooks

Hook 在生命周期边界运行，应有真实消费者、超时和失败策略。无消费者 Hook 会永久扩大插件契约。Hook 异常也不能随意破坏消息角色或吞掉用户可见错误。

### Delivery Ledger

最终回复生成与平台确认之间存在崩溃窗口：

```text
pending（尚未发送）
   └─重启→ 原样发送

sending（请求可能已到平台）
   └─重启→ 带“可能重复”标记重发

delivered
   └─重启→ 不再发送，等待保留期清理
```

这叫诚实的 At-least-once：系统不伪造 Exactly-once，也不因为不确定就丢失回复。最重要的是重发**已生成回复**，而不是重跑整个 Agent Turn；后者可能重复发邮件、下单或修改文件。

### 推演实验

- 场景 A：回复写入 Ledger 后、平台 Send 前崩溃。结论：恢复后原样发送。
- 场景 B：Send 已发出但确认前崩溃。结论：恢复后标注可能重复。
- 场景 C：Tool 有外部副作用，Final Delivery 失败。结论：只恢复 Final Delivery，不重跑 Tool。
- 场景 D：Cron Worker 卡住但 Event Loop 正常。结论：任务 Heartbeat/Timeout 处理；不把它等同于 systemd Event Loop Watchdog。

## 9. 分层安全模型

Hermes 的安全不是一个“Safe Mode”开关，而是多层边界：

```text
身份层      Pairing · Allowlist · Admin/User Scope
配置层      Profile Isolation · config/secrets separation
模型能力层  Toolset · check_fn · MCP filtering
参数层      Schema validation · Path/URL normalization
行动层      Dangerous command/write approval · Checkpoint
执行层      Local/Docker/SSH/Modal/... Sandbox
网络层      Egress policy · Iron Proxy · TLS/Auth
供应链层    Skill audit · Plugin provenance · MCP trust
观测层      Logs · redaction · opt-in telemetry
恢复层      Session/Delivery state · Backup · Recover
```

各层不能互相替代：Toolset 隐藏终端不等于文件系统 Sandbox；Docker 不等于允许任意外网；Pairing 不等于管理员命令；Checkpoint 不能撤销外部 API 副作用。

审批有多种作用域：默认应审阅具体命令/写入；Hermes 也支持 Session Pattern 或显式永久 Allowlist。扩大作用域必须是用户明确选择，并写入可审查配置，不能由“修复弹窗太多”自动放宽。

非秘密行为设置放在 `config.yaml`，`.env`/Secret Source 只承载 API key、Token、Password 等秘密。这既是可维护性规则，也避免秘密机制成为不可审查的功能开关通道。

### 威胁建模实验

对“从 Webhook 接收 URL，抓取内容并把摘要发到团队群”画信任边界：

1. Webhook Token/Source 验证。
2. Payload Size 和 Schema。
3. URL Scheme、DNS、内网 IP、重定向和 Egress。
4. 页面内容作为不可信数据，不作为系统指令。
5. Toolset 最小化，不给任务无关的 Terminal/File Write。
6. 群聊 Target、管理员 Scope 和 Delivery 失败。
7. 日志脱敏、Retention 和 Incident Recovery。

验收是能给每个风险指出实际控制层，而不是写一句“使用 Sandbox”。

## 10. 从原理推导扩展决策

先回答四个问题：

1. 需要的是知识流程，还是结构化行动？
2. 终端/文件/现有命令能否完成？
3. 前置服务未配置时，模型是否应该看到它？
4. 谁长期维护：Hermes Core、用户、第三方产品还是跨 Host 标准？

| 条件 | 结论 | 原理 |
|---|---|---|
| 只是可复用操作方法 | Skill | 渐进加载，不增加 Tool Schema |
| 管理本地配置/状态 | CLI + Skill | 命令可测试，模型通过终端调用 |
| 必须结构化且依赖服务 | `check_fn` Tool | 未配置时零 Schema |
| 用户/小众/第三方特有 | 独立 Plugin | 核心外维护和卸载 |
| 可复用给多个 Agent Host | MCP Server | 标准动态发现 |
| 每个用户都需要且无替代 | Core Tool | 才值得每轮永久成本 |

三个推演：

- 配置型订阅管理：CLI + Skill，不新增核心 Tool。
- Home Assistant 结构化设备动作：服务门控 Tool，凭据不存在时不可见。
- 厂商 SaaS Dashboard：独立 Plugin Repo 或 MCP，不进入核心 `plugins/` 树。

任何扩展的完成条件都包括：真实消费者、服务缺失路径、卸载恢复、错误边界、临时 Hermes Home E2E、Prompt/Tool Surface 影响说明。

## 原理自测

1. **系统 Prompt 为什么不能每轮重建？** 远端缓存依赖稳定前缀；重建会使长会话重复支付输入成本，并可能改变行为基线。
2. **Skill 为什么不永久塞进系统 Prompt？** 大多数任务不需要它；按当前用户轮加载既节省 Token，也保持系统前缀稳定。
3. **工具为什么不是越多越好？** Schema 每轮发送，增加成本、延迟、选择混淆和攻击面。
4. **`check_fn` 解决什么问题？** 在服务/凭据/二进制缺失时把工具从 Definitions 完全移除，做到未配置零 Schema；它不替代权限。
5. **Session、Memory、Context File 有何区别？** Session 是对话事实，Memory 是跨会话稳定事实，Context File 是项目/身份规则。
6. **为什么 Tool Handler Import 成功不等于工具可用？** 还要经过自动发现、Toolset、`check_fn` 和当前平台过滤。
7. **为什么压缩可能保持 Session ID 不变？** 默认 In-place 模式更新活跃视图并软归档旧消息；消费者应观察 Compression Event。
8. **压缩为什么不能替代审计日志？** 摘要有损；真实消息、事件和 Trajectory 才承担取证。
9. **Dashboard 与 Desktop 为什么不是同一前端？** Dashboard 主聊天是 PTY 内的 TUI；Desktop 是独立 React/JSON-RPC 客户端。
10. **Cron 为什么不继承当前聊天上下文？** 到期任务在新 Session 运行；只有显式 Prompt、Skills 和配置可复现。
11. **At-least-once 为什么可能产生标记后的重复？** Send 已发出但确认前崩溃时平台状态不可判定；标记重发比静默丢失诚实。
12. **为什么 Delivery 失败不能重跑 Agent？** Tool 可能有外部副作用；应恢复已生成 Final Reply。
13. **第三方 SaaS 为什么不进入核心树？** 它把厂商维护负担耦合给快速演进的核心，应独立 Plugin/MCP 发布。
14. **什么时候 E2E 比 Mock 更重要？** 配置传播、权限、Provider/Plugin Loader、文件/网络、Session/Delivery 等真实边界。
15. **为什么 Profile Clone 不做动态继承？** Profile 是隔离岛；动态耦合会让配置和凭据变化跨边界传播。
16. **Toolset 是权限系统吗？** 不是；它控制模型可见能力，真实权限还需要身份、参数、审批、执行和网络层。
17. **辅助压缩模型决定主模型压缩阈值吗？** 不决定；阈值来自主模型上下文窗口，辅助模型只影响摘要能否生成。
18. **好测试为什么断言关系而非数量？** Role/Call 配对、Gate 可见性等是行为契约；模型/工具数量是易变实现快照。

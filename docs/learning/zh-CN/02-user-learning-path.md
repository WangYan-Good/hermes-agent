# 最终用户与运维学习路线

> 目标不是“看过所有页面”，而是从安全使用逐步达到可自动化、可运营、可恢复。返回[学习中心](README.md)，查能力时使用[功能清单](01-feature-inventory.md)。

## 路线总览

| 等级 | 核心问题 | 完成后的能力 | 建议投入 |
|---|---|---|---|
| L0 | Hermes 如何安全启动？ | 能配置 Provider、完成对话、区分配置与秘密。 | 1–2 小时 |
| L1 | 如何让 Agent 理解项目并可靠行动？ | 能管理上下文、工具审批、会话、记忆、Skills/MCP 和回滚。 | 半天 |
| L2 | 如何让任务自动运行并跨端交付？ | 能使用 Cron、Gateway、语音/媒体、Dashboard 和 Desktop。 | 1 天 |
| L3 | 如何运营多身份、多模型和远程入口？ | 能管理 Profiles、路由、凭据、权限、出口、日志和备份。 | 1–2 天 |
| L4 | 如何让长期服务可观测、可恢复？ | 能做部署检查、故障注入、恢复和复盘。 | 持续实践 |

学习方式：每级先理解边界，再完成实验；只有达到“验收标准”才进入下一级。命令参数以[CLI 参考](../../../website/docs/reference/cli-commands.md)和[斜杠命令参考](../../../website/docs/reference/slash-commands.md)为准。

## L0：安全启动并完成第一次对话

### 先修知识

- 会使用终端执行命令，知道当前工作目录的含义。
- 已准备一个合法模型 Provider 账户、OAuth 或 API key。
- 知道凭据不能粘贴到聊天、日志、Git 或教程截图中。

### 学习目标

- 完成安装、Provider 配置和首次对话。
- 找到当前 Profile 的 `config.yaml`、`.env` 和日志目录。
- 解释为什么行为配置属于 `config.yaml`，API key/Token/Password 才属于秘密存储。

### 核心原理

Hermes 启动后，交互面把用户输入交给同一个 `AIAgent`。Provider 配置决定模型请求发往哪里；Toolset 决定模型能看到哪些行动能力；Profile 决定配置、凭据和会话保存在哪个 Hermes Home。

第一次学习不要同时开启 Gateway、Cron 和大量插件。先建立一个可工作的最小链路：

```text
终端输入 → Hermes CLI → AIAgent → Model Provider → 文本回复
```

行为设置与秘密分开是运维基础：`config.yaml` 可以审查和迁移，秘密文件需要更严格权限，并且不应出现在文档或会话中。

### 推荐阅读

1. [Installation](../../../website/docs/getting-started/installation.md)
2. [Quickstart](../../../website/docs/getting-started/quickstart.md)
3. [Configuration](../../../website/docs/user-guide/configuration.md)
4. [CLI Usage](../../../website/docs/user-guide/cli.md)
5. [Security](../../../website/docs/user-guide/security.md)

### 动手实验

1. 检查版本和安装：

   ```bash
   hermes version
   hermes doctor
   ```

2. 使用向导配置一个 Provider；如果使用 Nous Portal，可用 `hermes setup --portal`，否则运行 `hermes setup` 并选择相应 Provider。
3. 找到配置位置但不要打印秘密内容：

   ```bash
   hermes config path
   hermes config env-path
   hermes config check
   ```

4. 运行 `hermes`，发送三个问题：普通知识问题、要求说明当前工作目录的问题、要求列出当前可用工具类别的问题。
5. 运行 `hermes status` 和 `hermes tools --summary`，记录当前 Profile、Provider、Model 和已启用 Toolsets。

### 验收标准

- 能展示一次成功对话和一次 `hermes status` 输出，且截图没有 API key、Token 或会话隐私。
- 能准确指出 `config.yaml`、秘密文件和日志目录的职责差异。
- 能解释“模型已配置”不代表 Web、Browser、TTS、Image 等工具 Provider 已配置。
- `hermes doctor` 没有阻断基本对话的错误；非关键可选依赖缺失已被识别而非盲目安装。

### 常见误区

- 把超时、阈值、显示偏好等非秘密设置放进 `.env`。
- 一开始就启用所有 Toolsets，增加上下文成本和误调用面。
- 把终端当前目录误认为 Profile 的 Hermes Home。
- 在 Issue、聊天或截图中暴露完整诊断日志和凭据。

### 下一步

保留当前最小配置，进入 L1。先学习 Context、Session、Memory 的边界，再让 Agent 写文件或长期记忆。

## L1：让 Hermes 理解项目并可靠使用工具

### 先修知识

- 已完成 L0，并能稳定启动 CLI 或 TUI。
- 准备一个可丢弃的练习目录，目录内初始化 Git 更便于观察变化。
- 知道危险命令和文件写入需要审批，不以 YOLO 作为教程默认设置。

### 学习目标

- 用 Context Files 和 `@` 引用给 Agent 提供正确范围的信息。
- 使用文件、终端、Web 等工具，同时保留审批和 Checkpoint。
- 区分 Session、Memory、Context Files、Skill 和 MCP。
- 恢复过去 Session，并撤销一次错误文件修改。

### 核心原理

五种“让 Agent 知道更多”的方式用途不同：

| 机制 | 生命周期 | 适合内容 |
|---|---|---|
| 当前消息/`@` 引用 | 一轮 | 本次要分析的文件、Diff、URL |
| Context File | 项目/目录 | 构建命令、编码规范、安全边界 |
| Session | 一段对话 | 任务推理、工具结果和决策历史 |
| Memory | 跨 Session | 稳定偏好、长期项目事实 |
| Skill | 按需任务 | 可复用流程、脚本和模板 |

MCP 不是知识文件，而是连接外部工具服务的协议。Checkpoint 保存文件系统恢复点，不会把聊天历史一起回退。

### 推荐阅读

1. [Context Files](../../../website/docs/user-guide/features/context-files.md)
2. [Context References](../../../website/docs/user-guide/features/context-references.md)
3. [Tools](../../../website/docs/user-guide/features/tools.md)
4. [Sessions](../../../website/docs/user-guide/sessions.md)
5. [Memory](../../../website/docs/user-guide/features/memory.md)
6. [Skills](../../../website/docs/user-guide/features/skills.md)
7. [MCP](../../../website/docs/user-guide/features/mcp.md)
8. [Checkpoints](../../../website/docs/user-guide/checkpoints-and-rollback.md)

### 动手实验

1. 在练习目录创建 `AGENTS.md`，写入项目目标、允许使用的验证命令和禁止修改的目录；重新进入 Hermes，让它复述约束来源。
2. 使用 `@` 引用一个文件和当前 Git Diff，要求 Agent 只做解释，不修改文件；观察引用只属于当前轮。
3. 要求 Agent 对一个练习文件做小修改，阅读审批中的准确目标和 Diff 后批准；随后用 `git diff` 验证。
4. 使用 `/rollback` 列出 Checkpoint 并恢复该修改，确认文件回退而对话仍存在。
5. 保存一条真正跨会话稳定的偏好到 Memory；使用 `/new` 后询问该偏好。不要把临时任务状态写入 Memory。
6. 使用 `hermes sessions list` 或 `/sessions` 找到旧 Session 并恢复。
7. 运行 `hermes skills list`，选择一个已安装 Skill，用斜杠命令调用；观察它只在本任务按需加载。
8. 运行 `hermes mcp list`。若已有测试 Server，使用 `hermes mcp test <name>`；若没有，只阅读配置参考，不为完成练习随意连接未知远端。

### 验收标准

- 能根据生命周期为一条信息选择消息、Context File、Session、Memory 或 Skill。
- 能在不关闭审批的情况下完成一次文件修改并成功回滚。
- 能恢复一个旧 Session，并说明恢复 Session 不会自动切换 Profile。
- 能解释 Skill 提供流程知识、MCP 提供远程结构化能力，两者都不等于新增核心工具。
- 能证明长期偏好在新 Session 中可用，同时临时任务细节没有污染 Memory。

### 常见误区

- 把整个仓库或巨型日志一次性注入上下文。
- 把每次工具输出都保存为 Memory。
- 认为 `/rollback` 会撤回外部 API 调用、已发送消息或数据库副作用。
- 为一个 Shell 可以完成的动作安装高权限 MCP。
- 把 Skill 内容永久塞进系统 Prompt，破坏渐进加载和缓存。

### 下一步

选择一个重复任务，把稳定步骤沉淀为 Skill；然后进入 L2，用 Cron 或 Gateway 在独立 Session 中运行它。

## L2：建立自动化与多端工作流

### 先修知识

- 已完成 L1，能清楚列出任务所需 Context、Skill、Tool Provider 和投递目标。
- 准备一个测试消息平台 Bot，或只使用本地 Cron/CLI 做前半实验。
- 了解时区、重试和外部 API 费用的基本影响。

### 学习目标

- 创建一项可验证、可暂停的 Cron Job。
- 配置一个消息平台并理解每聊天/线程 Session。
- 理解 Gateway 的媒体、流式、Silence 和 Delivery Ledger 语义。
- 选择 TUI、Dashboard 或 Desktop，而不是重复配置三套 Agent。

### 核心原理

Cron 到期时在**新 Session**运行，不能依赖创建任务时聊天里的隐式信息；Prompt、Skills、目标和时区必须显式保存。Gateway 把平台事件归一化后再进入同一个 Agent Core，平台差异主要出现在权限、线程、媒体和投递层。

客户端边界：

- Ink TUI 是 Node/Ink + Python JSON-RPC 的终端应用。
- Dashboard 的主聊天用 xterm.js/PTY 嵌入真实 `hermes --tui`。
- Electron Desktop 是独立 React 聊天面，通过 WebSocket JSON-RPC 连接 `hermes serve`。

### 推荐阅读

1. [Cron](../../../website/docs/user-guide/features/cron.md)
2. [Automate with Cron](../../../website/docs/guides/automate-with-cron.md)
3. [Messaging Gateway](../../../website/docs/user-guide/messaging/index.md)
4. [Voice Mode](../../../website/docs/user-guide/features/voice-mode.md)
5. [TUI](../../../website/docs/user-guide/tui.md)
6. [Web Dashboard](../../../website/docs/user-guide/features/web-dashboard.md)
7. [Desktop](../../../website/docs/user-guide/desktop.md)

### 动手实验

1. 选择一个无副作用任务，例如“每 15 分钟生成当前时间与一句固定文本”。用 `hermes cron create` 的交互式流程创建，明确时区、Prompt 和本地投递目标。
2. 运行 `hermes cron list`，随后 `hermes cron run <job-id>`；检查 Runs 记录和结果。暂停、恢复后再删除测试 Job。
3. 运行 `hermes gateway setup` 配置一个测试平台；用 `hermes gateway status` 检查服务。
4. 从私聊和一个 Thread/频道分别发消息，运行 `/status`，确认 Session Origin 不同。
5. 发送一张测试图片或语音消息，观察平台是否支持相应能力；对照[平台矩阵](../../../website/docs/user-guide/messaging/index.md#platform-comparison)，不要把不支持误判为 Agent 故障。
6. 分别启动 TUI 和 Dashboard，确认 Dashboard 主聊天继承 TUI 的 Slash 行为；如果有 Desktop，确认其 Slash Palette 和 Pane 是独立 UI。
7. 在测试 Gateway 重启后继续同一聊天，确认 Session 可恢复；不要在此阶段故意制造发送中崩溃。

### 验收标准

- Cron Job 的时区、Prompt、附加 Skills 和目标清晰可见；立即运行产生预期结果。
- 暂停 Job 后不会运行，恢复后可再次手动触发。
- 能从两个不同 Origin 展示不同 Session ID，并说明管理员跨 Origin 权限。
- 能解释 Dashboard 与 Desktop 的主聊天为什么不是同一个前端实现。
- 能根据平台矩阵说明媒体/Streaming 降级，而不是承诺每个平台完全一致。

### 常见误区

- Cron Prompt 写“根据我们刚才讨论的内容”，却未把内容或 Skill 显式附加。
- 忽略服务器时区和夏令时。
- 用 Dashboard React 重新实现 TUI Transcript/Composer。
- 把 Gateway 网络断线等同于 systemd Event Loop Watchdog 失败。
- 在群聊中开放管理员命令或不设 Pairing/Allowlist。

### 下一步

把测试自动化和 Bot 移到独立 Profile，进入 L3 配置路由、Fallback、权限、出口、日志和备份。

## L3：运营多模型、多身份和消息服务

### 先修知识

- 已完成 L2，并保留一个可重复运行的 Job 或 Gateway 测试会话。
- 知道 Profile、Project、Session 的区别。
- 能阅读 YAML，能安全备份配置和秘密。

### 学习目标

- 用 Profile 隔离个人、团队或 Worker 环境。
- 配置 Model Routing、Fallback 和 Credential Pool，并理解各自失败语义。
- 保护 Gateway/Dashboard，控制网络出口和秘密来源。
- 使用日志、Doctor、Prompt Size 和 Backup 完成运行前检查。

### 核心原理

Profile 是独立岛：它拥有自己的 Hermes Home、配置、凭据、会话、Skills 和 Gateway。`--clone` 在创建时复制一个起点，之后不会从默认 Profile 动态继承。Project 是工作目录集合，Session 是对话记录，三者不可互换。

模型运行的三层选择也不同：Routing 决定优先走哪条 Route；Fallback 在可恢复调用失败后尝试下一项；Credential Pool 在同一 Provider 内管理多凭据健康和冷却。任何一层都不能用来绕过能力要求或安全错误。

### 推荐阅读

1. [Profiles](../../../website/docs/user-guide/profiles.md)
2. [Multi-profile Gateways](../../../website/docs/user-guide/multi-profile-gateways.md)
3. [Provider Routing](../../../website/docs/user-guide/features/provider-routing.md)
4. [Fallback Providers](../../../website/docs/user-guide/features/fallback-providers.md)
5. [Credential Pools](../../../website/docs/user-guide/features/credential-pools.md)
6. [Egress](../../../website/docs/user-guide/egress/index.md)
7. [Secrets](../../../website/docs/user-guide/secrets/index.md)
8. [CLI Reference](../../../website/docs/reference/cli-commands.md)

### 动手实验

1. 使用 `hermes profile create` 的交互流程创建测试 Profile，可选择从当前 Profile 克隆；修改测试 Profile 的显示设置，确认默认 Profile 不随之变化。
2. 在测试 Profile 运行 `hermes model`、`hermes fallback list` 和 `hermes auth status`，画出“Route → Fallback → Credential”关系。
3. 为 Gateway 启用 Pairing/Allowlist；用未批准测试账号联系 Bot，确认只得到受控配对流程。
4. 运行 `hermes security audit`、`hermes doctor`、`hermes prompt-size`，记录需要修复的问题和最大的 Prompt 来源。
5. 使用 `hermes logs list` 和 `hermes logs gateway --since 30m` 定位一次测试消息的运行记录。
6. 创建一次 `hermes backup`，记录归档位置、大小和包含范围；把备份按秘密级别存储，不上传公共位置。
7. 如果需要公开 Dashboard，先配置 Auth Provider；只在 loopback 上验证未配置认证的本地模式，不尝试 Insecure Bypass。
8. 阅读 Egress/Iron Proxy 配置，列出当前允许的目标。除非有明确需要，不在学习环境扩大 Allowlist。

### 验收标准

- 能证明两个 Profile 的配置和 Session 相互隔离，并解释 Clone 不是动态继承。
- 能分别说明 Routing、Fallback、Credential Pool 在何时生效。
- 未批准用户不能直接触发 Agent 或管理员命令。
- 能从日志定位一次消息或 Cron Run，并用 Session ID 串联记录。
- 已创建可读的备份清单，且备份没有被提交到仓库或公共存储。
- 公开 Dashboard 方案包含认证和 TLS/隧道边界。

### 常见误区

- 修改默认 Profile 后期待所有克隆 Profile 自动更新。
- 把 Project 当作安全隔离或凭据边界。
- 遇到认证错误仍继续 Fallback，造成账户锁定或费用不可控。
- 为了“方便”对公网绑定 Dashboard 并关闭认证。
- 开启外部 Observability 后忘记数据分类和 Opt-in。

### 下一步

选择一个接近真实运营的测试环境，进入 L4。练习受控故障、恢复和复盘，而不是等生产事故第一次验证备份。

## L4：达到可恢复、可观测的生产运行

### 先修知识

- 已完成 L3，有独立测试 Profile、可工作的 Gateway/Cron 和近期备份。
- 能在维护窗口重启测试服务，并能访问 systemd/launchd/Windows 服务管理工具。
- 已定义允许的故障注入范围，不在生产聊天或真实用户上试验。

### 学习目标

- 建立部署前检查表、健康信号、日志保留和备份周期。
- 验证 Gateway 重启、Session 延续和未完成投递的恢复语义。
- 知道 SessionDB/Checkpoint 损坏、磁盘增长和更新失败的恢复入口。
- 用事实复盘一次故障，不靠模型猜测。

### 核心原理

可靠性来自明确状态边界：SessionDB 保存对话；Delivery Ledger 保存最终回复投递状态；Cron Run 保存调度结果；日志保存运行证据；Backup/Recover 提供离线恢复路径。Gateway 使用 at-least-once 投递：发送中崩溃时平台是否收到不可判定，因此重发必须标注“可能重复”，而不是假装 exactly-once。

Watchdog 只判断事件循环是否得到调度时间，不把普通平台掉线误判为进程卡死。更新前备份、数据库恢复到新文件和 Checkpoint GC 都是为了避免修复动作破坏原证据。

### 推荐阅读

1. [Gateway Delivery Reliability](../../../website/docs/user-guide/messaging/index.md#delivery-reliability)
2. [Gateway Watchdog](../../../website/docs/user-guide/messaging/index.md#optional-linux-event-loop-watchdog)
3. [Logs/Backup/Sessions commands](../../../website/docs/reference/cli-commands.md)
4. [Session Lifecycle](../../../docs/session-lifecycle.md)
5. [Monitoring](../../../docs/observability/monitoring.md)
6. [Updating](../../../website/docs/getting-started/updating.md)

### 动手实验

1. **部署前检查：** 记录 `hermes version`、`hermes doctor`、`hermes gateway status`、`hermes sessions stats`、磁盘空间和最近备份时间。
2. **正常重启：** 在没有活动 Run 时重启测试 Gateway，确认平台重新连接、旧 Session 可继续、Cron 状态未丢失。
3. **受控中断：** 在测试 Profile 发起一个可安全重试的慢任务；只在“Agent 已完成但尚未确认投递”的受控测试设施中模拟中断。观察 Delivery Ledger 是直接重发还是带可能重复标记，不重新执行有副作用的 Prompt。
4. **日志关联：** 用消息时间、Origin、Session ID 和 Tool 名称在 Gateway/Agent Logs 中还原事件序列。
5. **恢复演练：** 复制测试 `state.db` 后，在副本/临时 Profile 上阅读并演练 `hermes sessions recover`；确认恢复目标是新数据库，不覆盖原证据。
6. **备份恢复：** 在临时 Hermes Home 导入备份，检查 Profile、Session 和 Skills 清单；不要覆盖当前运行中的 Home。
7. **更新预演：** 运行 `hermes update --check`，阅读变更与备份策略；只有在维护窗口才执行真实更新。
8. **容量检查：** 使用 `hermes sessions stats`、Checkpoint 状态和日志大小制定保留/优化策略；先 Dry Run，再执行删除。

### 验收标准

- 有一份写明 Owner、频率、阈值和恢复动作的运行检查表。
- 能从日志和数据库状态重建一次测试故障时间线。
- 能解释未开始发送、发送中和已送达三种 Ledger 状态的恢复差异。
- 能把受损数据库恢复到独立文件，并证明原文件未被覆盖。
- 能在临时 Home 验证备份内容，而不是只确认压缩包存在。
- 更新、Prune、Clear 等破坏性操作都有备份、范围和确认步骤。

### 常见误区

- 只监控进程存在，不监控 Event Loop、投递和 Cron Run。
- 把 at-least-once 描述成绝不重复。
- 在原损坏数据库上直接做不可逆修复。
- 从未验证备份可导入。
- 在没有 Session ID/时间线的情况下先改配置，破坏事故证据。

### 下一步

周期性重复故障演练；如果要二次开发或贡献核心，转到[开发者路线](03-developer-learning-path.md)和[核心原理](04-core-principles-and-labs.md)。

## 按目标跳转

| 目标 | 最短路线 |
|---|---|
| CLI 编码助手 | L0 → L1 的 Context/Tool/Checkpoint → [项目 1](05-progressive-projects.md#项目-1可回滚的个人项目助手) |
| 个人自动化 | L0 → L1 的 Skill → L2 的 Cron → [项目 2](05-progressive-projects.md#项目-2带来源的定时简报) |
| 团队 Bot | L0 → L2 Gateway → L3 Profile/Pairing/Backup → [项目 3](05-progressive-projects.md#项目-3可运营的团队消息助手) |
| 桌面工作台 | L0 → L1 Projects → L2 Desktop/Dashboard 边界 |
| 安全部署 | L0 Security → L3 Scope/Egress/Secrets → L4 Recovery Drill |

## 完成路线后的能力清单

- [ ] 能在不泄露凭据的情况下配置 Provider 和工具服务。
- [ ] 能为信息选择正确的 Context、Session、Memory 或 Skill 生命周期。
- [ ] 能在保留审批的情况下修改文件并使用 Checkpoint 回滚。
- [ ] 能创建显式、可暂停、可观察的 Cron Job。
- [ ] 能保护 Gateway 和 Dashboard 的远程入口。
- [ ] 能隔离 Profile，并区分 Profile、Project、Session。
- [ ] 能解释 Routing、Fallback 和 Credential Pool。
- [ ] 能用日志、Session ID 和 Delivery Ledger 还原故障。
- [ ] 能验证备份与数据库恢复，而不仅是创建文件。
- [ ] 能判断何时应进入开发者路线，而不是继续堆叠配置。

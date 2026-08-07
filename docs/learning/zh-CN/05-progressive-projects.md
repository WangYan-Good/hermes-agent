# 五个递进实战项目

> 每个项目都把多个功能组合成一条真实链路。不要跳过故障注入：能在失败路径解释状态，才算真正掌握。返回[学习中心](README.md)。

## 如何使用本教程

按顺序完成时，永久表面和风险逐步增加：

```text
项目 1：使用现有能力
   ↓
项目 2：组合 Skill + Cron
   ↓
项目 3：运营远程 Gateway
   ↓
项目 4：扩展 CLI + Skill
   ↓
项目 5：独立 Plugin 或 MCP
```

统一规则：

- 在练习分支、临时目录或独立测试 Profile 中操作。
- 不把 API key、Token、Password 写入教程文件、Git 或 Prompt。
- 保留危险命令和写操作审批，不用 YOLO 简化步骤。
- 先记录基线和验收，再修改；外部副作用必须可控。
- 文中的平台、模型和插件只是示例，动态清单查[功能清单](01-feature-inventory.md#10-动态目录与完整索引)。

## 项目 1：可回滚的个人项目助手

### 能力目标

在一个练习仓库中，让 Hermes 读取项目约束、分析指定文件、完成小改动、运行验证、保存适量长期记忆，并在故意错误修改后恢复工作区。

### 原理连接

- Context File 决定项目规则，`@` 引用决定本轮证据。
- Session 保存推理/工具历史，Memory 只保存跨会话稳定事实。
- File Write/Patch 经过审批，并在修改前产生 Checkpoint。
- `/rollback` 恢复文件系统，不撤销聊天或外部副作用。

对应原理：[Prompt Assembly](04-core-principles-and-labs.md#2-prompt-assembly-与缓存不变量)、[状态职责](04-core-principles-and-labs.md#6-会话记忆搜索与上下文压缩)、[安全层](04-core-principles-and-labs.md#9-分层安全模型)。

### 前置条件

- 已完成[用户路线 L0–L1](02-user-learning-path.md#l1让-hermes-理解项目并可靠使用工具)。
- Git、Python 和 Hermes CLI 可用。
- 使用无秘密、可删除的练习目录。

### 架构

```text
AGENTS.md + @file/@diff
        ↓
CLI/TUI Session → File/Terminal Tools → Approval
        ↓                            ↓
    SessionDB                  Checkpoint Store
        ↓                            ↓
   Memory（少量）               /rollback
```

### 分步实施

1. 创建练习仓库并记录基线：

   ```bash
   mkdir hermes-learning-lab
   cd hermes-learning-lab
   git init
   ```

2. 新建 `calculator.py` 与对应测试，初始实现只支持加法；运行测试并提交基线。
3. 新建 `AGENTS.md`，明确：只改 `calculator.py` 和测试、先写失败测试、运行的准确命令、禁止网络和删除操作。
4. 启动 Hermes，先要求它复述约束来源；再用 `@calculator.py` 和测试文件要求设计减法支持。
5. 要求先写测试并运行，确认失败后实现最小改动。逐次阅读 File Patch 和 Terminal Approval。
6. 运行测试、`git diff --check` 和 `git diff`；要求 Hermes 用一句话解释每处变化与需求关系。
7. 只把稳定偏好（例如“本项目新增运算必须先写测试”）写入 Memory，不保存本次具体 Diff。
8. 用 `/title hermes-learning-lab` 命名 Session，并用 `/status` 记录 Session ID 和工具摘要。

### 预期现象

- Agent 自动读取 `AGENTS.md`，当前轮 `@` 内容出现在 User Context，而不是永久系统配置。
- 写入前出现准确 Diff/目标，审批后才修改文件。
- 测试从失败变为通过，Session 中能找到 Tool Call/Result。
- `/rollback` 可列出此次修改前的 Checkpoint。

### 故障注入

要求 Hermes 在练习文件中故意加入一个会让测试失败的无害改动并批准执行；运行测试确认失败。随后执行 `/rollback` 恢复该 Checkpoint，再次运行测试。

观察三点：文件恢复、测试重新通过、当前 Session 对话仍保留。不要把故障注入扩展到外部 API、真实数据或删除命令。

### 验收清单

- [ ] Agent 能指出规则来自哪个 Context File。
- [ ] 修改范围没有越过 `AGENTS.md` 约束。
- [ ] 测试经历可见的 Red → Green。
- [ ] 所有写入保留审批，没有启用 YOLO。
- [ ] Memory 只包含稳定偏好。
- [ ] 错误改动通过 Checkpoint 恢复，Session 未被回退。
- [ ] `git diff --check` 无错误，最终 Diff 可解释。

### 进一步挑战

- 用 `session_search` 在新 Session 找回这次决策，但不把整段历史复制到 Prompt。
- 比较启用/禁用一个大 Toolset 的 `hermes prompt-size --json`。
- 在独立练习分支重复任务，验证 Context File 可复用。

## 项目 2：带来源的定时简报

### 能力目标

每天在固定时区搜索一个主题，提取来源、生成带引用摘要，并把结果投递到本地或测试消息渠道；任务可立即运行、暂停、恢复和审计。

### 原理连接

- Skill 固化检索、去重、引用和失败报告方法。
- Cron 在新 Session 运行，只拥有显式 Prompt、Skills 和配置。
- Web 结果是不可信数据，需要来源验证；投递失败不应重跑有副作用步骤。

对应原理：[Tool Gate](04-core-principles-and-labs.md#5-tool-registrytoolsets-与能力门控)、[Cron/Delivery](04-core-principles-and-labs.md#8-自动化并发和可靠投递)。

### 前置条件

- 已完成项目 1 和[用户路线 L2](02-user-learning-path.md#l2建立自动化与多端工作流)。
- 配置一个 Web Search/Extract Provider。
- 选择本地输出或测试消息渠道，不使用生产群。

### 架构

```text
Cron(schedule + timezone + prompt + skill)
        ↓ 新 Session
Web Search → Web Extract → 来源核验/去重
        ↓
引用化简报 → Delivery Target → Run/Ledger/Logs
```

### 分步实施

1. 明确主题，例如“Python 安全更新”，并规定只使用最近 24 小时、最多 5 个来源。
2. 创建或选择一个“带来源研究”Skill，规则包括：优先原始来源、记录发布日期与事件日期、至少两个独立来源、无法验证时明确写未知。
3. 手动调用 Skill 完成一次简报，确认 Web Tool 可见、引用可打开、没有把网页指令当成系统要求。
4. 使用 `hermes cron create` 交互式创建 Job：保存完整 Prompt、时区、Skill 和 Delivery Target。
5. 运行 `hermes cron list`，记录 Job ID、下一次时间和时区；执行 `hermes cron run <job-id>`。
6. 检查 Run 状态、生成内容、来源链接和目标渠道；用日志关联 Session ID。
7. 依次执行 Pause、确认不再到期运行、Resume、再次手动 Run。
8. 完成测试后保留 Job 或使用 `hermes cron remove <job-id>` 清理。

### 预期现象

- Cron Run 使用新 Session ID。
- 简报格式来自附加 Skill，而不是创建 Job 的旧聊天历史。
- 每个事实能追到链接和日期；没有新信息时明确报告，而不是补写旧闻。
- 暂停/恢复不删除历史 Runs。

### 故障注入

将测试 Job 的主题临时改成一个无可靠结果的唯一字符串，并立即运行。正确行为是生成“没有可验证信息”的简报或结构化失败，而不是伪造来源。

再选择一个测试 Delivery Target 暂时不可用的窗口，确认 Run/Delivery 错误可见。恢复投递时只发送已生成结果；不要让任务重复执行外部副作用。

### 验收清单

- [ ] Job 明确保存时区、Prompt、Skill 和目标。
- [ ] 手动 Run 使用新 Session，结果可在 Runs/Logs 中追踪。
- [ ] 来源包含 URL、发布日期和必要的事件日期。
- [ ] 无结果路径不幻觉内容。
- [ ] Pause/Resume 行为可观察。
- [ ] Delivery 失败与 Agent 生成失败能区分。
- [ ] 凭据没有进入 Cron Prompt 或日志截图。

### 进一步挑战

- 增加去重状态，只报告上次成功 Run 之后的新条目。
- 为简报生成 TTS，但保留文本和引用作为可审计主版本。
- 使用 Automation Blueprint 表达同一流程并比较生成配置。

## 项目 3：可运营的团队消息助手

### 能力目标

在独立 Profile 部署一个测试团队 Bot，具备 Pairing/Allowlist、线程 Session、日志、备份、正常重启和可解释的 Delivery 恢复语义。

### 原理连接

- Profile 隔离配置、凭据、Session、Skills 和 Gateway Service。
- Platform Adapter 统一入站事件，权限在 Agent 运行前检查。
- Delivery Ledger 恢复最终回复，发送中崩溃只能做到带标记的 At-least-once。

对应原理：[多入口](04-core-principles-and-labs.md#7-多入口复用同一核心)、[安全](04-core-principles-and-labs.md#9-分层安全模型)。

### 前置条件

- 已完成项目 2 和用户路线 L3。
- 一个测试平台 Bot；下文以 Telegram 为流程示例，其他平台查[完整索引](01-feature-inventory.md#完整渠道索引)。
- 有两个测试账号：管理员和未批准用户。

### 架构

```text
Team Profile (独立 HERMES_HOME)
  ├─ Provider/Tools/Skills
  ├─ Pairing + Admin Scope
  ├─ Telegram Platform Plugin
  ├─ SessionDB + Delivery Ledger
  └─ Gateway Service + Logs + Backup
```

### 分步实施

1. 通过 `hermes profile create` 创建团队测试 Profile；如果 Clone，只把它当创建时快照。
2. 在该 Profile 配置模型和最小 Toolsets，运行 `hermes doctor` 和 `hermes tools --summary`。
3. 运行 `hermes gateway setup` 选择 Telegram，凭据只写秘密存储。
4. 启动/安装 Gateway，运行 `hermes gateway status`；管理员账号发送 `/start` 和 `/whoami`。
5. 未批准账号联系 Bot，管理员用 `hermes pairing list/approve` 完成受控配对；验证普通用户不能执行管理员命令。
6. 在私聊和群 Thread 分别发消息，用 `/status` 记录不同 Session Origin/ID。
7. 查看 `hermes logs gateway --since 30m`，按 Session ID 关联入站、Agent Run、Tool 和 Delivery。
8. 创建 `hermes backup`，记录 Manifest、保存位置和恢复责任人。
9. 在无活动 Run 时正常重启 Gateway，确认 Session、Pairing 和 Cron/配置仍在。

### 预期现象

- 团队 Profile 的 Session/配置不出现在默认 Profile。
- 未批准用户无法直接消耗 Agent Turn。
- Thread/私聊 Session 隔离，模型切换按 Session 持久化。
- 正常重启不会丢失 Session 或 Pairing。

### 故障注入

先在测试环境完成一个无副作用 Prompt。使用项目已有测试设施或维护窗口模拟两种状态：Final Reply 尚未 Send；Send 已开始但未确认。第一种恢复后原样投递，第二种带“可能重复”标记。

不要通过在生产进程上随机 `kill -9` 制造测试，也不要用会发邮件、下单或写外部系统的 Prompt。目标是验证 Ledger 状态机，而不是制造真实副作用。

### 验收清单

- [ ] Profile、凭据和 Session 与个人环境隔离。
- [ ] Pairing/Allowlist 和 Admin/User 命令分权有效。
- [ ] 私聊与 Thread Session 可区分。
- [ ] 日志能用 Session ID 还原一次请求。
- [ ] 备份 Manifest 已验证且按秘密级别存储。
- [ ] 正常重启后会话可继续。
- [ ] 能解释 Pending/Sending/Delivered 的恢复差异。

### 进一步挑战

- 配置 systemd Event Loop Watchdog，并区分网络掉线与 Loop Stall。
- 增加一个只读 Cron 简报并投递到测试群 Thread。
- 在临时 Hermes Home 验证备份导入，不覆盖运行中 Profile。

## 项目 4：CLI 命令与 Skill 扩展

### 能力目标

通过一个“工作区备注”练习理解 CLI/Slash Registry、配置持久化和 Skill 按需加载：命令管理一条非秘密备注，Skill 读取备注并指导 Agent 在任务开始时检查它；不新增核心 Tool。

### 原理连接

- 管理配置/状态优先使用 CLI + Skill，不为简单文件操作新增 Schema。
- Slash Command 在中央 Registry 定义，帮助、Alias、Gateway/Telegram/Slack/Autocomplete 从同一来源派生。
- Skill 作为当前用户轮按需注入，系统 Prompt 保持稳定。

### 前置条件

- 已完成[开发者路线 L0–L3](03-developer-learning-path.md#l3选择正确的扩展点)。
- 使用练习分支和临时 Hermes Home。
- 阅读[Extending CLI](../../../website/docs/developer-guide/extending-the-cli.md)与[Creating Skills](../../../website/docs/developer-guide/creating-skills.md)。

### 架构

```text
/workspace-note set <text>
        ↓ COMMAND_REGISTRY / resolver
HermesCLI handler → save_config_value("learning.workspace_note", text)
        ↓
config.yaml
        ↑
workspace-note Skill → hermes config get learning.workspace_note
        ↓
当前 User Turn 指导（不新增 Tool Schema）
```

### 分步实施

1. 在 [`hermes_cli/commands.py`](../../../hermes_cli/commands.py) 增加 `CommandDef("workspace-note", ...)`，定义 `show/set/clear` 参数提示和一个 Alias。
2. 在 `HermesCLI.process_command()` 使用解析后的 Canonical Name 分发到小型 Handler；Handler 使用现有 `save_config_value()`，不直接拼 YAML。
3. 明确该配置是非秘密行为数据，写入 `config.yaml`；拒绝空 `set`，`clear` 删除/置空目标键，并给出用户可读反馈。
4. 为 Registry 派生行为写表驱动测试：Canonical、Alias、CLI Help、Autocomplete；若 Gateway 不应开放，明确 `cli_only`。
5. 创建用户 Skill `workspace-note`，`SKILL.md` 说明用 `hermes config get learning.workspace_note` 读取备注，在当前任务开始时复述约束；不读 `.env`。
6. 用临时 Hermes Home 运行 `set → show → clear → show`，检查配置和错误路径。
7. 启动两轮 Session，第一轮不调用 Skill，第二轮用 `/workspace-note` Skill；比较系统 Prompt Hash，应保持相同。
8. 运行命令/Skill 的目标测试和 `git diff --check`，审查是否有真实消费者和合并价值。

### 预期现象

- Alias、Help 和 Autocomplete 自动从 Registry 派生。
- 配置只写临时 Profile 的 `config.yaml`。
- 未调用 Skill 时不会加载完整说明；调用后作为当前用户轮内容出现。
- Tool Definitions 数量不变。

### 故障注入

分别输入空备注、超长备注、未知子动作和只读配置文件。Handler 应返回明确错误且不损坏 YAML；Skill 读取不到备注时应说明“未配置”，不能发明内容。

再临时删除 Skill，确认 CLI 命令仍可独立工作；这证明命令拥有状态管理，Skill 只拥有 Agent 使用方法。

### 验收清单

- [ ] 命令只有一个中央 Registry 定义。
- [ ] Alias/Help/Autocomplete 测试通过。
- [ ] 非秘密设置写 `config.yaml`，没有新 `HERMES_*` 环境变量。
- [ ] Skill 不永久修改系统 Prompt。
- [ ] 未新增核心 Tool 或重复配置 Manager。
- [ ] 临时 Home 的 Set/Show/Clear 和错误路径通过。
- [ ] 删除 Skill 后 CLI 状态管理仍工作。

### 进一步挑战

- 把实验改为独立用户 Plugin/命令包，避免提议无普遍价值的核心功能。
- 给 Skill 增加模板/脚本，但保持 `SKILL.md` 可完整读取。
- 用行为测试证明 Profile 之间备注不继承。

## 项目 5：独立 Plugin 或 MCP 服务

### 能力目标

为一个“内部服务状态查询”能力选择独立 Plugin 或 MCP Server，实现结构化输入输出、服务门控、真实加载 E2E 和卸载恢复；不把第三方产品实现提交进 Hermes 核心树。

### 原理连接

- Plugin 适合 Hermes 用户特有、需要本地 Hooks/配置集成的能力。
- MCP 适合可被多个 Agent Host 复用的结构化服务。
- 无论哪条路线，未配置时都应零/最小工具面，错误不能破坏 Agent Loop。

### 前置条件

- 已完成项目 4 和开发者路线 L4。
- 一个本地 Mock Status API，返回固定 JSON；无需连接真实生产系统。
- 独立练习仓库或 `~/.hermes/plugins/` 临时目录，不在核心 `plugins/` 添加厂商目录。

### 架构

```text
                 ┌─ Hermes Plugin ─ manifest/config/check_fn/tool ─┐
Agent Tool Call ─┤                                                  ├→ Mock Status API
                 └─ MCP Client ─ stdio/HTTP Server/tool handshake ─┘

共同契约：service_status(service: str) -> {status, checked_at, detail}
```

### 分步实施

1. 写契约：只允许已知 Service Slug；返回 `status`、`checked_at`、`detail`；超时和 5xx 返回结构化错误，不返回秘密。
2. 用 Mock Server 固定 Success、Unknown、Timeout、Malformed 四个 Fixture。
3. **Plugin 支线：** 创建独立 Plugin Manifest，注册 `service_status` Tool；`check_fn` 检查 Base URL/Token 配置，缺失时不注册 Schema。
4. **MCP 支线：** 创建 stdio 或 HTTP MCP Server，声明同名 Tool；在 Hermes MCP Config 中只允许该 Tool，运行 `hermes mcp test <name>`。
5. 两条支线都使用 `config.yaml` 保存非秘密 Base URL/Timeout，Token 使用 Secret Source/凭据机制。
6. 在临时 Hermes Home 从真实 Plugin Loader 或 MCP Handshake 启动 Agent，获取 Tool Definitions 并调用四种 Fixture。
7. 记录 Tool Schema Token、调用日志、错误 JSON 和 Session Role/Call 配对。
8. 禁用/卸载 Plugin 或移除 MCP 配置，重新启动 Agent；`service_status` 应消失，其他 Core Tools 不变。
9. 在独立仓库写安装、权限、数据流、卸载和安全文档；第三方实现通过自己的 Release 维护。

### 预期现象

- 配置缺失时 Plugin Tool 不可见，或 MCP Server 不连接。
- Success 返回契约字段；Timeout/Malformed 变成 Tool Result 错误而非未捕获异常。
- Agent 能解释错误但不会伪造服务状态。
- 卸载后核心启动、Prompt 和 Toolsets 正常。

### 故障注入

依次关闭 Mock Server、返回无效 JSON、延迟超过 Timeout、使用未知 Service Slug。检查：

- Plugin/MCP 错误被包装成结构化结果。
- Token/Authorization 不进入日志和 Tool Result。
- Retry 有上限，不在不可恢复输入错误上循环。
- MCP 断连不阻止无关 Core Tools 加载。

### 验收清单

- [ ] 已用 Footprint Ladder 说明选择 Plugin 或 MCP 的理由。
- [ ] 有真实消费者和明确 Tool Contract。
- [ ] 非秘密配置与 Token 分离。
- [ ] 缺失配置/服务时工具面正确收缩。
- [ ] Success/Unknown/Timeout/Malformed E2E 全部通过。
- [ ] Role/Tool Call 配对合法，错误没有逃逸 Agent Loop。
- [ ] 卸载后核心恢复且无残留注册。
- [ ] 第三方实现位于独立仓库，有安装和卸载文档。

### 进一步挑战

- 同时实现 Plugin 与 MCP，比较 Hermes 特有集成成本和跨 Host 复用价值。
- 加入 OAuth，但只申请读取状态的最小 Scope。
- 增加观测指标，同时保持第三方外发为显式 Opt-in。

## 从实战继续进阶

- 偏产品使用：重复项目 2–3，逐步增加来源质量、权限和恢复要求。
- 偏二次开发：把项目 4–5 的实验结果整理成 Design Spec 和 E2E Contract。
- 偏核心贡献：从真实 Bug 复现开始，按[贡献前检查表](03-developer-learning-path.md#贡献前检查表)审查缓存、角色、安全与可靠性。
- 偏运维：把项目 3 的验收清单变成团队 Runbook，定期在测试 Profile 做恢复演练。

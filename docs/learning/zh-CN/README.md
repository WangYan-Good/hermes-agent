# Hermes Agent 功能全景与进阶学习指南

> 适用基线：`develop@1a9a9c1fe`（2026-08-08）。本文档按功能族梳理当前项目；命令、模型、插件、平台和技能等动态目录以仓库中的参考页与注册表为准。

## 这套文档解决什么问题

Hermes 不是单一的聊天 CLI，而是一套复用同一智能体核心的个人 Agent 平台：它可以运行在终端、消息平台、编辑器、Web Dashboard 和 Electron Desktop 中，也能通过定时任务、子智能体、技能、插件与模型上下文协议（MCP）扩展。

项目已有大量单点文档，但第一次接触时仍容易遇到三个问题：不知道功能边界、不知道先学什么、不知道用户功能如何落到源码。本学习中心把这些问题拆成三层：

1. 用[功能清单](01-feature-inventory.md)回答“当前能做什么”。
2. 用[用户路线](02-user-learning-path.md)和[开发者路线](03-developer-learning-path.md)回答“按什么顺序学”。
3. 用[核心原理与实验](04-core-principles-and-labs.md)和[递进实战](05-progressive-projects.md)回答“为什么这样设计、如何证明自己掌握了”。

这不是官网的逐字翻译，也不把易变的工具 Schema、模型列表或技能正文复制一遍。每个功能族都尽量给出入口、前置条件、工作原理、源码证据和深入阅读位置。

## 选择你的路线

| 你是谁 | 建议起点 | 先达到的目标 | 接下来 |
|---|---|---|---|
| 首次使用者 | [用户路线 L0](02-user-learning-path.md#l0安全启动并完成第一次对话) | 安全完成安装、模型提供商（Provider）配置和第一次对话 | L1：上下文、工具、会话、记忆与技能 |
| 进阶用户 | [功能清单](01-feature-inventory.md) | 能为任务选择工具、技能、MCP、Cron 或消息渠道 | 用户路线 L2–L3 |
| 部署运维人员 | [用户路线 L3](02-user-learning-path.md#l3运营多模型多身份和消息服务) | 能管理配置档案（Profile）、消息网关（Gateway）、权限、日志、备份和恢复 | L4：可观测、可恢复运行 |
| 插件/集成开发者 | [开发者路线 L0](03-developer-learning-path.md#l0从入口追到-aiagent) | 能从任一入口追踪到 `AIAgent`，并选对扩展层级 | L2–L3：工具、Provider、Plugin 与 MCP |
| 核心贡献者 | [核心原理](04-core-principles-and-labs.md) | 能维护提示词缓存、角色交替、安全与可靠性不变量 | 开发者路线 L4 和贡献前检查表 |

如果还没有可运行的 Hermes，先阅读[安装指南](../../../website/docs/getting-started/installation.md)和[快速开始](../../../website/docs/getting-started/quickstart.md)。

## 五分钟理解 Hermes

可以用五句话建立正确心智模型：

1. **一个核心，多种入口。** 经典 CLI、Ink TUI、Gateway、ACP、API Server、批处理和桌面端最终都把任务交给 [`AIAgent`](../../../run_agent.py)。
2. **模型负责决策，工具负责行动。** 模型返回文本或工具调用；[工具注册表（Tool Registry）](../../../tools/registry.py)把结构化调用分发给具体 Handler，再把结果送回模型循环。
3. **上下文不等于记忆。** 当前会话、项目上下文文件、`MEMORY.md`/`USER.md`、外部记忆提供商和压缩摘要承担不同职责。
4. **核心保持窄，能力长在边缘。** 新能力优先复用现有代码，其次考虑 CLI + Skill、服务门控工具、Plugin 或 MCP，最后才新增核心工具。
5. **长会话的缓存不变量是架构约束。** 系统提示词在会话生命周期中保持字节稳定，消息保持合法角色顺序；动态能力通过当前轮输入、工具结果或已注册扩展进入，而不是随意重写历史前缀。

从用户角度，最常见的组合是：

```text
项目上下文 + 会话 + 工具审批
    → 完成一次可回滚任务
    → 把稳定方法沉淀成 Skill
    → 用 Cron / Gateway 自动运行和投递
    → 用 Profile、权限、日志和备份进行运营
```

从开发者角度，最重要的阅读顺序是：

```text
入口 → AIAgent → Prompt / Provider → Model response
                    ↓
              Tool Registry → Tool handler
                    ↓
              SessionDB / delivery
```

## 系统全景

```text
┌───────────────────────────────────────────────────────────────┐
│ 交互入口                                                      │
│ CLI · Ink TUI · Gateway · API · ACP · Batch · Dashboard      │
│ Electron Desktop · Python Library                             │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│ AIAgent                                                       │
│ Prompt Assembly · 智能体循环（Agent Loop）· Budget/Interrupt │
├──────────────────────┬─────────────────────┬──────────────────┤
│ Provider Runtime     │ Tool Registry       │ Context Runtime  │
│ model/API/fallback   │ schema/gate/handler │ cache/compress   │
└──────────┬───────────┴──────────┬──────────┴─────────┬────────┘
           ▼                      ▼                    ▼
  Model Provider          Built-ins / MCP      SessionDB / FTS5
  Plugins & OAuth         Plugins / Skills     Memory / Checkpoint
           │                      │                    │
           └──────────────────────┴────────────────────┘
                                  ▼
                      展示、持久化与可靠投递
```

主要边界：

- [`run_agent.py`](../../../run_agent.py)负责会话主循环，不应吸收所有外围能力。
- [`model_tools.py`](../../../model_tools.py)和[`tools/registry.py`](../../../tools/registry.py)负责工具发现与调用边界。
- [`toolsets.py`](../../../toolsets.py)负责按平台和场景选择可见工具集合。
- [`hermes_state.py`](../../../hermes_state.py)及拆分模块负责持久化会话与搜索。
- [`gateway/`](../../../gateway/)负责消息平台会话、权限、投递和定时任务整合。
- [`plugins/`](../../../plugins/)、[`skills/`](../../../skills/)和 MCP 承担边缘扩展。
- [`ui-tui/`](../../../ui-tui/)、[`web/`](../../../web/)和[`apps/desktop/`](../../../apps/desktop/)是不同交互面，不应被理解成三份 Agent 实现。

## 文档地图

| 文档 | 回答的问题 | 推荐读者 |
|---|---|---|
| [01 · 功能清单](01-feature-inventory.md) | Hermes 当前有哪些能力？入口、原理和源码在哪里？ | 所有人 |
| [02 · 用户与运维学习路线](02-user-learning-path.md) | 怎样从第一次对话进阶到可靠部署？ | 用户、管理员、运维 |
| [03 · 开发者与贡献者学习路线](03-developer-learning-path.md) | 怎样读主链路、选择扩展点并贡献代码？ | 二次开发者、贡献者 |
| [04 · 核心原理与最小实验](04-core-principles-and-labs.md) | Agent Loop、缓存、工具、持久化和安全为何这样设计？ | 进阶用户、开发者 |
| [05 · 五个递进实战](05-progressive-projects.md) | 怎样把单项能力组合成完整项目？ | 所有人 |

已有详细资料仍是单项事实来源：

- [功能总览](../../../website/docs/user-guide/features/overview.md)
- [CLI 命令参考](../../../website/docs/reference/cli-commands.md)
- [斜杠命令参考](../../../website/docs/reference/slash-commands.md)
- [内置工具参考](../../../website/docs/reference/tools-reference.md)
- [工具集参考](../../../website/docs/reference/toolsets-reference.md)
- [系统架构](../../../website/docs/developer-guide/architecture.md)
- [Agent Loop](../../../website/docs/developer-guide/agent-loop.md)

## 盘点基线与准确性

本套文档以 2026-08-08 的 `develop@1a9a9c1fe` 为代码盘点基线；设计与编写工作在其后的文档提交中完成。

完整性采用两条规则：

- 稳定部分按**功能族**完整覆盖，例如“浏览器自动化”“消息 Gateway”“外部记忆 Provider”。
- 动态部分提供**完整索引入口**，例如模型、命令、工具、平台、插件和技能目录，不冻结数量或枚举长度。

发生冲突时，准确性优先级为：注册表/入口代码 → 运行时核心代码 → 自动生成参考页 → 使用指南 → 本学习中心。若某项能力需要凭据、特定平台、可选依赖或实验开关，功能清单会明确标注。

## 如何验证自己学会了

不要用“读完页面”作为完成标准。每一级学习都应留下可观察证据：

- 用户能够展示一次成功工具调用、会话恢复、检查点回滚或定时任务投递。
- 运维人员能够从状态与日志定位故障，并从备份或恢复入口重建服务。
- 开发者能够画出调用链、指出扩展面、运行真实加载路径，并解释为何不会破坏缓存和安全不变量。
- 贡献者能够在当前 `develop` 复现问题，指出表现位置，说明原提交意图，并用行为契约而非易变快照验证修复。

每篇路线都提供验收标准；[实战教程](05-progressive-projects.md)还加入故障注入，要求读者证明系统在非理想路径下仍可理解、可恢复。

## 术语约定

| 术语 | 本文含义 |
|---|---|
| 智能体循环（Agent Loop） | 模型调用、工具执行、结果回注和最终回复构成的迭代循环。 |
| 模型提供商（Provider） | 提供模型 API、认证和模型目录的运行后端；可以是内置能力或插件。 |
| 工具注册表（Tool Registry） | 保存工具 Schema、Handler、所属 Toolset 和门控条件的中心注册结构。 |
| 工具集（Toolset） | 按场景组织的一组工具，用来控制当前平台暴露给模型的能力面。 |
| 模型上下文协议（MCP） | 连接外部工具服务器的开放协议；Hermes 支持 stdio 和 HTTP 等传输。 |
| 消息网关（Gateway） | 连接 Telegram、Discord、Slack 等渠道，处理权限、会话、媒体与投递的后台进程。 |
| 配置档案（Profile） | 拥有独立 Hermes Home、配置、凭据和会话状态的隔离运行身份。 |
| 技能（Skill） | 模型按需加载的流程知识、脚本和模板；属于程序性记忆，不等于原生工具。 |
| 插件（Plugin） | 在核心之外提供工具、Hooks、Provider、平台或其他扩展的运行时包。 |
| 端到端测试（E2E） | 从真实入口、配置和加载链路验证行为，而不是只替换依赖做 Mock。 |


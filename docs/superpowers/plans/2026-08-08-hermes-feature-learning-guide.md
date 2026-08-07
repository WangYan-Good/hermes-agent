# Hermes 功能全景与进阶学习指南 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `docs/learning/zh-CN/` 交付一套同时服务最终用户、运维人员、二次开发者和贡献者的中文功能清单、学习路线、原理教程与递进实战文档。

**Architecture:** 文档采用“一个入口 + 五篇主题文档”的分层结构。功能清单按用户可感知的功能族建立证据索引，两条学习路线共享基础后分流，原理篇按运行数据流组织，实战篇负责组合知识；动态命令、工具、插件、平台和技能目录只链接单一事实来源，不在多处复制。

**Tech Stack:** GitHub Flavored Markdown、仓库内 Python/TypeScript 源码、现有 Docusaurus 文档、Shell 只读校验命令。

## Global Constraints

- 盘点基线固定为 `develop` 分支提交 `1a9a9c1fe`；设计规格提交为 `45d11aa92`。
- 正式交付只创建 `docs/learning/zh-CN/` 下六个 Markdown 文件，不修改 Hermes 运行时代码。
- “所有功能”按用户可感知的功能族覆盖，动态目录通过完整索引和现有参考文档承接。
- 每个功能族必须包含能力、入口、前置条件、原理、源码证据、深入阅读和边界说明。
- 每个学习等级必须包含先修知识、学习目标、原理、推荐阅读、源码入口、动手实验、验收标准和常见误区。
- 非秘密配置只能指向 `~/.hermes/config.yaml`；`~/.hermes/.env` 只用于 API key、token、password 等凭据。
- 数量只作为带日期的盘点快照，不写成行为契约或测试不变量。
- 无法从当前代码或现有文档证实的能力不写入清单。
- 对实验性、插件提供、凭据门控和平台限定能力明确标注边界。
- 使用 `apply_patch` 创建和编辑文档；保留工作树中所有无关用户修改。

---

### Task 1: 建立中文学习中心入口

**Files:**
- Create: `docs/learning/zh-CN/README.md`
- Reference: `docs/superpowers/specs/2026-08-08-hermes-feature-learning-guide-design.md`
- Reference: `README.zh-CN.md`
- Reference: `website/docs/getting-started/learning-path.md`
- Reference: `website/docs/developer-guide/architecture.md`

**Interfaces:**
- Consumes: 已批准设计中的六文件结构、L0–L4 分层和功能族完整性定义。
- Produces: 五篇主题文档共用的入口、术语约定、相对链接和系统全景图。

- [ ] **Step 1: 记录任务开始前的工作树状态**

Run: `git status --short`

Expected: 只显示用户已有修改；若出现无关修改，记录并在后续提交中排除。

- [ ] **Step 2: 用最终章节创建入口文档**

使用 `apply_patch` 创建 `docs/learning/zh-CN/README.md`，正文必须直接写成可交付内容，并包含以下标题：

```markdown
# Hermes Agent 功能全景与进阶学习指南

## 这套文档解决什么问题
## 选择你的路线
## 五分钟理解 Hermes
## 系统全景
## 文档地图
## 盘点基线与准确性
## 如何验证自己学会了
## 术语约定
```

“选择你的路线”使用角色表格，至少覆盖首次使用者、进阶用户、部署运维、插件/集成开发者和核心贡献者；“系统全景”使用一个紧凑的文本或 Mermaid 数据流图，包含入口层、`AIAgent`、Provider Runtime、Tool Registry、SessionDB、Gateway、Plugins/Skills/MCP；“文档地图”链接到以下固定文件名：

```text
01-feature-inventory.md
02-user-learning-path.md
03-developer-learning-path.md
04-core-principles-and-labs.md
05-progressive-projects.md
```

同时链接现有安装、Quickstart、功能参考和开发者架构文档，仓库内链接统一从当前目录使用 `../../../website/docs/...` 或 `../../../<source-path>`。

- [ ] **Step 3: 校验入口文档结构和链接名称**

Run: `grep -nE '^#|01-feature-inventory|02-user-learning-path|03-developer-learning-path|04-core-principles-and-labs|05-progressive-projects|1a9a9c1fe' docs/learning/zh-CN/README.md`

Expected: 八个规定标题、五个主题链接和盘点基线均出现。

- [ ] **Step 4: 检查 Markdown 空白与补丁格式**

Run: `git diff --check -- docs/learning/zh-CN/README.md`

Expected: 无输出，退出码为 0。

- [ ] **Step 5: 提交入口文档**

```bash
git add docs/learning/zh-CN/README.md
git commit -m "docs: add Hermes learning guide entrypoint"
```

### Task 2: 编写功能清单的入口、核心、上下文与工具部分

**Files:**
- Create: `docs/learning/zh-CN/01-feature-inventory.md`
- Reference: `hermes_cli/main.py`
- Reference: `hermes_cli/commands.py`
- Reference: `run_agent.py`
- Reference: `model_tools.py`
- Reference: `toolsets.py`
- Reference: `tools/registry.py`
- Reference: `hermes_state.py`
- Reference: `agent/prompt_builder.py`
- Reference: `agent/context_compressor.py`
- Reference: `website/docs/reference/cli-commands.md`
- Reference: `website/docs/reference/slash-commands.md`
- Reference: `website/docs/reference/tools-reference.md`
- Reference: `website/docs/reference/toolsets-reference.md`
- Reference: `website/docs/user-guide/features/overview.md`

**Interfaces:**
- Consumes: `README.md` 的术语、盘点基线与角色分类。
- Produces: 功能清单统一表格格式，以及前四个功能域的可追溯条目；Task 3 延续相同格式补齐其余功能域。

- [ ] **Step 1: 从单一事实来源建立本批证据清单**

Run: `grep -nE '^(#|##) |^\| `?[a-zA-Z0-9_-]+`? \|' website/docs/reference/tools-reference.md website/docs/reference/toolsets-reference.md website/docs/user-guide/features/overview.md`

Expected: 能看到工具集标题、工具行和功能总览条目；只把可由这些参考页或源码入口证实的能力写入文档。

- [ ] **Step 2: 创建功能清单说明和统一表格**

使用 `apply_patch` 创建文件，包含以下开头：

```markdown
# Hermes Agent 功能清单

## 阅读说明
## 1. 交互入口与客户端
## 2. 智能体核心运行时
## 3. 上下文、记忆与持久化
## 4. 工具与执行环境
```

每节使用同一列定义：

```markdown
| 功能族 | 能力与场景 | 用户入口/前置条件 | 工作原理 | 关键源码 | 深入阅读 | 边界 |
|---|---|---|---|---|---|---|
```

“阅读说明”解释核心、内置可选、插件提供、凭据门控、平台限定和实验性六种边界标签，并声明功能数量是 2026-08-08 的快照而非契约。

- [ ] **Step 3: 填写交互入口与客户端功能域**

本节至少包含以下独立行，并为每行链接真实源码或现有文档：

```text
经典交互式 CLI
Ink TUI
Web Dashboard（嵌入真实 TUI）
Electron Desktop（独立 React 聊天面）
消息 Gateway
OpenAI 兼容 API Server
ACP 编辑器集成
Python 程序化调用
批处理与轨迹生成
无 UI 的 hermes serve 后端
```

明确 Dashboard 不重写主聊天面，而 Desktop 是独立聊天面；说明两者都复用 Python 后端但传输和会话呈现不同。

- [ ] **Step 4: 填写智能体核心运行时功能域**

本节至少包含以下独立行：

```text
AIAgent 同步对话循环
Chat Completions、Codex Responses/App Server、Anthropic API 模式
Provider 与模型解析
模型路由、辅助模型和回退链
流式文本、推理内容与工具进度
工具调用分发和结果回注
共享迭代预算与一次宽限调用
中断、重试和后台运行
目标、Todo、Deliverable Mode 和用量统计
提示词组装与平台提示
```

“工作原理”列说明消息如何在 assistant tool call、tool result 和下一次模型调用之间循环，避免只写文件职责。

- [ ] **Step 5: 填写上下文、记忆与持久化功能域**

本节至少包含以下独立行：

```text
项目上下文文件发现
@ 文件/目录/Git diff/URL 引用
SQLite 会话存储与 FTS5 搜索
会话恢复、标题、归档、清理、修复与恢复
MEMORY.md 与 USER.md 内置记忆
外部 Memory Provider
上下文压缩与 in-place/legacy 模式
提示词缓存
轨迹保存与压缩
文件系统检查点和回滚
Projects 工作区
Profiles 隔离
```

明确会话历史、长期记忆、项目上下文和压缩摘要的职责差异；不得描述为同一存储层。

- [ ] **Step 6: 填写工具与执行环境功能域**

按“工具族”而不是逐个 Schema 参数组织，至少覆盖：

```text
文件读取、写入、补丁和搜索
终端、后台进程和桌面终端读取
本地、Docker、SSH、Modal、Daytona、Singularity、Vercel Sandbox 环境
Web 搜索与网页/PDF 抽取
多后端浏览器自动化和 CDP 逃生口
视觉与视频分析
图像生成与视频生成
语音转写、TTS、Voice Mode 和 Wake Word
Computer Use
代码执行 RPC
会话搜索
任务清单、澄清和用户审批
Home Assistant、Feishu、Discord、Spotify 等门控集成工具
```

解释 Tool Registry 自动发现、toolset 选择和 `check_fn` 门控之间的关系；动态工具明细链接 `tools-reference.md` 和 `toolsets-reference.md`，不复制完整 Schema。

- [ ] **Step 7: 校验本批覆盖与路径**

Run: `grep -nE '^## [1-4]\.|AIAgent|Tool Registry|check_fn|SessionDB|FTS5|Dashboard|Desktop|ACP|代码执行|上下文压缩|提示词缓存' docs/learning/zh-CN/01-feature-inventory.md`

Expected: 四个功能域及关键原理词均有命中。

Run: `git diff --check -- docs/learning/zh-CN/01-feature-inventory.md`

Expected: 无输出，退出码为 0。

- [ ] **Step 8: 提交第一批功能清单**

```bash
git add docs/learning/zh-CN/01-feature-inventory.md
git commit -m "docs: inventory Hermes core capabilities"
```

### Task 3: 补齐自动化、渠道、扩展、配置与运维功能清单

**Files:**
- Modify: `docs/learning/zh-CN/01-feature-inventory.md`
- Reference: `cron/`
- Reference: `batch_runner.py`
- Reference: `tools/delegate_tool.py`
- Reference: `tools/kanban_tools.py`
- Reference: `gateway/run.py`
- Reference: `gateway/platforms/`
- Reference: `plugins/platforms/`
- Reference: `plugins/model-providers/`
- Reference: `plugins/memory/`
- Reference: `plugins/image_gen/`
- Reference: `plugins/video_gen/`
- Reference: `plugins/web/`
- Reference: `hermes_cli/subcommands/`
- Reference: `website/docs/user-guide/messaging/index.md`
- Reference: `website/docs/user-guide/features/plugins.md`
- Reference: `website/docs/user-guide/security.md`
- Reference: `website/docs/reference/cli-commands.md`
- Reference: `website/docs/reference/skills-catalog.md`
- Reference: `website/docs/reference/optional-skills-catalog.md`

**Interfaces:**
- Consumes: Task 2 定义的七列表格和边界标签。
- Produces: 完整的十域功能清单、动态目录索引和功能组合关系说明。

- [ ] **Step 1: 读取渠道与插件的实际清单**

Run: `find plugins -type f -name plugin.yaml -printf '%h\n' | sort`

Expected: 输出平台、模型、记忆、浏览器、Web、媒体、观测等插件目录；按类别归纳，不把目录数写成测试断言。

Run: `grep -nE '^\| (Telegram|Discord|Slack|Google Chat|WhatsApp|Signal|SMS|Email|Home Assistant|Mattermost|Matrix|DingTalk|Feishu|WeCom|Weixin|BlueBubbles|Photon|QQ|Yuanbao|Microsoft Teams|LINE|ntfy|Raft|IRC|Buzz|SimpleX)' website/docs/user-guide/messaging/index.md`

Expected: 当前消息平台及能力矩阵均能从参考文档读取。

- [ ] **Step 2: 追加自动化与协作功能域**

新增标题 `## 5. 自动化、协作与后台任务`，至少覆盖：

```text
Cron 创建、暂停、恢复、立即运行和投递
Automation Blueprints
批处理并发与结构化轨迹
同步/异步子智能体委派
Mixture of Agents
Kanban 编排、依赖、阻塞和附件
后台进程和 Background Session
Gateway Hooks、Plugin Hooks 与 Middleware
Webhook 入站、过滤、转换和出站投递
Heartbeat 和长任务活性
```

说明 Cron 使用新会话、委派使用隔离上下文、Kanban 使用任务状态和依赖边、Hooks 在生命周期边界运行。

- [ ] **Step 3: 追加消息渠道与媒体功能域**

新增标题 `## 6. 消息渠道、语音与媒体`。平台按“主流协作、移动消息、企业/中国生态、家庭/通知、开放协议与中继”分组，但必须在表格或紧随其后的完整索引中逐名列出 `website/docs/user-guide/messaging/index.md` 当前矩阵中的每个平台。

除平台外，单独覆盖：

```text
每聊天/线程会话
流式编辑、typing 和 reaction
图片、文件、语音消息与 TTS
Discord Voice Channel
配对、管理员/用户命令权限和群聊策略
可靠投递账本与重启恢复
Hermes Relay 能力协商
Open WebUI 等 OpenAI 兼容前端
```

- [ ] **Step 4: 追加扩展体系功能域**

新增标题 `## 7. 技能、插件与 MCP 扩展体系`，至少覆盖：

```text
内置技能和可选技能
Skill progressive disclosure 与斜杠调用
技能创建、安装、审计、升级、发布、快照和同步
Skill Bundles
Curator 自改进维护
通用插件和 Hooks
模型、记忆、上下文引擎、平台、浏览器、Web、图像、视频、密钥源、Dashboard Auth 插件
MCP stdio/HTTP、OAuth、工具过滤、采样和 Catalog
Desktop Plugin SDK
Plugin LLM Access
```

解释 Footprint Ladder：优先扩展现有代码，其次 CLI + Skill、门控工具、Plugin、MCP，最后才是核心工具。

- [ ] **Step 5: 追加配置、个性化与模型运行功能域**

新增标题 `## 8. 配置、身份与模型运行`，至少覆盖：

```text
config.yaml 和密钥文件职责
Setup Wizard、Tools UI 和 Config CLI
Profiles、分发、克隆、导入导出和多 Gateway
Provider 插件与模型 Catalog
路由、排序、白名单/黑名单和每 Provider Proxy
Fallback Provider 与辅助模型回退
Credential Pools 和 OAuth 登录
SOUL.md、Personality、Skins 和 Pets
Projects 与工作目录绑定
Skills Sync 和多设备配置
```

- [ ] **Step 6: 追加安全、可靠性与运维功能域**

新增标题 `## 9. 安全、可靠性与运维`，至少覆盖：

```text
危险命令和写操作审批
Gateway 配对、作用域和管理员权限
终端沙箱与远程执行隔离
网络出口策略和 Iron Proxy
Secret Source 与 Bitwarden/1Password/Command
URL 安全、路径安全和技能审计
日志、Debug Bundle、Doctor、Status 和 Prompt Size
Monitoring、Observability 和 Metrics Relay
Backup、Import、Migration 和 OpenClaw/其他 Agent 导入
Update、版本检查和回滚保护
SessionDB 修复、恢复和存储优化
Gateway 服务、Watchdog 和 Dashboard 认证
```

明确审批不能被文档示例绕过，公开 Dashboard 绑定需要认证，非秘密配置不能放进 `.env`。

- [ ] **Step 7: 追加完整动态索引和组合关系**

新增以下标题：

```markdown
## 10. 动态目录与完整索引
## 功能如何组合
## 盘点边界
```

动态索引必须链接 CLI 命令、斜杠命令、Tools、Toolsets、模型、MCP、内置技能、可选技能、消息平台和插件目录；“功能如何组合”至少给出“个人编码助手、定时简报、团队机器人、桌面工作台、扩展开发”五条能力链。

- [ ] **Step 8: 反向覆盖检查**

Run: `grep -nE '^## ([5-9]|10)\.|Cron|Kanban|Mixture of Agents|Telegram|SimpleX|Skill Bundles|Curator|MCP|Footprint Ladder|Credential Pools|Iron Proxy|Observability|可靠投递' docs/learning/zh-CN/01-feature-inventory.md`

Expected: 六个新增功能域和列出的代表能力均有命中。

Run: `git diff --check -- docs/learning/zh-CN/01-feature-inventory.md`

Expected: 无输出，退出码为 0。

- [ ] **Step 9: 提交完整功能清单**

```bash
git add docs/learning/zh-CN/01-feature-inventory.md
git commit -m "docs: complete Hermes feature inventory"
```

### Task 4: 编写最终用户与运维学习路线

**Files:**
- Create: `docs/learning/zh-CN/02-user-learning-path.md`
- Reference: `website/docs/getting-started/installation.md`
- Reference: `website/docs/getting-started/quickstart.md`
- Reference: `website/docs/user-guide/cli.md`
- Reference: `website/docs/user-guide/tui.md`
- Reference: `website/docs/user-guide/desktop.md`
- Reference: `website/docs/user-guide/configuration.md`
- Reference: `website/docs/user-guide/sessions.md`
- Reference: `website/docs/user-guide/features/`
- Reference: `website/docs/user-guide/messaging/`
- Reference: `website/docs/guides/`

**Interfaces:**
- Consumes: Task 3 的功能域名称和 `README.md` 的角色导航。
- Produces: L0–L4 用户/运维路线；Task 7 的项目会链接这些阶段作为先修知识。

- [ ] **Step 1: 创建路线总览和统一阶段模板**

使用 `apply_patch` 创建文件，包含：

```markdown
# 最终用户与运维学习路线

## 路线总览
## L0：安全启动并完成第一次对话
## L1：让 Hermes 理解项目并可靠使用工具
## L2：建立自动化与多端工作流
## L3：运营多模型、多身份和消息服务
## L4：达到可恢复、可观测的生产运行
## 按目标跳转
## 完成路线后的能力清单
```

每个 L0–L4 阶段固定使用“先修知识、学习目标、核心原理、推荐阅读、动手实验、验收标准、常见误区、下一步”八个小节。

- [ ] **Step 2: 编写 L0 与 L1**

L0 实验必须覆盖安装核对、`hermes setup`、`hermes status`、第一次对话、配置路径和不泄露密钥；L1 实验必须覆盖上下文文件、`@` 引用、工具审批、会话恢复、记忆、技能、MCP 和检查点回滚。

验收标准必须是可观察行为，例如“能解释为什么长期偏好不应只留在会话历史”“能在不关闭审批的情况下完成一次文件修改并回滚”，不能写“理解相关功能”。

- [ ] **Step 3: 编写 L2 与 L3**

L2 覆盖 Cron、后台任务、Gateway、语音/媒体、Dashboard 和 Desktop；L3 覆盖 Profiles、Provider/Model、Fallback、Credential Pools、权限、配对、网络出口、日志和备份。

在 L2 明确 Dashboard 嵌入 TUI 而 Desktop 使用独立聊天面；在 L3 明确 Profiles 是隔离岛，克隆是创建时复制而非动态继承。

- [ ] **Step 4: 编写 L4、目标跳转和能力清单**

L4 使用“部署前检查 → 故障注入 → 恢复 → 复盘”的实验，覆盖 Gateway 服务状态、日志筛选、Watchdog、SessionDB 恢复入口、备份与升级；“按目标跳转”至少给出 CLI 编码、个人自动化、团队 Bot、桌面使用和安全部署五条短路线。

- [ ] **Step 5: 校验学习阶段的教学闭环**

Run: `grep -c '^### \(先修知识\|学习目标\|核心原理\|推荐阅读\|动手实验\|验收标准\|常见误区\|下一步\)' docs/learning/zh-CN/02-user-learning-path.md`

Expected: 输出 `40`，代表 5 个阶段各有 8 个教学小节。

Run: `git diff --check -- docs/learning/zh-CN/02-user-learning-path.md`

Expected: 无输出，退出码为 0。

- [ ] **Step 6: 提交用户路线**

```bash
git add docs/learning/zh-CN/02-user-learning-path.md
git commit -m "docs: add progressive Hermes user learning path"
```

### Task 5: 编写二次开发者与贡献者学习路线

**Files:**
- Create: `docs/learning/zh-CN/03-developer-learning-path.md`
- Reference: `AGENTS.md`
- Reference: `run_agent.py`
- Reference: `model_tools.py`
- Reference: `toolsets.py`
- Reference: `tools/registry.py`
- Reference: `hermes_state.py`
- Reference: `gateway/run.py`
- Reference: `tui_gateway/server.py`
- Reference: `apps/desktop/AGENTS.md`
- Reference: `website/docs/developer-guide/architecture.md`
- Reference: `website/docs/developer-guide/agent-loop.md`
- Reference: `website/docs/developer-guide/tools-runtime.md`
- Reference: `website/docs/developer-guide/prompt-assembly.md`
- Reference: `website/docs/developer-guide/context-compression-and-caching.md`
- Reference: `website/docs/developer-guide/plugins/index.md`

**Interfaces:**
- Consumes: 功能清单中的源码入口和 Footprint Ladder。
- Produces: L0–L4 源码学习与扩展路线；Task 6 原理篇负责深入解释这里引用的不变量。

- [ ] **Step 1: 创建开发路线框架**

使用 `apply_patch` 创建文件，包含：

```markdown
# 二次开发者与贡献者学习路线

## 路线总览
## 开发环境与阅读方法
## L0：从入口追到 AIAgent
## L1：运行最小程序化调用
## L2：掌握提示词、模型、工具和会话主链路
## L3：选择正确的扩展点
## L4：维护缓存、安全与可靠性不变量
## 贡献前检查表
```

每个 L0–L4 阶段同样使用“先修知识、学习目标、核心原理、源码阅读顺序、动手实验、验收标准、常见误区、下一步”八个小节。

- [ ] **Step 2: 编写 L0 与 L1**

L0 以 CLI、Gateway、TUI Gateway、API/ACP 入口追踪到 `AIAgent`，要求读者画出调用图；L1 使用 `AIAgent.chat()` 和 `run_conversation()` 的最小示例，解释 Provider、Model、API Mode、Toolsets、Session ID 和 `HERMES_HOME` 的作用。

实验必须使用临时 `HERMES_HOME`，避免污染真实用户状态，并明确示例需要读者自行提供合法 Provider 凭据。

- [ ] **Step 3: 编写 L2**

按以下源码顺序组织：

```text
agent/prompt_builder.py
hermes_cli/runtime_provider.py
run_agent.py
model_tools.py
tools/registry.py
toolsets.py
hermes_state.py 与 hermes_state_* 模块
agent/context_compressor.py
```

实验要求读者跟踪一次“用户消息 → 模型工具调用 → 工具结果 → 最终回复”，并检查数据库中的会话记录；说明真实集成路径优先于只测 Mock。

- [ ] **Step 4: 编写 L3**

使用 Footprint Ladder 作为决策树，分别讲解：扩展现有代码、CLI + Skill、`check_fn` 门控工具、独立 Plugin、MCP Server 和核心工具。补充模型 Provider、Memory Provider、Context Engine、Gateway Platform、Desktop Plugin SDK 的入口与最小验证路径。

实验给出三个需求，让读者判断扩展层级：配置型订阅管理应选 CLI + Skill；仅在凭据存在时出现的结构化智能家居动作应选门控工具；第三方 SaaS 应选独立插件仓库或 MCP。

- [ ] **Step 5: 编写 L4 与贡献前检查表**

L4 必须覆盖：系统提示词字节稳定、严格角色交替、上下文压缩边界、工具 Schema 面积、审批/沙箱/出口安全、会话与投递可靠性、并发与中断、E2E 测试和贡献者署名。

贡献前检查表采用可勾选项，并包含“能指出 Bug 在当前 `develop` 的具体表现行”“读过相关符号的 `git log -p -S` 意图”“没有新增非秘密 `HERMES_*` 配置”“没有把第三方产品耦合进核心树”。

- [ ] **Step 6: 校验教学闭环与源码链接**

Run: `grep -c '^### \(先修知识\|学习目标\|核心原理\|源码阅读顺序\|动手实验\|验收标准\|常见误区\|下一步\)' docs/learning/zh-CN/03-developer-learning-path.md`

Expected: 输出 `40`。

Run: `grep -nE 'Footprint Ladder|prompt_builder.py|runtime_provider.py|tools/registry.py|hermes_state.py|context_compressor.py|角色交替|E2E' docs/learning/zh-CN/03-developer-learning-path.md`

Expected: 每个关键架构入口和不变量至少命中一次。

- [ ] **Step 7: 提交开发者路线**

```bash
git add docs/learning/zh-CN/03-developer-learning-path.md
git commit -m "docs: add Hermes developer learning path"
```

### Task 6: 编写核心原理与最小实验教程

**Files:**
- Create: `docs/learning/zh-CN/04-core-principles-and-labs.md`
- Reference: `run_agent.py`
- Reference: `agent/prompt_builder.py`
- Reference: `hermes_cli/runtime_provider.py`
- Reference: `agent/context_compressor.py`
- Reference: `model_tools.py`
- Reference: `tools/registry.py`
- Reference: `toolsets.py`
- Reference: `hermes_state.py`
- Reference: `gateway/session.py`
- Reference: `gateway/run.py`
- Reference: `tui_gateway/server.py`
- Reference: `hermes_cli/pty_bridge.py`
- Reference: `apps/shared/`
- Reference: `website/docs/developer-guide/`

**Interfaces:**
- Consumes: Task 5 的源码阅读顺序和不变量列表。
- Produces: 可被两条学习路线和五个项目链接的原理解释与验证实验。

- [ ] **Step 1: 创建按数据流组织的原理框架**

使用 `apply_patch` 创建文件，包含：

```markdown
# Hermes 核心原理与最小实验

## 1. 一次请求的完整旅程
## 2. Prompt Assembly 与缓存不变量
## 3. 消息角色交替与工具调用协议
## 4. Provider Runtime 与 API 模式
## 5. Tool Registry、Toolsets 与能力门控
## 6. 会话、记忆、搜索与上下文压缩
## 7. 多入口复用同一核心
## 8. 自动化、并发和可靠投递
## 9. 分层安全模型
## 10. 从原理推导扩展决策
## 原理自测
```

第 1 节包含从客户端/适配器到持久化/投递的完整文本数据流图，后续每节说明自己处于链路的哪一段。

- [ ] **Step 2: 编写 Prompt、角色与 Provider 原理**

第 2–4 节必须解释：系统提示词为何在会话中保持字节稳定；Skill 为何作为用户消息按需注入；为何不能在循环中插入合成 user 消息；assistant tool call 与 tool result 如何维持协议；不同 API 模式如何被适配到统一循环；主模型、辅助模型、回退模型和 Provider 路由的职责差异。

提供“比较连续两轮系统提示词哈希”和“打印一轮工具调用的角色序列”两个最小观察实验，实验只读取临时会话状态。

- [ ] **Step 3: 编写工具与 Footprint 原理**

第 5 节解释：模块级 `registry.register()`、自动发现、Schema、Handler、`check_fn`、Toolset、平台默认值和 MCP 动态注册的关系；用一个决策表推导何时使用 CLI + Skill、门控工具、Plugin 或 MCP。

最小实验要求读者比较未配置/已配置某门控服务时的可见工具 Schema，验收结果是工具只在前置条件满足时出现，而不是仅验证 Python 函数可导入。

- [ ] **Step 4: 编写持久化与压缩原理**

第 6 节用职责表区分 SessionDB、FTS5、`MEMORY.md`、`USER.md`、外部 Memory Provider、Context Files、压缩摘要和轨迹；解释压缩四阶段、受保护首段、尾部 token budget、in-place 与 legacy rotation，以及缓存和会话 ID 的关系。

最小实验要求使用临时 `HERMES_HOME` 创建会话、搜索消息、归档并确认搜索边界，再对照压缩前后的消息角色序列。

- [ ] **Step 5: 编写多入口、异步可靠性与安全原理**

第 7–9 节必须解释：经典 CLI/TUI/Gateway/ACP/API/Batch 如何汇入 `AIAgent`；Dashboard 的 PTY 字节流与 Desktop 的 JSON-RPC/React 面如何不同；Cron 新会话、委派隔离、Kanban 状态机、Hook 生命周期和 Delivery Ledger 的失败模型；审批、配对、作用域、远程执行环境、网络出口和 Secret Source 的分层边界。

提供一次“发送开始前崩溃/发送中崩溃”的投递语义推演，以及一次“功能需要凭据、结构化参数、第三方维护”条件下的扩展层级推演。

- [ ] **Step 6: 编写原理自测**

自测至少包含 15 道带答案的问题，其中必须包括：

```text
系统提示词为何不能中途重建？
Skill 为什么不直接永久塞进系统提示词？
工具为何不是越多越好？
check_fn 解决什么成本问题？
会话、记忆和上下文文件有何区别？
Dashboard 与 Desktop 为什么不是同一个前端？
Cron 与当前聊天上下文为何隔离？
At-least-once 投递为什么可能产生标注后的重复？
第三方 SaaS 为什么不进入核心树？
什么时候 E2E 比 Mock 更重要？
```

- [ ] **Step 7: 校验原理覆盖和格式**

Run: `grep -nE '^## ([1-9]|10)\.|字节稳定|角色交替|check_fn|in-place|PTY|JSON-RPC|Delivery Ledger|At-least-once|Footprint' docs/learning/zh-CN/04-core-principles-and-labs.md`

Expected: 十个原理章节及所有关键不变量都有命中。

Run: `git diff --check -- docs/learning/zh-CN/04-core-principles-and-labs.md`

Expected: 无输出，退出码为 0。

- [ ] **Step 8: 提交原理教程**

```bash
git add docs/learning/zh-CN/04-core-principles-and-labs.md
git commit -m "docs: explain Hermes core principles with labs"
```

### Task 7: 编写五个递进实战项目

**Files:**
- Create: `docs/learning/zh-CN/05-progressive-projects.md`
- Reference: `docs/learning/zh-CN/02-user-learning-path.md`
- Reference: `docs/learning/zh-CN/03-developer-learning-path.md`
- Reference: `docs/learning/zh-CN/04-core-principles-and-labs.md`
- Reference: `website/docs/guides/daily-briefing-bot.md`
- Reference: `website/docs/guides/team-telegram-assistant.md`
- Reference: `website/docs/guides/use-mcp-with-hermes.md`
- Reference: `website/docs/developer-guide/creating-skills.md`
- Reference: `website/docs/developer-guide/plugins/index.md`

**Interfaces:**
- Consumes: 用户路线、开发者路线和原理篇中的概念、命令与安全约束。
- Produces: 五个可验收项目和从项目返回相应学习阶段的链接。

- [ ] **Step 1: 创建统一项目模板**

使用 `apply_patch` 创建文件，包含：

```markdown
# 五个递进实战项目

## 如何使用本教程
## 项目 1：可回滚的个人项目助手
## 项目 2：带来源的定时简报
## 项目 3：可运营的团队消息助手
## 项目 4：CLI 命令与 Skill 扩展
## 项目 5：独立 Plugin 或 MCP 服务
## 从实战继续进阶
```

每个项目固定使用“能力目标、原理连接、前置条件、架构、分步实施、预期现象、故障注入、验收清单、进一步挑战”九个小节。

- [ ] **Step 2: 编写项目 1 和项目 2**

项目 1 组合 Context Files、`@` 引用、Session、Memory、审批和 Checkpoint，故障注入为一次错误修改后执行回滚。项目 2 组合 Web、来源核验、Skill、Cron 和目标渠道投递，明确 Cron 在新会话运行且技能需要显式附加。

所有写文件示例都保留审批；所有定时表达式示例同时给出时区核对步骤。

- [ ] **Step 3: 编写项目 3**

项目 3 选择一个消息平台作为示例，但把平台差异链接到完整矩阵。实施步骤覆盖独立 Profile、Gateway Setup、Pairing/Allowlist、线程会话、日志、备份和重启恢复；故障注入为 Gateway 重启并核对会话及 Delivery Ledger 行为。

- [ ] **Step 4: 编写项目 4**

项目 4 实现一个管理型 CLI 命令和对应 Skill 的设计练习，不新增核心工具。教程说明 Slash Command Registry、CLI Handler、`config.yaml` 持久化、Skill 的 `SKILL.md` 与测试入口；验收包括命令帮助、别名解析、Skill 按需加载和系统提示词不变。

- [ ] **Step 5: 编写项目 5**

项目 5 先用决策表在独立 Plugin 与 MCP Server 之间选择，再给出两条实现支线。两条支线都要求：真实消费者、凭据/服务门控、结构化输入输出、加载路径 E2E、错误边界、卸载后核心恢复；明确第三方产品插件应发布到独立仓库而非 `plugins/` 核心树。

- [ ] **Step 6: 校验项目教学闭环**

Run: `grep -c '^### \(能力目标\|原理连接\|前置条件\|架构\|分步实施\|预期现象\|故障注入\|验收清单\|进一步挑战\)' docs/learning/zh-CN/05-progressive-projects.md`

Expected: 输出 `45`，代表 5 个项目各有 9 个小节。

Run: `grep -nE '回滚|Cron|时区|Pairing|Delivery Ledger|Slash Command Registry|系统提示词|MCP Server|独立仓库|E2E' docs/learning/zh-CN/05-progressive-projects.md`

Expected: 每个项目的关键安全和原理连接都有命中。

- [ ] **Step 7: 提交实战教程**

```bash
git add docs/learning/zh-CN/05-progressive-projects.md
git commit -m "docs: add progressive Hermes tutorial projects"
```

### Task 8: 做跨文档完整性、事实和链接验证

**Files:**
- Modify: `docs/learning/zh-CN/README.md`
- Modify: `docs/learning/zh-CN/01-feature-inventory.md`
- Modify: `docs/learning/zh-CN/02-user-learning-path.md`
- Modify: `docs/learning/zh-CN/03-developer-learning-path.md`
- Modify: `docs/learning/zh-CN/04-core-principles-and-labs.md`
- Modify: `docs/learning/zh-CN/05-progressive-projects.md`
- Reference: `docs/superpowers/specs/2026-08-08-hermes-feature-learning-guide-design.md`
- Reference: `website/docs/reference/`

**Interfaces:**
- Consumes: Tasks 1–7 的全部文档。
- Produces: 链接可解析、路径存在、功能域覆盖、无占位符且术语一致的最终文档集。

- [ ] **Step 1: 检查六个交付文件存在且非空**

Run:

```bash
for file in README.md 01-feature-inventory.md 02-user-learning-path.md 03-developer-learning-path.md 04-core-principles-and-labs.md 05-progressive-projects.md; do
  test -s "docs/learning/zh-CN/$file" || exit 1
done
```

Expected: 无输出，退出码为 0。

- [ ] **Step 2: 检查本地相对链接目标**

Run:

```bash
grep -rhoE '\]\((\.\./)+[^)# ]+' docs/learning/zh-CN \
  | sed 's/^](//' \
  | sort -u \
  | while IFS= read -r target; do
      test -e "docs/learning/zh-CN/$target" || { echo "missing: $target"; exit 1; }
    done
```

Expected: 无 `missing:` 输出，退出码为 0。

- [ ] **Step 3: 检查设计规格的功能域覆盖**

Run: `grep -nE '^## [0-9]+\.|交互入口|智能体核心|上下文|工具与执行环境|自动化|消息渠道|扩展体系|配置|安全|动态目录' docs/learning/zh-CN/01-feature-inventory.md`

Expected: 设计规格列出的九大范围及动态索引全部出现。

- [ ] **Step 4: 检查源码路径和命令事实**

逐一核对文档中出现频率最高的入口：

```bash
test -e run_agent.py
test -e model_tools.py
test -e toolsets.py
test -e tools/registry.py
test -e agent/prompt_builder.py
test -e hermes_cli/runtime_provider.py
test -e agent/context_compressor.py
test -e hermes_state.py
test -e gateway/run.py
test -e tui_gateway/server.py
test -e apps/desktop/AGENTS.md
```

Expected: 每条命令退出码为 0。随后将教程中的 CLI 命令逐项与 `website/docs/reference/cli-commands.md`、斜杠命令与 `website/docs/reference/slash-commands.md` 对照；发现差异时修正文档，不修改运行时代码。

- [ ] **Step 5: 扫描占位符、空泛验收和危险配置建议**

Run: `grep -RInE 'TO[D]O|TB[D]|待补充|以后实现|适当处理|理解即可|\.env.*(timeout|threshold|feature|display)' docs/learning/zh-CN`

Expected: 无输出。若命中引用语境，改写以避免被误认为未完成内容或非秘密 `.env` 配置建议。

- [ ] **Step 6: 检查跨文档术语一致性**

统一使用以下首次出现格式，后续可使用英文简称：

```text
智能体循环（Agent Loop）
模型提供商（Provider）
模型上下文协议（MCP）
工具注册表（Tool Registry）
工具集（Toolset）
消息网关（Gateway）
配置档案（Profile）
端到端测试（E2E）
```

Run: `grep -RInE 'Agent Loop|Provider|MCP|Tool Registry|Toolset|Gateway|Profile|E2E' docs/learning/zh-CN`

Expected: 每个术语能在首次出现附近找到中文解释；修复没有解释的缩写。

- [ ] **Step 7: 做最终格式和差异审查**

Run: `git diff --check`

Expected: 无输出，退出码为 0。

Run: `git diff --stat 45d11aa92..HEAD`

Expected: 变更仅包含 `docs/learning/zh-CN/` 文档和已批准的设计/计划文件，没有运行时代码改动。

- [ ] **Step 8: 提交验证修订**

```bash
git add docs/learning/zh-CN
git commit -m "docs: verify Hermes feature learning guide"
```

- [ ] **Step 9: 最终状态检查**

Run: `git status --short`

Expected: 没有本任务产生的未提交修改；用户原有无关修改保持原样。


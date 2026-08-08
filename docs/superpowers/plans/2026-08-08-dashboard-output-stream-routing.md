# Dashboard Output Stream Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Web Dashboard 嵌入的 Hermes TUI 中检测并路由并发会话输出，让用户选择优先流或最多双窗格显示，同时保持单一输入焦点和全局控制请求。

**Architecture:** 在现有 Gateway 事件处理器之前加入 Dashboard 专用输出路由器；活动会话继续使用完整 `TurnController`，非活动会话写入有界轻量缓冲区。布局层把现有 Transcript 作为主窗格，并渲染一个只读副窗格；切换焦点继续调用现有 `session.activate`，成功提交后再交换主副会话。

**Tech Stack:** TypeScript 6、React 19、Ink (`@hermes/ink`)、nanostores、Vitest、Python `tui_gateway` 现有 JSON-RPC 协议。

## Global Constraints

- 第一版只在 `DASHBOARD_TUI_MODE === true` 时自动启用；该常量来自现有 `HERMES_TUI_DASHBOARD`，不得新增配置项或环境变量。
- Web Dashboard 继续使用单一 xterm、单一 PTY 和真实 `hermes --tui`；不得在 `web/` 中重写 Transcript 或 Composer。
- 最多两个会话同时可见；终端宽度 `>= 110` 列使用左右分栏，`< 110` 列使用标签式单窗格呈现。
- 只有主窗格拥有 Composer；副窗格始终只读。点击或快捷键切换到副窗格时必须先成功执行 `session.activate`。
- 冲突只由第二个会话的实际可显示事件触发；每个并发冲突周期最多提示一次，并发输出降到 1 以下后才开始下一周期。
- 第三个及更多输出流进入等待栏；默认副窗格是“当前主会话 + 最新产生输出的会话”，后续输出不得自动替换副窗格。
- 活动流完成后保留最终内容，不自动切换焦点；审批、澄清、sudo、secret 的优先级高于输出冲突选择器。
- 非活动缓冲区最多保留 200 个条目或 64 KiB UTF-8 文本；连续 delta 必须合并，高频 delta 按 `STREAM_BATCH_MS` 批量刷新。
- 非活动窗格不得显示原始 reasoning 文本，只能显示消息、状态、工具和子代理摘要。
- 不修改 Agent 核心、系统提示词、消息角色、模型工具 schema 或提示词缓存行为。
- 每个任务遵循 TDD：先写失败测试、确认失败、最小实现、确认通过，再提交一个聚焦 commit。

---

## File Structure

- `ui-tui/src/app/outputStreamStore.ts`：输出流数据、冲突周期、布局状态、有界缓冲区和主会话快照。
- `ui-tui/src/app/outputStreamRouter.ts`：Gateway 事件分类、Dashboard 开关、高频 delta 批处理和路由结果。
- `ui-tui/src/app/controlPromptQueue.ts`：跨会话控制请求 FIFO、来源标识、完成与过期提升逻辑。
- `ui-tui/src/components/splitOutputPane.tsx`：宽屏双栏、窄屏标签、只读输出和等待栏。
- `ui-tui/src/components/outputConflictPrompt.tsx`：单周期冲突决策卡。
- `ui-tui/src/components/outputManager.tsx`：`/outputs` 管理器，用于切换、替换副窗格和退出分屏。
- 现有 `createGatewayEventHandler.ts` 只负责把事件交给路由器后继续原活动会话路径，不承载缓冲区 reducer。
- 现有 `useSessionLifecycle.ts` 负责 RPC 与原子会话提交；输出状态通过明确的 transition hooks 接入。
- 现有 `appLayout.tsx` 只组合主 Transcript、输出窗格和单一 Composer，不复制 Transcript 状态控制器。

---

### Task 1: 输出流状态、缓冲限制与冲突周期

**Files:**
- Create: `ui-tui/src/app/outputStreamStore.ts`
- Create: `ui-tui/src/__tests__/outputStreamStore.test.ts`

**Interfaces:**
- Produces: `OutputEntry`, `OutputStream`, `OutputConflict`, `OutputStreamsState`。
- Produces: `observeOutputEvent(event, sessionId, options)`, `syncOutputSessions(items, currentSessionId)`, `resolveOutputConflict(decision)`, `setSecondaryOutput(sessionId)`, `exitOutputSplit()`, `resetOutputStreams()`。
- Produces: `$outputStreams`, `$outputConflict`, `$outputLayout` nanostores，供 Tasks 2、4、5、6 使用。

- [ ] **Step 1: 写入 reducer 的失败测试**

```ts
import { beforeEach, describe, expect, it } from 'vitest'
import {
  getOutputStreamsState,
  observeOutputEvent,
  resetOutputStreams,
  resolveOutputConflict
} from '../app/outputStreamStore.js'

describe('output stream state', () => {
  beforeEach(resetOutputStreams)

  it('opens one conflict only when a second producing session paints output', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    expect(getOutputStreamsState().conflict).toBeNull()

    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    expect(getOutputStreamsState().conflict?.candidateSessionId).toBe('sid-b')

    resolveOutputConflict('keep-primary')
    observeOutputEvent({ payload: { name: 'search', preview: 'next' }, type: 'tool.progress' }, 'sid-b', {
      buffer: true,
      now: 3
    })
    expect(getOutputStreamsState().conflict).toBeNull()
  })

  it('starts a new episode only after producing streams drop below two', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    resolveOutputConflict('keep-primary')
    observeOutputEvent({ payload: { text: 'done' }, type: 'message.complete' }, 'sid-b', { buffer: true, now: 3 })
    observeOutputEvent({ type: 'message.start' }, 'sid-b', { buffer: true, now: 4 })
    expect(getOutputStreamsState().conflict).not.toBeNull()
  })
})
```

- [ ] **Step 2: 运行测试并确认缺少模块而失败**

Run: `cd ui-tui && npx vitest run src/__tests__/outputStreamStore.test.ts`

Expected: FAIL，错误包含 `Cannot find module '../app/outputStreamStore.js'`。

- [ ] **Step 3: 定义状态和公开接口**

```ts
export const OUTPUT_ENTRY_LIMIT = 200
export const OUTPUT_BYTE_LIMIT = 64 * 1024
export const OUTPUT_SPLIT_MIN_COLS = 110

export type OutputEntryKind = 'message' | 'status' | 'subagent' | 'system' | 'tool'
export type OutputTerminalStatus = 'closed' | 'completed' | 'disconnected' | 'error'
export type OutputConflictDecision = 'keep-primary' | 'open-manager' | 'prioritize-candidate' | 'split'

export interface OutputEntry {
  complete: boolean
  id: string
  kind: OutputEntryKind
  label?: string
  text: string
  timestamp: number
  tone?: 'error' | 'info' | 'warn'
}

export interface OutputStream {
  bytes: number
  entries: OutputEntry[]
  hasDisplayOutput: boolean
  lastOutputAt: number
  omitted: boolean
  preview: string
  producing: boolean
  sessionId: string
  status: string
  title: string
  unreadCount: number
}

export interface OutputConflict {
  candidateSessionId: string
  episode: number
  primarySessionId: string
}

export interface OutputLayout {
  mode: 'single' | 'split'
  primarySessionId: null | string
  secondarySessionId: null | string
}

export interface OutputStreamsState {
  conflict: null | OutputConflict
  conflictHandled: boolean
  episode: number
  layout: OutputLayout
  streams: Record<string, OutputStream>
}
```

Implement `observeOutputEvent` as a table-driven mapping for exactly these display events: `message.start/delta/interim/complete`, `tool.start/progress/complete`, `status.update`, `background.complete`, `subagent.spawn_requested/start/progress/complete`, and `error`. Ignore reasoning events. Mark `message.complete` and `error` non-producing; never overwrite `completed/error/closed/disconnected` with a later running status.

- [ ] **Step 4: 增加缓冲上限、delta 合并和终态测试**

```ts
it('merges deltas and caps by entries and bytes with an omission marker', () => {
  for (let i = 0; i < 220; i += 1) {
    observeOutputEvent({ payload: { text: `row-${i}-${'x'.repeat(400)}` }, type: 'message.interim' }, 'sid-b', {
      buffer: true,
      now: i
    })
  }
  const stream = getOutputStreamsState().streams['sid-b']!
  expect(stream.entries.length).toBeLessThanOrEqual(200)
  expect(stream.bytes).toBeLessThanOrEqual(64 * 1024)
  expect(stream.omitted).toBe(true)
})

it('does not let a late running event overwrite a terminal state', () => {
  observeOutputEvent({ payload: { message: 'failed' }, type: 'error' }, 'sid-b', { buffer: true, now: 1 })
  observeOutputEvent({ payload: { text: 'running' }, type: 'status.update' }, 'sid-b', { buffer: true, now: 2 })
  expect(getOutputStreamsState().streams['sid-b']?.status).toBe('error')
})
```

- [ ] **Step 5: 运行状态测试**

Run: `cd ui-tui && npx vitest run src/__tests__/outputStreamStore.test.ts`

Expected: PASS，且 Vitest 不报告未清理定时器。

- [ ] **Step 6: 提交状态模型**

```bash
git add ui-tui/src/app/outputStreamStore.ts ui-tui/src/__tests__/outputStreamStore.test.ts
git commit -m "feat(tui): add bounded output stream state"
```

---

### Task 2: Gateway 输出路由与 delta 批处理

**Files:**
- Create: `ui-tui/src/app/outputStreamRouter.ts`
- Create: `ui-tui/src/__tests__/outputStreamRouter.test.ts`
- Modify: `ui-tui/src/app/createGatewayEventHandler.ts:380-390, 710-720`
- Modify: `ui-tui/src/app/interfaces.ts:440-475`
- Modify: `ui-tui/src/app/useMainApp.ts:780-850`
- Test: `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`

**Interfaces:**
- Consumes: Task 1 `observeOutputEvent()` and `resetOutputStreams()`。
- Produces: `OutputRoute = 'active' | 'inactive-control' | 'inactive-output' | 'ignored'`。
- Produces: `createOutputStreamRouter(options): { route; flush; dispose; titleForSession }`。
- Changes: `GatewayEventHandlerContext.outputRouter` becomes the sole pre-filter route dependency.

- [ ] **Step 1: 写路由分类与 Dashboard 开关失败测试**

```ts
const delta = (sessionId: string, text: string): GatewayEvent => ({
  payload: { text },
  session_id: sessionId,
  type: 'message.delta'
})
const clarify = (sessionId: string, question: string): GatewayEvent => ({
  payload: { choices: null, question, request_id: `clarify-${sessionId}` },
  session_id: sessionId,
  type: 'clarify.request'
})

it('keeps standalone TUI filtering unchanged', () => {
  const router = createOutputStreamRouter({ dashboardMode: false })
  expect(router.route(delta('sid-b', 'B'), 'sid-a')).toBe('ignored')
  expect(getOutputStreamsState().streams['sid-b']).toBeUndefined()
})

it('buffers inactive display events only in dashboard mode', () => {
  const router = createOutputStreamRouter({ dashboardMode: true })
  expect(router.route(delta('sid-b', 'B'), 'sid-a')).toBe('inactive-output')
  router.flush()
  expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('B')
})

it('classifies inactive control requests separately', () => {
  const router = createOutputStreamRouter({ dashboardMode: true })
  expect(router.route(clarify('sid-b', 'choose'), 'sid-a')).toBe('inactive-control')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ui-tui && npx vitest run src/__tests__/outputStreamRouter.test.ts`

Expected: FAIL，错误包含缺少 `outputStreamRouter.js`。

- [ ] **Step 3: 实现表驱动分类与批处理**

```ts
const DISPLAY_TYPES = new Set<GatewayEvent['type']>([
  'message.start', 'message.delta', 'message.interim', 'message.complete',
  'tool.start', 'tool.progress', 'tool.complete', 'status.update',
  'background.complete', 'subagent.spawn_requested', 'subagent.start',
  'subagent.progress', 'subagent.complete', 'error'
])
const CONTROL_TYPES = new Set<GatewayEvent['type']>([
  'approval.request', 'clarify.request', 'sudo.request', 'secret.request',
  'sudo.expire', 'secret.expire'
])

export interface OutputStreamRouterOptions {
  batchMs?: number
  dashboardMode?: boolean
  now?: () => number
}

export function createOutputStreamRouter(options: OutputStreamRouterOptions = {}) {
  const dashboardMode = options.dashboardMode ?? DASHBOARD_TUI_MODE
  // message.delta for each session is accumulated and flushed once per
  // batchMs; non-delta display events flush that session first to preserve order.
  return { route, flush, dispose }
}
```

`route()` must always pass `gateway.*` events; active-session events return `active` after updating producing metadata without buffering; inactive reasoning/voice/theme events return `ignored`; `dispose()` clears the timer and flushes no late React update after unmount.

- [ ] **Step 4: 验证 delta 在一个批次内只提交一次 store 更新**

```ts
it('coalesces inactive message deltas at STREAM_BATCH_MS', () => {
  vi.useFakeTimers()
  const router = createOutputStreamRouter({ batchMs: 20, dashboardMode: true })
  router.route(delta('sid-b', 'a'), 'sid-a')
  router.route(delta('sid-b', 'b'), 'sid-a')
  expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('')
  vi.advanceTimersByTime(20)
  expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('ab')
  router.dispose()
  vi.useRealTimers()
})
```

- [ ] **Step 5: 在现有事件处理器最前面接入路由器**

```ts
const route = ctx.outputRouter.route(ev, sid)

if (route === 'inactive-output' || route === 'ignored') {
  return
}

// Task 3 will consume inactive-control through the control queue. Until then,
// preserve the old behavior by returning here instead of mutating active state.
if (route === 'inactive-control') {
  return
}
```

In `useMainApp`, construct the router once with `useMemo`, pass it through `GatewayEventHandlerContext`, and call `dispose()` from an effect cleanup. Remove the old broad `ev.session_id !== sid` guard only after the new route guard is present.

- [ ] **Step 6: 运行路由与事件处理回归测试**

Run: `cd ui-tui && npx vitest run src/__tests__/outputStreamRouter.test.ts src/__tests__/createGatewayEventHandler.test.ts`

Expected: PASS；现有活动会话的 message/tool/todo 测试结果不变。

- [ ] **Step 7: 提交事件路由**

```bash
git add ui-tui/src/app/outputStreamRouter.ts ui-tui/src/app/createGatewayEventHandler.ts ui-tui/src/app/interfaces.ts ui-tui/src/app/useMainApp.ts ui-tui/src/__tests__/outputStreamRouter.test.ts ui-tui/src/__tests__/createGatewayEventHandler.test.ts
git commit -m "feat(tui): route concurrent dashboard output"
```

---

### Task 3: 跨会话控制请求 FIFO 与来源安全响应

**Files:**
- Create: `ui-tui/src/app/controlPromptQueue.ts`
- Create: `ui-tui/src/__tests__/controlPromptQueue.test.ts`
- Modify: `ui-tui/src/types.ts:65-105`
- Modify: `ui-tui/src/app/interfaces.ts:270-315`
- Modify: `ui-tui/src/app/overlayStore.ts:1-150`
- Modify: `ui-tui/src/app/createGatewayEventHandler.ts:1160-1250`
- Modify: `ui-tui/src/components/appOverlays.tsx:45-175`
- Modify: `ui-tui/src/app/useMainApp.ts:650-760, 900-980`
- Modify: `ui-tui/src/app/useInputHandlers.ts:95-220`
- Modify: `ui-tui/src/gatewayTypes.ts:300-330`
- Test: `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`
- Test: `ui-tui/src/__tests__/useInputHandlers.test.ts`

**Interfaces:**
- Consumes: Task 2 `inactive-control` route.
- Produces: `ControlPrompt`, `enqueueControlPrompt()`, `completeControlPrompt()`, `expireControlPrompt()`, `controlPromptFromEvent()`。
- Changes: `ApprovalReq`, `ClarifyReq`, `SudoReq`, `SecretReq` carry `sessionId` and `sessionTitle`.
- Guarantees: only one control prompt is active; the remainder are FIFO in `OverlayState.controlQueue`.

- [ ] **Step 1: 写 FIFO、过期和来源测试**

```ts
const clarifyPrompt = (sid: string, question: string): ControlPrompt => ({
  kind: 'clarify',
  request: { choices: null, question, requestId: `clarify-${sid.at(-1)}`, sessionId: sid, sessionTitle: sid }
})

const secretPrompt = (sid: string, envVar: string): ControlPrompt => ({
  kind: 'secret',
  request: { envVar, prompt: `Enter ${envVar}`, requestId: `secret-${sid.at(-1)}`, sessionId: sid, sessionTitle: sid }
})

const sudoPrompt = (sid: string, requestId: string): ControlPrompt => ({
  kind: 'sudo',
  request: { requestId, sessionId: sid, sessionTitle: sid }
})

it('keeps the first control prompt active and queues the second', () => {
  enqueueControlPrompt(clarifyPrompt('sid-a', 'A?'))
  enqueueControlPrompt(secretPrompt('sid-b', 'TOKEN'))
  expect(getOverlayState().clarify?.sessionId).toBe('sid-a')
  expect(getOverlayState().controlQueue).toHaveLength(1)

  completeControlPrompt('clarify', 'clarify-a')
  expect(getOverlayState().clarify).toBeNull()
  expect(getOverlayState().secret?.sessionId).toBe('sid-b')
})

it('expires the matching queued request without clearing another session', () => {
  enqueueControlPrompt(sudoPrompt('sid-a', 'sudo-a'))
  enqueueControlPrompt(sudoPrompt('sid-b', 'sudo-b'))
  expireControlPrompt('sudo', 'sudo-b')
  expect(getOverlayState().sudo?.requestId).toBe('sudo-a')
  expect(getOverlayState().controlQueue).toEqual([])
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd ui-tui && npx vitest run src/__tests__/controlPromptQueue.test.ts`

Expected: FAIL，错误包含缺少 `controlPromptQueue.js`。

- [ ] **Step 3: 定义控制请求 union 和队列状态**

```ts
export interface ControlPromptSource {
  sessionId: string
  sessionTitle: string
}

export type ControlPrompt =
  | { kind: 'approval'; request: ApprovalReq }
  | { kind: 'clarify'; request: ClarifyReq }
  | { kind: 'secret'; request: SecretReq }
  | { kind: 'sudo'; request: SudoReq }
```

Add `controlQueue: ControlPrompt[]` to `OverlayState` and initialize it to `[]`. Change `resetFlowOverlays()` to preserve active control fields plus `controlQueue`; answered requests and explicit `*.expire` events are the only paths that remove them. `resetOverlayState()` remains a full reset for process/session teardown and tests.

- [ ] **Step 4: 用队列替换四个直接 overlay 写入分支**

`createGatewayEventHandler` must call `controlPromptFromEvent(ev, currentSid, titleLookup)` for both active and inactive control requests. Request events enqueue and return. Expire events remove the exact request and return. Delete the four direct `patchOverlayState({ approval|clarify|sudo|secret })` assignments.

```ts
const control = controlPromptFromEvent(ev, sid, ctx.outputRouter.titleForSession)
if (control?.kind === 'request') {
  enqueueControlPrompt(control.prompt)
  return
}
if (control?.kind === 'expire') {
  expireControlPrompt(control.promptKind, control.requestId)
  return
}
```

When `flushAbandonedClarify()` persists a timed-out clarification, finish it through
`completeControlPrompt('clarify', clarify.requestId)` instead of directly nulling the
overlay, so the next queued control prompt is promoted.

- [ ] **Step 5: 让所有响应使用请求自身的会话来源**

```ts
respondWith('approval.respond', { choice, session_id: approval.sessionId }, done)
rpc('clarify.respond', { answer, request_id: clarify.requestId, session_id: clarify.sessionId })
rpc('sudo.respond', { password: pw, request_id: sudo.requestId, session_id: sudo.sessionId })
rpc('secret.respond', { request_id: secret.requestId, session_id: secret.sessionId, value })
```

On successful or `{status: 'expired'}` responses, call `completeControlPrompt()` so the next FIFO item is promoted. For `expired`, emit `request expired for <sessionTitle>` and do not append an answer as a user message. Extend the four response interfaces in `gatewayTypes.ts` with `status?: 'expired' | 'ok'`.

- [ ] **Step 6: 显示控制请求来源并覆盖 Ctrl+C 路径**

In `PromptZone`, render `from: <sessionTitle>` above approval/clarify/sudo/secret whenever the source is non-empty. Update `dismissSensitivePrompt()` and approval Ctrl+C to use the request's `sessionId`, then promote the next queued prompt.

```ts
expect(rpc).toHaveBeenCalledWith('sudo.respond', {
  password: '', request_id: 'sudo-b', session_id: 'sid-b'
})
```

- [ ] **Step 7: 运行控制请求测试**

Run: `cd ui-tui && npx vitest run src/__tests__/controlPromptQueue.test.ts src/__tests__/createGatewayEventHandler.test.ts src/__tests__/useInputHandlers.test.ts`

Expected: PASS；两个不同 `session_id` 的控制请求按到达顺序展示和响应。

- [ ] **Step 8: 提交全局控制层**

```bash
git add ui-tui/src/app/controlPromptQueue.ts ui-tui/src/types.ts ui-tui/src/gatewayTypes.ts ui-tui/src/app/interfaces.ts ui-tui/src/app/overlayStore.ts ui-tui/src/app/createGatewayEventHandler.ts ui-tui/src/app/useMainApp.ts ui-tui/src/app/useInputHandlers.ts ui-tui/src/components/appOverlays.tsx ui-tui/src/__tests__/controlPromptQueue.test.ts ui-tui/src/__tests__/createGatewayEventHandler.test.ts ui-tui/src/__tests__/useInputHandlers.test.ts
git commit -m "feat(tui): queue cross-session control prompts"
```

---

### Task 4: 原子会话切换与主会话快照

**Files:**
- Modify: `ui-tui/src/app/outputStreamStore.ts`
- Modify: `ui-tui/src/app/useSessionLifecycle.ts:120-390`
- Modify: `ui-tui/src/app/useMainApp.ts:500-560, 1120-1240`
- Modify: `ui-tui/src/app/interfaces.ts:430-510`
- Modify: `ui-tui/src/__tests__/outputStreamStore.test.ts`
- Modify: `ui-tui/src/__tests__/useSessionLifecycle.test.ts`

**Interfaces:**
- Produces: `SessionTransitionKind`, `SessionTransition`, `SessionTransitionHooks`。
- Produces: `capturePrimaryOutputSnapshot(sessionId, title, status, history, streamingText)` and `commitOutputPrimaryTransition(transition)`。
- Changes: `activateLiveSession(id)` resolves `Promise<boolean>`; failure leaves transcript, focus, layout and secondary assignment unchanged.
- Produces: `activateLiveSessionAtomic(options): Promise<boolean>`，用于把响应验证与状态提交隔离成可测试的原子边界。

- [ ] **Step 1: 写快照与失败原子性测试**

```ts
it('seeds the old primary transcript before promoting a live session', () => {
  capturePrimaryOutputSnapshot('sid-a', 'Alpha', 'working', [
    { role: 'user', text: 'question' }, { role: 'assistant', text: 'partial' }
  ], ' tail')
  commitOutputPrimaryTransition({ kind: 'activate-live', nextSessionId: 'sid-b', previousSessionId: 'sid-a' })
  const state = getOutputStreamsState()
  expect(state.layout.primarySessionId).toBe('sid-b')
  expect(state.streams['sid-a']?.entries.map(x => x.text).join(' ')).toContain('partial tail')
})

it('does not run transition hooks for an invalid activation response', async () => {
  const beforeCommit = vi.fn()
  const afterCommit = vi.fn()
  const ok = await activateLiveSessionAtomic({
    id: 'sid-b', previousSessionId: 'sid-a', request: vi.fn().mockResolvedValue({}), beforeCommit, afterCommit, commit: vi.fn(), fail: vi.fn()
  })
  expect(ok).toBe(false)
  expect(beforeCommit).not.toHaveBeenCalled()
  expect(afterCommit).not.toHaveBeenCalled()
})
```

- [ ] **Step 2: 运行目标测试确认失败**

Run: `cd ui-tui && npx vitest run src/__tests__/outputStreamStore.test.ts src/__tests__/useSessionLifecycle.test.ts`

Expected: FAIL，缺少 transition 和 snapshot exports。

- [ ] **Step 3: 定义 transition hooks 并只在有效响应后提交**

```ts
export type SessionTransitionKind = 'activate-live' | 'new-live' | 'replace' | 'resume'
export interface SessionTransition {
  kind: SessionTransitionKind
  nextSessionId: string
  previousSessionId: null | string
}
export interface SessionTransitionHooks {
  afterCommit: (transition: SessionTransition) => void
  beforeCommit: (transition: SessionTransition) => void
}
```

```ts
export interface ActivateLiveSessionAtomicOptions {
  afterCommit: (transition: SessionTransition) => void
  beforeCommit: (transition: SessionTransition) => void
  commit: (response: SessionActivateResponse) => void
  fail: (message: string) => void
  id: string
  previousSessionId: null | string
  request: (id: string) => Promise<unknown>
}

export async function activateLiveSessionAtomic(
  options: ActivateLiveSessionAtomicOptions
): Promise<boolean>

```
For `session.activate`, validate `asRpcResult<SessionActivateResponse>` first, build the transition, call `beforeCommit`, run the existing reset/history/UI hydration, then call `afterCommit`. Catch/invalid paths return `false` without either hook. Apply the same ordering to `newLiveSession`; cold `newSession` and `resumeById` use `replace`/`resume` so the output store exits split mode.

- [ ] **Step 4: 在 `useMainApp` 注入快照和提交回调**

```ts
const transitionHooks = useMemo<SessionTransitionHooks>(() => ({
  beforeCommit: transition => {
    if (transition.previousSessionId && (transition.kind === 'activate-live' || transition.kind === 'new-live')) {
      capturePrimaryOutputSnapshot(
        transition.previousSessionId,
        getUiState().sessionTitle,
        getUiState().status,
        historyItemsRef.current,
        getTurnState().streaming
      )
    }
  },
  afterCommit: commitOutputPrimaryTransition
}), [])
```

Pass `transitionHooks` into `useSessionLifecycle`. Do not snapshot cold sessions that are about to be closed.

- [ ] **Step 5: 运行会话与路由回归测试**

Run: `cd ui-tui && npx vitest run src/__tests__/useSessionLifecycle.test.ts src/__tests__/outputStreamStore.test.ts src/__tests__/activeSessionSwitcher.test.ts`

Expected: PASS；activation failure keeps previous `sid` and layout, successful activation restores full messages plus inflight assistant text.

- [ ] **Step 6: 提交原子切换**

```bash
git add ui-tui/src/app/outputStreamStore.ts ui-tui/src/app/useSessionLifecycle.ts ui-tui/src/app/useMainApp.ts ui-tui/src/app/interfaces.ts ui-tui/src/__tests__/outputStreamStore.test.ts ui-tui/src/__tests__/useSessionLifecycle.test.ts
git commit -m "feat(tui): preserve output state across live session switches"
```

---

### Task 5: 冲突选择器、双栏/标签布局与等待栏

**Files:**
- Create: `ui-tui/src/components/outputConflictPrompt.tsx`
- Create: `ui-tui/src/components/splitOutputPane.tsx`
- Create: `ui-tui/src/__tests__/splitOutputPane.test.tsx`
- Modify: `ui-tui/src/components/appLayout.tsx:130-255, 515-590`
- Modify: `ui-tui/src/components/appOverlays.tsx:45-175`
- Modify: `ui-tui/src/app/interfaces.ts:570-650`
- Modify: `ui-tui/src/app/overlayStore.ts:20-65`
- Modify: `ui-tui/src/app/useMainApp.ts:1040-1260`

**Interfaces:**
- Consumes: Task 1 layout/conflict stores and Task 4 `activateLiveSession(): Promise<boolean>`。
- Produces: `outputPaneMode(cols)`, `outputPaneWidths(cols)`, `SplitOutputPane`, `ReadonlyOutputPane`, `OverflowBar`, `OutputConflictPrompt`。
- Adds App actions: `decideOutputConflict(decision)`, `focusOutputSession(sessionId)`。

- [ ] **Step 1: 写 109/110 列、只读副窗格和等待栏失败测试**

```tsx
import { PassThrough } from 'stream'
import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { stripAnsi } from '../lib/text.js'
import { describe, expect, it, vi } from 'vitest'
import {
  observeOutputEvent,
  setSecondaryOutput,
  syncOutputSessions
} from '../app/outputStreamStore.js'
import { outputPaneMode, SplitOutputPane } from '../components/splitOutputPane.js'


const renderToText = (node: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 120, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => { output += chunk.toString() })
  const app = renderSync(node, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })
  app.unmount()
  app.cleanup()
  return stripAnsi(output)
}

const seedThreeStreamsAndSplit = (a: string, b: string, c: string) => {
  syncOutputSessions([
    { id: a, status: 'working', title: 'Alpha' },
    { id: b, status: 'working', title: 'Beta' },
    { id: c, status: 'working', title: 'Gamma' }
  ], a)
  observeOutputEvent({ payload: { text: 'B' }, type: 'message.interim' }, b, { buffer: true, now: 1 })
  observeOutputEvent({ payload: { text: 'C' }, type: 'message.interim' }, c, { buffer: true, now: 2 })
  setSecondaryOutput(b)
}

it('uses tabs at 109 columns and two panes at 110 columns', () => {
  expect(outputPaneMode(109)).toBe('tabs')
  expect(outputPaneMode(110)).toBe('split')
})

it('renders one readonly secondary and leaves the third stream in overflow', () => {
  seedThreeStreamsAndSplit('sid-a', 'sid-b', 'sid-c')
  const output = renderToText(
    <SplitOutputPane cols={120} onFocusSession={vi.fn()} renderPrimary={() => <Text>PRIMARY</Text>} />
  )
  expect(output).toContain('PRIMARY')
  expect(output).toContain('Beta · read-only')
  expect(output).toContain('waiting: Gamma')
})
```

- [ ] **Step 2: 运行组件测试确认失败**

Run: `cd ui-tui && npx vitest run src/__tests__/splitOutputPane.test.tsx`

Expected: FAIL，缺少 `splitOutputPane.js`。

- [ ] **Step 3: 实现响应式布局和只读条目渲染**

```ts
export const outputPaneMode = (cols: number) => (cols >= OUTPUT_SPLIT_MIN_COLS ? 'split' : 'tabs')
export const outputPaneWidths = (cols: number) => {
  const usable = Math.max(2, cols - 1)
  const primary = Math.floor(usable / 2)
  return { primary, secondary: usable - primary }
}
```

`SplitOutputPane` receives `renderPrimary(width)` so the existing `TranscriptPane` wraps using its real pane width. In split mode render primary, one-cell divider, `ReadonlyOutputPane`; in tabs mode render headers for primary/secondary and only the primary full Transcript after activation. Render an omission line when `stream.omitted` is true. `OverflowBar` lists all streams except primary and secondary with unread counts.

- [ ] **Step 4: 实现优先级最低的冲突提示**

Add `OutputConflictPrompt` after approval/billing/subscription/confirm/clarify/sudo/secret branches in `PromptZone`. Options are `Current`, `New output`, `Split`, and `Other…`; Esc maps to `keep-primary`. Extend `$isBlocked` to compute from both `$overlayState` and `$outputStreams`, blocking Composer only when a conflict is visible and no higher-priority PromptZone item is active.

```tsx
if (output.conflict) {
  return <OutputConflictPrompt conflict={output.conflict} onDecision={onOutputConflictDecision} t={theme} />
}
```

- [ ] **Step 5: 用输出容器组合现有 Transcript**

Replace only the non-agents/non-journey Transcript branch:

```tsx
<SplitOutputPane
  cols={composer.cols}
  onFocusSession={actions.focusOutputSession}
  renderPrimary={paneCols => (
    <TranscriptPane
      actions={actions}
      composer={{ ...composer, cols: paneCols }}
      progress={progress}
      transcript={transcript}
    />
  )}
/>
```

Keep `PromptZone` and `ComposerPane` outside `SplitOutputPane`, spanning the full terminal width, so only one input surface exists.

- [ ] **Step 6: 测试冲突决策不会自动替换副窗格或切换完成流**

```ts
it('keeps a pinned secondary when a third session outputs', () => {
  setSecondaryOutput('sid-b')
  observeOutputEvent({ payload: { text: 'C' }, type: 'message.delta' }, 'sid-c', { buffer: true, now: 3 })
  expect(getOutputStreamsState().layout.secondarySessionId).toBe('sid-b')
})
```

- [ ] **Step 7: 运行布局与现有 Chrome 回归测试**

Run: `cd ui-tui && npx vitest run src/__tests__/splitOutputPane.test.tsx src/__tests__/appChromeBlockedTimers.test.tsx src/__tests__/widgetGridComponent.test.tsx`

Expected: PASS；PromptZone 仍为正常流布局，状态栏计时器规则不回退。

- [ ] **Step 8: 提交输出布局**

```bash
git add ui-tui/src/components/outputConflictPrompt.tsx ui-tui/src/components/splitOutputPane.tsx ui-tui/src/components/appLayout.tsx ui-tui/src/components/appOverlays.tsx ui-tui/src/app/interfaces.ts ui-tui/src/app/overlayStore.ts ui-tui/src/app/useMainApp.ts ui-tui/src/__tests__/splitOutputPane.test.tsx ui-tui/src/__tests__/outputStreamStore.test.ts
git commit -m "feat(tui): add dashboard split output layout"
```

---

### Task 6: `/outputs` 管理器、快捷键和本地补全

**Files:**
- Create: `ui-tui/src/components/outputManager.tsx`
- Create: `ui-tui/src/__tests__/outputManager.test.tsx`
- Modify: `ui-tui/src/app/slash/commands/session.ts:60-165`
- Modify: `ui-tui/src/app/slash/types.ts:10-25`
- Modify: `ui-tui/src/app/overlayStore.ts:15-145`
- Modify: `ui-tui/src/components/appOverlays.tsx:175-430`
- Modify: `ui-tui/src/app/interfaces.ts:410-680`
- Modify: `ui-tui/src/app/useInputHandlers.ts:15-90, 430-540`
- Modify: `ui-tui/src/app/useMainApp.ts:840-940, 1080-1190`
- Modify: `ui-tui/src/hooks/useCompletion.ts:1-45, 80-135`
- Test: `ui-tui/src/__tests__/createSlashHandler.test.ts`
- Test: `ui-tui/src/__tests__/useInputHandlers.test.ts`
- Test: `ui-tui/src/__tests__/useCompletion.test.ts`

**Interfaces:**
- Consumes: Tasks 1/4/5 output actions.
- Produces: `OutputManager`, `outputFocusDirection(key)`, `mergeLocalTuiCommandItems(input, items, dashboardMode)`。
- Adds: `OverlayState.outputs: boolean` and `SlashCommand.dashboardOnly?: boolean`。

- [ ] **Step 1: 写 `/outputs` 本地命令和 Dashboard 补全测试**

```ts
it('opens output manager locally in dashboard mode', () => {
  envState.dashboardTuiMode = true
  const ctx = buildCtx()
  expect(createSlashHandler(ctx)('/outputs')).toBe(true)
  expect(getOverlayState().outputs).toBe(true)
  expect(ctx.gateway.gw.request).not.toHaveBeenCalled()
})

it('merges dashboard-only local commands without duplicating backend items', () => {
  expect(mergeLocalTuiCommandItems('/out', [], true)).toContainEqual(
    expect.objectContaining({ text: '/outputs' })
  )
  expect(mergeLocalTuiCommandItems('/out', [], false)).toEqual([])
})
```

- [ ] **Step 2: 运行命令与补全测试确认失败**

Run: `cd ui-tui && npx vitest run src/__tests__/createSlashHandler.test.ts src/__tests__/useCompletion.test.ts`

Expected: FAIL，`/outputs` 回落到 Gateway 且本地补全函数不存在。

- [ ] **Step 3: 注册 Dashboard-only 命令并接入补全**

```ts
{
  dashboardOnly: true,
  help: 'manage competing output streams',
  name: 'outputs',
  run: (_arg, ctx) => {
    if (!DASHBOARD_TUI_MODE) {
      return ctx.transcript.sys('outputs are available in Dashboard chat')
    }
    patchOverlayState({ outputs: true })
  }
}
```

Generalize `mergeWidgetAppItems` into `mergeLocalTuiCommandItems`; merge both widget apps and `SLASH_COMMANDS`, dedupe by `item.text`, and omit `dashboardOnly` commands when `DASHBOARD_TUI_MODE` is false.

- [ ] **Step 4: 实现 OutputManager 交互**

Render rows from `$outputStreams`, ordered current primary, secondary, then `lastOutputAt` descending. Keys: arrows move, Enter activates selected session, `s` pins/replaces secondary and enters split, `x` exits split, Esc closes. Each row shows title, status, unread count, preview, and `primary`/`secondary`/`waiting` role.

```ts
export interface OutputManagerProps {
  onActivate: (sessionId: string) => void
  onClose: () => void
  onExitSplit: () => void
  onSetSecondary: (sessionId: string) => void
  t: Theme
}
```

Add `outputs` to `hasFloatingPanel`, `$isBlocked`, `FloatingOverlays`, reset preservation, and Ctrl+C/Esc close handling.

- [ ] **Step 5: 添加 Alt+Left/Alt+Right 焦点切换**

```ts
export const outputFocusDirection = (key: {
  leftArrow: boolean; meta: boolean; rightArrow: boolean
}): -1 | 0 | 1 => key.meta && key.leftArrow ? -1 : key.meta && key.rightArrow ? 1 : 0
```

Add `cycleOutputFocus(direction)` to `InputHandlerActions`. Handle it only after blocking prompts return and before history arrow handling. The action activates the other visible session through Task 4; it does not mutate layout before activation succeeds.

- [ ] **Step 6: 运行管理器、命令、快捷键测试**

Run: `cd ui-tui && npx vitest run src/__tests__/outputManager.test.tsx src/__tests__/createSlashHandler.test.ts src/__tests__/useInputHandlers.test.ts src/__tests__/useCompletion.test.ts src/__tests__/slashParity.test.ts`

Expected: PASS；standalone completion does not advertise `/outputs`，Dashboard execution never calls `slash.exec`。

- [ ] **Step 7: 提交管理界面**

```bash
git add ui-tui/src/components/outputManager.tsx ui-tui/src/app/slash/commands/session.ts ui-tui/src/app/slash/types.ts ui-tui/src/app/overlayStore.ts ui-tui/src/components/appOverlays.tsx ui-tui/src/app/interfaces.ts ui-tui/src/app/useInputHandlers.ts ui-tui/src/app/useMainApp.ts ui-tui/src/hooks/useCompletion.ts ui-tui/src/__tests__/outputManager.test.tsx ui-tui/src/__tests__/createSlashHandler.test.ts ui-tui/src/__tests__/useInputHandlers.test.ts ui-tui/src/__tests__/useCompletion.test.ts
git commit -m "feat(tui): add output stream manager"
```

---

### Task 7: 活跃元数据、重连恢复、关闭清理与完整验证

**Files:**
- Modify: `ui-tui/src/app/outputStreamStore.ts`
- Modify: `ui-tui/src/app/outputStreamRouter.ts`
- Modify: `ui-tui/src/app/useMainApp.ts:535-650, 780-880, 960-1040`
- Modify: `ui-tui/src/app/createGatewayEventHandler.ts:590-730`
- Modify: `ui-tui/src/__tests__/outputStreamStore.test.ts`
- Modify: `ui-tui/src/__tests__/outputStreamRouter.test.ts`
- Modify: `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`
- Verify: `docs/superpowers/specs/2026-08-08-dashboard-output-stream-routing-design.md:73`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: `markOutputTransportDisconnected()`, `markOutputTransportReady()`, `removeOutputSession(sessionId)`。
- Finalizes: active-list polling updates titles/status; reconnect clears stale live buffers and restores metadata without auto-focus.

- [ ] **Step 1: 写重连、元数据和关闭失败测试**

```ts
it('restores titles/status after reconnect without choosing a new focus', () => {
  syncOutputSessions([
    { id: 'sid-a', status: 'working', title: 'Alpha' },
    { id: 'sid-b', status: 'working', title: 'Beta' }
  ], 'sid-a')
  observeOutputEvent({ payload: { text: 'stale' }, type: 'message.interim' }, 'sid-b', { buffer: true, now: 1 })

  setSecondaryOutput('sid-b')
  markOutputTransportDisconnected()
  markOutputTransportReady()
  syncOutputSessions([
    { id: 'sid-a', status: 'working', title: 'Alpha' },
    { id: 'sid-b', status: 'idle', title: 'Beta' }
  ], 'sid-a')
  const state = getOutputStreamsState()
  expect(state.layout.primarySessionId).toBe('sid-a')
  expect(state.layout.secondarySessionId).toBe('sid-b')
  expect(state.streams['sid-b']?.title).toBe('Beta')
  expect(state.streams['sid-b']?.entries).toEqual([])
})

it('marks a closed visible stream ended and preserves its final buffer', () => {
  syncOutputSessions([
    { id: 'sid-a', status: 'working', title: 'Alpha' },
    { id: 'sid-b', status: 'working', title: 'Beta' }
  ], 'sid-a')
  observeOutputEvent({ payload: { text: 'final' }, type: 'message.interim' }, 'sid-b', { buffer: true, now: 2 })
  setSecondaryOutput('sid-b')
  const before = getOutputStreamsState().streams['sid-b']?.entries
  removeOutputSession('sid-b')
  expect(getOutputStreamsState().streams['sid-b']?.status).toBe('closed')
  expect(getOutputStreamsState().streams['sid-b']?.entries).toEqual(before)
})
```

- [ ] **Step 2: 运行生命周期测试确认失败**

Run: `cd ui-tui && npx vitest run src/__tests__/outputStreamStore.test.ts src/__tests__/outputStreamRouter.test.ts`

Expected: FAIL，缺少 transport/close APIs。

- [ ] **Step 3: 接入现有 `session.active_list` 轮询**

Inside the existing 1.5s poll in `useMainApp`, after parsing `result.sessions`, call:

```ts
syncOutputSessions(result.sessions, getUiState().sid)
```

The sync updates title/status/model/preview metadata only. It must not create a conflict because an active-list row is not an actual display event. It must not replace `secondarySessionId`.

- [ ] **Step 4: 接入 Gateway 断线、ready 和 session.close**

Call `markOutputTransportDisconnected()` in the existing Gateway `exitHandler`; call `markOutputTransportReady()` from the `gateway.ready` path before the next active-list refresh. Ready clears stale non-primary live entries and unread counts while preserving primary/secondary assignment; the active-list refresh then restores titles and statuses. On successful `closeLiveSession(id)`, call `removeOutputSession(id)`. Preserve visible final entries and show `closed`; non-visible closed streams may be pruned on the next reset.

- [ ] **Step 5: 核对设计文档中的 Dashboard 常量名称**

Confirm the design document already contains this exact gating sentence; do not create a second configuration switch:

```markdown
Dashboard 自动启用条件复用现有 `DASHBOARD_TUI_MODE` 常量；该常量由 PTY 启动器设置的 `HERMES_TUI_DASHBOARD` 解析得到，不增加新的用户配置或环境变量。
```

- [ ] **Step 6: 运行完整自动化验证**

Run: `cd ui-tui && npm run typecheck`

Expected: PASS with exit code 0.

Run: `cd ui-tui && npm test`

Expected: PASS with no failed Vitest files.

Run: `cd ui-tui && npm run lint`

Expected: PASS with no ESLint errors.

Run: `cd ui-tui && npm run build`

Expected: PASS and regenerated TUI bundle completes without unresolved imports.

- [ ] **Step 7: Dashboard 人工 E2E 验证**

Start the existing Dashboard through the repository's documented launcher, connect through the user's SSH port-forward, and perform this exact sequence:

1. In `/chat`, start a long response in session Alpha.
2. Run `/sessions new`, start a second long response in session Beta, and wait until Beta emits its first message/tool/status event.
3. Confirm the conflict card appears once; choose Split.
4. Confirm wide terminal shows Alpha + Beta, only the primary has Composer, and Beta updates live.
5. Start Gamma and confirm it appears only in the waiting bar with unread count.
6. Resize from 110 to 109 columns and back; confirm state, focus and secondary assignment survive.
7. Trigger a clarify or approval in the non-primary session; confirm it replaces the conflict card, shows the source session, and the answer resumes that session.
8. Activate the secondary, confirm full SessionDB history replaces the main transcript, and activation failure simulation leaves the old focus intact.
9. Let the primary finish while another stream runs; confirm no automatic focus switch.
10. Reconnect the browser WebSocket; confirm primary history restores, titles/status refresh, and new non-primary output starts a fresh live buffer.

- [ ] **Step 8: 提交生命周期与验证收尾**

```bash
git add ui-tui/src/app/outputStreamStore.ts ui-tui/src/app/outputStreamRouter.ts ui-tui/src/app/useMainApp.ts ui-tui/src/app/createGatewayEventHandler.ts ui-tui/src/__tests__/outputStreamStore.test.ts ui-tui/src/__tests__/outputStreamRouter.test.ts ui-tui/src/__tests__/createGatewayEventHandler.test.ts docs/superpowers/specs/2026-08-08-dashboard-output-stream-routing-design.md
git commit -m "test(tui): verify dashboard output stream lifecycle"
```

---

## Final Acceptance Gate

- [ ] `git status --short` contains no unexpected generated or temporary files; `.superpowers/` is not committed.
- [ ] `cd ui-tui && npm run typecheck` passes.
- [ ] `cd ui-tui && npm test` passes.
- [ ] `cd ui-tui && npm run lint` passes.
- [ ] `cd ui-tui && npm run build` passes.
- [ ] Dashboard E2E confirms one prompt per conflict episode, max two visible panes, one Composer, overflow waiting list, stable completion, correct control-source routing, and reconnect recovery.
- [ ] Standalone `hermes --tui` still ignores inactive-session display events and does not auto-open conflict UI.
- [ ] No files under `web/`, Agent core, tool schema, system prompt, or config schema changed.

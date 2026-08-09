import { DASHBOARD_TUI_MODE } from '../config/env.js'
import { STREAM_BATCH_MS } from '../config/timing.js'
import type { GatewayEvent } from '../gatewayTypes.js'

import { getOutputStreamsState, observeOutputEvent } from './outputStreamStore.js'
import type { OutputEvent } from './outputStreamStore.js'

export type OutputRoute = 'active' | 'inactive-control' | 'inactive-output' | 'ignored'

export interface OutputStreamRouterOptions {
  batchMs?: number
  dashboardMode?: boolean
  now?: () => number
}

export interface OutputStreamRouter {
  disconnect: () => void
  dispose: () => void
  flush: () => void
  route: (event: GatewayEvent, activeSessionId: null | string) => OutputRoute
  titleForSession: (sessionId: string) => string
}

const DISPLAY_TYPES = new Set<GatewayEvent['type']>([
  'message.start',
  'message.delta',
  'message.interim',
  'message.complete',
  'tool.start',
  'tool.progress',
  'tool.complete',
  'status.update',
  'background.complete',
  'subagent.spawn_requested',
  'subagent.start',
  'subagent.progress',
  'subagent.complete',
  'error'
])

const CONTROL_TYPES = new Set<GatewayEvent['type']>([
  'approval.request',
  'clarify.request',
  'sudo.request',
  'secret.request',
  'sudo.expire',
  'secret.expire'
])

interface PendingDelta {
  event: GatewayEvent
  text: string
}

export function createOutputStreamRouter(options: OutputStreamRouterOptions = {}): OutputStreamRouter {
  const dashboardMode = options.dashboardMode ?? DASHBOARD_TUI_MODE
  const batchMs = options.batchMs ?? STREAM_BATCH_MS
  const now = options.now ?? Date.now
  const pendingDeltas = new Map<string, PendingDelta>()
  let disposed = false
  let disconnected = false
  let timer: null | ReturnType<typeof setTimeout> = null

  const clearTimer = () => {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  const clearPending = () => {
    clearTimer()
    pendingDeltas.clear()
  }

  const flushSession = (sessionId: string) => {
    const pending = pendingDeltas.get(sessionId)

    if (!pending || disposed) {
      return
    }

    pendingDeltas.delete(sessionId)
    observeOutputEvent(
      { ...pending.event, payload: { ...(pending.event.payload ?? {}), text: pending.text } },
      sessionId,
      { buffer: true, now: now() }
    )
  }

  const flush = () => {
    if (disposed) {
      return
    }
    clearTimer()

    for (const sessionId of [...pendingDeltas.keys()]) {
      flushSession(sessionId)
    }
  }

  const scheduleFlush = () => {
    if (timer || disposed) {
      return
    }
    timer = setTimeout(flush, batchMs)
    timer.unref?.()
  }

  const route = (event: GatewayEvent, activeSessionId: null | string): OutputRoute => {
    if (disposed) {
      return 'ignored'
    }

    if (event.type === 'gateway.ready' && disconnected) {
      clearPending()
      disconnected = false
    }

    if (event.type.startsWith('gateway.')) {
      return 'active'
    }

    const sessionId = event.session_id
    const inactive = Boolean(sessionId && activeSessionId && sessionId !== activeSessionId)

    if (inactive) {
      if (!dashboardMode) {
        return 'ignored'
      }

      if (CONTROL_TYPES.has(event.type)) {
        return 'inactive-control'
      }

      if (!DISPLAY_TYPES.has(event.type)) {
        return 'ignored'
      }

      if (event.type === 'message.delta') {
        const text = event.payload?.text

        if (text) {
          const pending = pendingDeltas.get(sessionId!)
          pendingDeltas.set(sessionId!, { event, text: `${pending?.text ?? ''}${text}` })
          scheduleFlush()
        } else {
          observeOutputEvent(event, sessionId!, { buffer: true, now: now() })
        }
      } else {
        flushSession(sessionId!)
        observeOutputEvent(toOutputEvent(event), sessionId!, { buffer: true, now: now() })
      }

      return 'inactive-output'
    }

    if (DISPLAY_TYPES.has(event.type)) {
      const outputSessionId = sessionId ?? activeSessionId

      if (outputSessionId) {
        observeOutputEvent(toOutputEvent(event), outputSessionId, { buffer: false, now: now() })
      }
    }

    return 'active'
  }

  return {
    disconnect: () => {
      disconnected = true
      clearPending()
    },
    dispose: () => {
      disposed = true
      clearPending()
    },
    flush,
    route,
    titleForSession: sessionId => getOutputStreamsState().streams[sessionId]?.title ?? sessionId
  }
}

function toOutputEvent(event: GatewayEvent): OutputEvent {
  return {
    ...(event.payload ? { payload: event.payload as Record<string, unknown> } : {}),
    type: event.type
  }
}

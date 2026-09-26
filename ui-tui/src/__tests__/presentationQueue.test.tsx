import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { ChatSwitch } from '../../../web/src/pages/chat/chat-switch.js'
import { TerminalLifecycle } from '../../../web/src/pages/chat/terminal-lifecycle.js'
import { resetOutputStreams } from '../app/outputStreamStore.js'
import { patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import type * as ComposerModule from '../app/useComposerState.js'
import type { useComposerState } from '../app/useComposerState.js'
import { useMainApp } from '../app/useMainApp.js'
import type { GatewayClient } from '../gatewayClient.js'
import { connectPresentationControl } from '../presentationControl.js'

vi.mock('@/lib/api', () => ({ api: {} }))
vi.mock('@hermes/ink', async () => import('../../packages/hermes-ink/src/entry-exports.js'))
vi.mock('../config/env.js', async importActual => ({
  ...(await importActual<object>()),
  DASHBOARD_TUI_MODE: true,
  INLINE_MODE: false,
  STARTUP_IMAGE: '',
  STARTUP_QUERY: '',
  STARTUP_RESUME_ID: ''
}))
const captured = vi.hoisted(() => ({ composer: null as ReturnType<typeof useComposerState> | null }))
vi.mock('../app/useComposerState.js', async importActual => {
  const actual = await importActual<typeof ComposerModule>()

  return {
    ...actual,
    useComposerState: (...args: Parameters<typeof useComposerState>) => {
      captured.composer = actual.useComposerState(...args)

      return captured.composer
    }
  }
})
const wire = vi.hoisted(() => ({ control: null as EventTarget | null, changed: 0, deliver: (_data: string) => {} }))
vi.mock('undici', () => ({
  WebSocket: class extends EventTarget {
    static OPEN = 1
    readyState = 1
    constructor() {
      super()
      wire.control = this
    }
    send(data: string) {
      const frame = JSON.parse(data)

      if (frame.changed) {
        wire.changed++
      }

      wire.deliver(JSON.stringify({ handoff: true, ...frame }))
    }
    close() {
      this.readyState = 3
    }
  }
}))

// Only drain React effects and the control protocol's existing setImmediate;
// no polling, timer advancement or unrelated gateway events wake the switch.
async function flush() {
  for (let i = 0; i < 12; i++) {
    await new Promise<void>(resolve => setImmediate(resolve))
  }
}

beforeEach(() => {
  resetOutputStreams()
  resetOverlayState()
  resetTurnState()
  resetUiState()
  turnController.fullReset()
  vi.stubEnv('HERMES_TUI_PRESENTATION_URL', 'ws://local/private-publisher')
  vi.stubGlobal('window', { location: { href: 'http://local/chat' } })
  vi.stubGlobal('WebSocket', { OPEN: 1 })
  wire.changed = 0
})
afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

async function setup() {
  const gateway = new EventEmitter()

  const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'image.attach') {
      return { name: 'image.png', path: '/tmp/image.png' }
    }

    if (method === 'setup.status') {
      return { provider_configured: true }
    }

    if (method === 'session.handoff') {
      return params?.action === 'release' ? { released: true } : { ready: true, stored_id: 'durable', ticket: 'ticket' }
    }

    return {}
  })

  Object.assign(gateway, {
    request,
    drain: vi.fn(),
    getLogTail: () => '',
    kill: vi.fn(),
    publishLocalEvent: vi.fn(),
    send: vi.fn(),
    start: vi.fn()
  })
  const stdout = Object.assign(new PassThrough(), { columns: 120, rows: 40, isTTY: true })
  const stdin = Object.assign(new PassThrough(), { isTTY: true, ref() {}, unref() {}, setRawMode() {} })

  function Harness() {
    useMainApp(gateway as unknown as GatewayClient)

    return null
  }

  const app = renderSync(<Harness />, {
    stdout: stdout as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    patchConsole: false
  })

  await flush()
  patchUiState({ sid: 'runtime', busy: false })
  await flush()
  const terminal = new TerminalLifecycle()
  const actions: string[] = []
  const replies: Record<string, unknown>[] = []

  const socket = Object.assign(new EventTarget(), {
    url: 'ws://local/api/pty',
    readyState: 1,
    send(data: string) {
      const frame = JSON.parse(data)
      actions.push(frame.action)
      wire.control!.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ ...frame, input_bytes: 0 }) }))
    },
    close: vi.fn()
  })

  terminal.attach(socket as unknown as WebSocket)

  wire.deliver = data => {
    replies.push(JSON.parse(data))
    terminal.frame(data)
  }

  const disconnect = connectPresentationControl(request as Parameters<typeof connectPresentationControl>[0])
  socket.dispatchEvent(new Event('open'))
  await flush()
  const machine = new ChatSwitch('terminal', 'durable')
  machine.register(terminal, 0)
  const nativeMounts = vi.fn()
  machine.subscribe(() => {
    if (machine.getSnapshot().mounted === 'native') {
      nativeMounts()
    }
  })
  const composer = () => captured.composer!

  return {
    actions,
    composer,
    machine,
    nativeMounts,
    replies,
    request,
    terminal,
    stdin,
    cleanup() {
      machine.dispose()
      disconnect()
      app.unmount()
      app.cleanup()
      socket.dispatchEvent(new Event('close'))
    }
  }
}

it.each([1, 2])('notifies on %i local queue items and resumes once only after the final removal', async count => {
  const h = await setup()

  try {
    expect(h.machine.getSnapshot().phase).toBe('stable-terminal')
    // Deliberately request before the changed frame refresh completes: prepare
    // must independently sample the real mutable composer authority.
    h.composer().actions.setQueueEdit(0)

    if (count === 2) {
      h.composer().actions.enqueue('first')
    }

    h.composer().actions.enqueue('second')
    h.machine.request('native')
    await flush()
    expect(h.replies.some(r => (r.result as { draft?: boolean })?.draft)).toBe(true)
    expect(h.actions.filter(a => a === 'prepare')).toHaveLength(1)
    expect(h.machine.getSnapshot()).toMatchObject({ phase: 'waiting-for-idle', mounted: 'terminal' })
    expect(h.nativeMounts).not.toHaveBeenCalled()
    expect(h.actions).not.toContain('release')

    if (count === 2) {
      const changed = wire.changed
      h.composer().actions.removeQueue(0)
      await flush()
      expect(wire.changed).toBeGreaterThan(changed)
      expect(h.machine.getSnapshot().phase).toBe('waiting-for-idle')
      expect(h.actions).not.toContain('release')
    }

    const beforeFinal = wire.changed
    expect(h.composer().actions.takeQueue(0)?.text).toBe('second')
    await flush()
    expect(wire.changed).toBeGreaterThan(beforeFinal)
    expect(h.actions.filter(a => a === 'prepare')).toHaveLength(2)
    expect(h.actions.filter(a => a === 'release')).toHaveLength(1)
    expect(h.nativeMounts).toHaveBeenCalledTimes(1)
    expect(h.machine.getSnapshot()).toMatchObject({ mounted: 'native', resume: 'durable' })
    expect(h.request.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(0)
  } finally {
    h.cleanup()
  }
})

it('observes queue editing, text/token drafts, multiline buffer, busy and overlays', async () => {
  const h = await setup()

  try {
    const checkTransition = async (change: () => void, blocked: boolean) => {
      const before = wire.changed
      change()
      await flush()
      expect(wire.changed).toBeGreaterThan(before)
      expect(!!h.terminal.status().blocked).toBe(blocked)
    }

    await checkTransition(() => {
      h.composer().actions.setQueueEdit(0)
      h.composer().actions.enqueue('edit me')
    }, true)
    await checkTransition(() => {
      h.composer().actions.setQueueEdit(0)
      h.composer().actions.setInput('edit me')
    }, true)
    // Actual Ink Ctrl-X handler removes the selected queue item and clears input.
    await checkTransition(() => {
      stdinWrite(h.stdin, '\x18')
    }, false)
    expect(h.composer().refs.queueRef.current).toHaveLength(0)
    await checkTransition(() => h.composer().actions.setInputBuf(['multiline']), true)
    await checkTransition(() => h.composer().actions.setInputBuf([]), false)
    await checkTransition(() => h.composer().actions.setInput('draft'), true)
    await checkTransition(() => h.composer().actions.clearIn(), false)

    const pasted = await h
      .composer()
      .actions.handleTextPaste({ text: 'line\n'.repeat(50), value: '', cursor: 0, bracketed: true })

    expect(pasted?.value).toContain('[[')
    await checkTransition(() => h.composer().actions.setInput(pasted!.value), true)
    await checkTransition(() => h.composer().actions.clearIn(), false)
    await h.composer().actions.attachImagePath('/tmp/image.png')
    await flush()
    expect(h.composer().refs.inputRef.current).toContain('[[ Image')
    expect(h.terminal.status().blocked).toBeTruthy()
    await checkTransition(() => {
      h.composer().actions.syncTokens('')
      h.composer().actions.setInput('')
    }, false)
    expect(h.composer().refs.tokensRef.current).toHaveLength(0)
    expect(h.request.mock.calls.some(([method]) => method === 'image.detach')).toBe(true)
    await checkTransition(() => patchUiState({ busy: true }), true)
    await checkTransition(() => patchUiState({ busy: false }), false)
    await checkTransition(() => patchOverlayState({ sessions: true }), true)
    await checkTransition(() => patchOverlayState({ sessions: false }), false)
    expect(h.request.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(0)
  } finally {
    h.cleanup()
  }
})

function stdinWrite(stdin: PassThrough, text: string) {
  stdin.write(text)
}

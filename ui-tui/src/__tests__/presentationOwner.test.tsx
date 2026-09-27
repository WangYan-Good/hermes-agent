import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { resetOutputStreams } from '../app/outputStreamStore.js'
import { patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import type * as ComposerModule from '../app/useComposerState.js'
import type { useComposerState } from '../app/useComposerState.js'
import { useMainApp } from '../app/useMainApp.js'
import type { GatewayClient } from '../gatewayClient.js'
import { $presentationFrozen, handlePresentationControl } from '../presentationControl.js'

vi.mock('@hermes/ink', async () => import('../../packages/hermes-ink/src/entry-exports.js'))
vi.mock('../config/env.js', async importActual => ({
  ...(await importActual<object>()),
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

async function flush() {
  for (let n = 0; n < 8; n++) {
    await new Promise<void>(resolve => setImmediate(resolve))
  }
}
beforeEach(() => {
  resetOutputStreams()
  resetOverlayState()
  resetTurnState()
  resetUiState()
  turnController.fullReset()
  $presentationFrozen.set(false)
  vi.stubEnv('HERMES_TUI_PRESENTATION_URL', '')
})
afterEach(() => vi.unstubAllEnvs())
it('reads the actual composer refs, queue and prompt queue before allowing preparation', async () => {
  const gateway = new EventEmitter()
  const request = vi.fn(async (method: string) => (method === 'setup.status' ? { provider_configured: true } : {}))
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
  const authority = vi.fn().mockResolvedValue({ confirmed: true, ready: true, ticket: 'ticket', cancelled: true })
  const invoke = (action: string) =>
    handlePresentationControl(
      { type: 'control', id: 'id', instance: 'instance', generation: 'generation', input_bytes: 0, action },
      () => 0,
      authority
    )

  try {
    await flush()
    patchUiState({ sid: 'runtime', busy: false })
    await flush()
    const composer = () => captured.composer!
    composer().actions.setInput('draft without a React commit')
    expect((await invoke('prepare')).blocked).toContain('composer')
    await invoke('cancel')
    composer().actions.clearIn()
    await flush()
    composer().actions.setInputBuf(['multiline'])
    expect((await invoke('prepare')).blocked).toContain('composer')
    await invoke('cancel')
    composer().actions.clearIn()
    await flush()
    composer().actions.setQueueEdit(0)
    composer().actions.enqueue('queued')
    expect((await invoke('prepare')).blocked).toContain('queue')
    await invoke('cancel')
    composer().actions.takeQueue(0)
    composer().actions.setQueueEdit(null)
    await flush()
    patchOverlayState({
      controlQueue: [{ kind: 'sudo', request: { sessionId: 'runtime', sessionTitle: 'Terminal', requestId: 'prompt' } }]
    })
    expect((await invoke('prepare')).blocked).toContain('interaction')
    await invoke('cancel')
    resetOverlayState()
    patchUiState({ busy: true })
    await flush()
    expect((await invoke('prepare')).blocked).toContain('turn')
    await invoke('cancel')
    patchUiState({ busy: false })
    await flush()
    expect((await invoke('prepare')).ready).toBe(true)
    expect(request.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(false)
  } finally {
    app.unmount()
    app.cleanup()
  }
})

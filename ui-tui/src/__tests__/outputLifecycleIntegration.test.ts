import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React, { useLayoutEffect, useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { createOutputLifecycleCoordinator } from '../app/outputLifecycleCoordinator.js'
import { createOutputStreamRouter } from '../app/outputStreamRouter.js'
import {
  commitOutputPrimaryTransition,
  getOutputStreamsState,
  resetOutputStreams,
  setSecondaryOutput,
  syncOutputSessions
} from '../app/outputStreamStore.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { useSessionLifecycle } from '../app/useSessionLifecycle.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { Msg } from '../types.js'

type Lifecycle = ReturnType<typeof useSessionLifecycle>

const ref = <T,>(current: T) => ({ current })

const flushPromises = async () => {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve()
  }
}

const makeStreams = () => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', () => {})

  return { stderr, stdin, stdout }
}

function LifecycleHarness({ expose, gw }: { expose: React.MutableRefObject<Lifecycle | null>; gw: GatewayClient }) {
  const [history, setHistory] = useState<Msg[]>([])

  const lifecycle = useSessionLifecycle({
    colsRef: ref(100),
    composerActions: { setComposerTokens: vi.fn() } as any,
    getHistoryItems: () => history,
    gw,
    panel: vi.fn(),
    rpc: vi.fn(async () => null),
    scrollRef: ref(null),
    setHistoryItems: setHistory,
    setLastUserMsg: vi.fn(),
    setSessionStartedAt: vi.fn(),
    setStickyPrompt: vi.fn(),
    setVoiceProcessing: vi.fn(),
    setVoiceRecording: vi.fn(),
    sys: vi.fn(),
    transitionHooks: {
      afterCommit: commitOutputPrimaryTransition,
      beforeCommit: vi.fn()
    }
  })

  useLayoutEffect(() => {
    expose.current = lifecycle
  }, [expose, lifecycle])

  return React.createElement(Text, null, '')
}

describe('dashboard output lifecycle integration', () => {
  beforeEach(() => {
    resetOverlayState()
    resetOutputStreams()
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('preserves split focus through exit, ready, and the real recovery resume commit', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'session.resume') {
        return { messages: [], session_id: 'sid-a', status: 'idle' }
      }

      return null
    })

    const gw = { request } as unknown as GatewayClient
    const expose = ref<Lifecycle | null>(null)
    const streams = makeStreams()

    const app = renderSync(React.createElement(LifecycleHarness, { expose, gw }), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    expect(expose.current).not.toBeNull()
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' }
      ],
      'sid-a'
    )
    setSecondaryOutput('sid-b')
    patchUiState({ sid: 'sid-a' })

    const outputRouter = createOutputStreamRouter({ dashboardMode: true })
    const outputLifecycle = createOutputLifecycleCoordinator(outputRouter)
    const recoverSidRef = ref<null | string>('sid-a')

    const onEvent = createGatewayEventHandler({
      composer: { setInput: vi.fn() },
      gateway: { gw, rpc: vi.fn(async () => null) },
      outputRouter,
      outputLifecycle,
      session: {
        STARTUP_RESUME_ID: '',
        colsRef: ref(100),
        newSession: vi.fn(),
        recoverSidRef,
        resetSession: expose.current!.resetSession,
        resumeById: expose.current!.resumeById,
        setCatalog: vi.fn()
      },
      submission: { submitRef: ref(vi.fn()) },
      system: { bellOnComplete: false, sys: vi.fn() },
      transcript: { appendMessage: vi.fn(), panel: vi.fn(), setHistoryItems: vi.fn() },
      voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn(), setVoiceTts: vi.fn() }
    })

    outputLifecycle.disconnect()
    patchUiState({ busy: false, sid: null, status: 'gateway exited' })
    onEvent({ payload: {}, type: 'gateway.ready' })
    await flushPromises()

    expect(getUiState().sid).toBe('sid-a')
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'sid-a',
      secondarySessionId: 'sid-b'
    })

    onEvent.dispose()
    outputRouter.dispose()
    app.unmount()
    app.cleanup()
  })
})

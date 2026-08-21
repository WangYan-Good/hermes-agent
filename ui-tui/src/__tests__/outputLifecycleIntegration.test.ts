import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React, { useLayoutEffect, useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import type { GatewayRpc } from '../app/interfaces.js'
import { createOutputLifecycleCoordinator } from '../app/outputLifecycleCoordinator.js'
import { createOutputStreamRouter } from '../app/outputStreamRouter.js'
import {
  commitOutputPrimaryTransition,
  getOutputStreamsState,
  observeOutputEvent,
  resetOutputStreams,
  type SessionTransitionHooks,
  setSecondaryOutput,
  syncOutputSessions
} from '../app/outputStreamStore.js'
import { createOutputSubscriptionCoordinator, resetOutputSubscriptionState } from '../app/outputSubscriptionCoordinator.js'
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

function LifecycleHarness({ expose, gw, rpc, sys = vi.fn(), transitionHooks }: {
  expose: React.MutableRefObject<Lifecycle | null>
  gw: GatewayClient
  rpc?: GatewayRpc
  sys?: (text: string) => void
  transitionHooks?: SessionTransitionHooks
}) {
  const [history, setHistory] = useState<Msg[]>([])

  const lifecycle = useSessionLifecycle({
    colsRef: ref(100),
    composerActions: { setComposerTokens: vi.fn() } as any,
    getHistoryItems: () => history,
    gw,
    panel: vi.fn(),
    rpc: rpc ?? vi.fn(async () => null),
    scrollRef: ref(null),
    setHistoryItems: setHistory,
    setLastUserMsg: vi.fn(),
    setSessionStartedAt: vi.fn(),
    setStickyPrompt: vi.fn(),
    setVoiceProcessing: vi.fn(),
    setVoiceRecording: vi.fn(),
    sys,
    transitionHooks: transitionHooks ?? {
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
    resetOutputSubscriptionState()
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('resumes by durable key and atomically remaps the recovered runtime session', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'session.resume') {
        return {
          message_count: 1,
          messages: [],
          resumed: 'stored-a',
          running: false,
          session_id: 'runtime-new-a',
          session_key: 'stored-a',
          started_at: 1,
          status: 'idle'
        }
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
        { id: 'runtime-old-a', session_key: 'stored-a', status: 'working', title: 'Alpha' },
        { id: 'runtime-old-b', session_key: 'stored-b', status: 'working', title: 'Beta' }
      ],
      'runtime-old-a'
    )
    observeOutputEvent({ payload: { text: 'preserved output' }, type: 'message.interim' }, 'runtime-old-a', {
      buffer: true,
      now: 10
    })
    setSecondaryOutput('runtime-old-b')
    patchUiState({ sid: 'runtime-old-a' })

    const outputRouter = createOutputStreamRouter({ dashboardMode: true })
    const outputLifecycle = createOutputLifecycleCoordinator(outputRouter)
    const recoverSidRef = ref<null | string>('stored-a')

    const onEvent = createGatewayEventHandler({
      composer: { cancelQueued: vi.fn(), setInput: vi.fn() },
      gateway: { gw, rpc: vi.fn(async () => null) },
      outputRouter,
      outputLifecycle,
      outputSubscriptions: createOutputSubscriptionCoordinator(vi.fn()),
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

    expect(request).toHaveBeenCalledWith('session.resume', { cols: 100, session_id: 'stored-a' })
    expect(getUiState().sid).toBe('runtime-new-a')
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'runtime-new-a',
      secondarySessionId: 'runtime-old-b'
    })
    expect(getOutputStreamsState().streams['runtime-old-a']).toBeUndefined()
    expect(getOutputStreamsState().streams['runtime-new-a']).toMatchObject({
      entries: [expect.objectContaining({ text: 'preserved output' })],
      sessionId: 'runtime-new-a',
      sessionKey: 'stored-a',
      title: 'Alpha'
    })

    onEvent.dispose()
    outputRouter.dispose()
    app.unmount()
    app.cleanup()
  })

  it('records session.create stored_session_id as the new output stream durable key', async () => {
    const request = vi.fn(async () => null)

    const rpc = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'session.create') {
        return { session_id: 'runtime-created', stored_session_id: 'stored-created' }
      }

      return null
    }) as unknown as GatewayRpc

    const gw = { request } as unknown as GatewayClient
    const expose = ref<Lifecycle | null>(null)
    const streams = makeStreams()

    const app = renderSync(React.createElement(LifecycleHarness, { expose, gw, rpc }), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    await expose.current!.newLiveSession()
    await flushPromises()

    expect(getUiState().sid).toBe('runtime-created')
    expect(getOutputStreamsState().streams['runtime-created']).toMatchObject({
      sessionId: 'runtime-created',
      sessionKey: 'stored-created'
    })

    app.unmount()
    app.cleanup()
  })

  it('keeps the disconnected layout unchanged when durable recovery fails', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'session.resume') {
        throw new Error('resume failed')
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

    syncOutputSessions(
      [
        { id: 'runtime-old-a', session_key: 'stored-a', status: 'working' },
        { id: 'runtime-old-b', session_key: 'stored-b', status: 'working' }
      ],
      'runtime-old-a'
    )
    setSecondaryOutput('runtime-old-b')
    patchUiState({ sid: 'runtime-old-a' })

    const outputRouter = createOutputStreamRouter({ dashboardMode: true })
    const outputLifecycle = createOutputLifecycleCoordinator(outputRouter)
    const recoverSidRef = ref<null | string>('stored-a')

    const onEvent = createGatewayEventHandler({
      composer: { cancelQueued: vi.fn(), setInput: vi.fn() },
      gateway: { gw, rpc: vi.fn(async () => null) },
      outputRouter,
      outputLifecycle,
      outputSubscriptions: createOutputSubscriptionCoordinator(vi.fn()),
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

    expect(request).toHaveBeenCalledWith('session.resume', { cols: 100, session_id: 'stored-a' })
    expect(getUiState().sid).toBeNull()
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'runtime-old-a',
      secondarySessionId: 'runtime-old-b'
    })
    expect(getOutputStreamsState().streams['runtime-old-a']).toBeDefined()

    onEvent.dispose()
    outputRouter.dispose()
    app.unmount()
    app.cleanup()
  })

  it('reports a recovery identity collision and keeps UI, layout, and private streams unchanged', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'setup.status') {
        return { provider_configured: true }
      }

      if (method === 'session.resume') {
        return {
          inflight: null,
          message_count: 1,
          messages: [],
          resumed: 'stored-a',
          running: false,
          session_id: 'runtime-b',
          session_key: 'stored-a',
          started_at: 1,
          status: 'idle'
        }
      }

      return null
    })

    const gw = { request } as unknown as GatewayClient
    const expose = ref<Lifecycle | null>(null)
    const streams = makeStreams()
    const sys = vi.fn()
    const afterCommit = vi.fn()
    const outputRouter = createOutputStreamRouter({ dashboardMode: true })
    const outputLifecycle = createOutputLifecycleCoordinator(outputRouter)

    const app = renderSync(
      React.createElement(LifecycleHarness, {
        expose,
        gw,
        sys,
        transitionHooks: { afterCommit, beforeCommit: outputLifecycle.validateTransition }
      }),
      {
        patchConsole: false,
        stderr: streams.stderr as NodeJS.WriteStream,
        stdin: streams.stdin as NodeJS.ReadStream,
        stdout: streams.stdout as NodeJS.WriteStream
      }
    )

    syncOutputSessions(
      [
        { id: 'runtime-a', session_key: 'stored-a', status: 'working', title: 'Alpha' },
        { id: 'runtime-b', session_key: 'stored-b', status: 'working', title: 'Beta' }
      ],
      'runtime-a'
    )
    observeOutputEvent({ payload: { text: 'A private' }, type: 'message.interim' }, 'runtime-a', {
      buffer: true,
      now: 10
    })
    observeOutputEvent({ payload: { text: 'B private' }, type: 'message.interim' }, 'runtime-b', {
      buffer: true,
      now: 20
    })
    setSecondaryOutput('runtime-b')
    patchUiState({ sid: 'runtime-a', status: 'ready' })
    const outputBefore = structuredClone(getOutputStreamsState())

    expose.current!.resumeById('stored-a', 'recover')
    await flushPromises()

    expect(getUiState()).toMatchObject({ sid: 'runtime-a', status: 'ready' })
    expect(getOutputStreamsState()).toEqual(outputBefore)
    expect(sys).toHaveBeenCalledWith(expect.stringMatching(/session identity collision/i))
    expect(afterCommit).not.toHaveBeenCalled()

    outputRouter.dispose()
    app.unmount()
    app.cleanup()
  })
})

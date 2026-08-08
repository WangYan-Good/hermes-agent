import { beforeEach, describe, expect, it, vi } from 'vitest'

import { enqueueControlPrompt } from '../app/controlPromptQueue.js'
import { createOutputLifecycleCoordinator } from '../app/outputLifecycleCoordinator.js'
import { createOutputStreamRouter } from '../app/outputStreamRouter.js'
import { getOutputStreamsState, observeOutputEvent, resetOutputStreams } from '../app/outputStreamStore.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'

const approval = (sessionId: string) => ({
  kind: 'approval' as const,
  request: { command: 'deploy', description: 'approve?', sessionId }
})

const sudo = (sessionId: string) => ({
  kind: 'sudo' as const,
  request: { requestId: `sudo-${sessionId}`, sessionId }
})

describe('output lifecycle coordinator', () => {
  beforeEach(() => {
    resetOutputStreams()
    resetOverlayState()
    resetTurnState()
    turnController.fullReset()
  })

  it('applies active-list metadata without overwriting newer live fields', () => {
    const router = createOutputStreamRouter({ dashboardMode: true })
    const lifecycle = createOutputLifecycleCoordinator(router)

    lifecycle.syncActiveSessions(
      [
        { id: 'sid-a', status: 'idle', title: 'Alpha' },
        { id: 'sid-b', model: 'old', preview: 'old', status: 'idle', title: 'Beta' }
      ],
      'sid-a'
    )
    router.route({ payload: { text: 'new live' }, session_id: 'sid-b', type: 'message.delta' }, 'sid-a')
    router.flush()
    lifecycle.syncActiveSessions(
      [
        { id: 'sid-a', status: 'idle', title: 'Alpha' },
        { id: 'sid-b', model: 'new', preview: 'stale', status: 'idle', title: 'Beta renamed' }
      ],
      'sid-a'
    )

    expect(getOutputStreamsState().streams['sid-b']).toMatchObject({
      model: 'new',
      preview: 'new live',
      producing: true,
      status: 'running',
      title: 'Beta renamed',
      unreadCount: 1
    })
  })

  it('coordinates disconnect cleanup across router, streams, controls, and turn state', () => {
    vi.useFakeTimers()
    const router = createOutputStreamRouter({ batchMs: 20, dashboardMode: true })
    const lifecycle = createOutputLifecycleCoordinator(router)

    lifecycle.syncActiveSessions([{ id: 'sid-a', status: 'working', title: 'Alpha' }], 'sid-a')
    enqueueControlPrompt(sudo('sid-a'))
    turnController.hydrateStreamingText('stale active tail')
    router.route({ payload: { text: 'stale pending' }, session_id: 'sid-b', type: 'message.delta' }, 'sid-a')

    lifecycle.disconnect()
    vi.advanceTimersByTime(20)

    expect(getOutputStreamsState().streams['sid-a']).toMatchObject({ producing: false, status: 'disconnected' })
    expect(getOutputStreamsState().streams['sid-b']).toBeUndefined()
    expect(getOverlayState().sudo).toBeNull()
    expect(turnController.bufRef).toBe('')
  })

  it('cleans only a successfully closed target and leaves failures untouched', () => {
    const router = createOutputStreamRouter({ dashboardMode: true })
    const lifecycle = createOutputLifecycleCoordinator(router)

    lifecycle.syncActiveSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' },
        { id: 'sid-c', status: 'waiting', title: 'Gamma' }
      ],
      'sid-a'
    )
    observeOutputEvent({ payload: { text: 'final B' }, type: 'message.interim' }, 'sid-b', { buffer: true })
    enqueueControlPrompt(approval('sid-a'))
    enqueueControlPrompt(sudo('sid-b'))

    expect(lifecycle.applyCloseResult('sid-a', { closed: false, ok: false })).toBe(false)
    expect(getOutputStreamsState().streams['sid-a']?.status).toBe('working')
    expect(getOverlayState().approval?.sessionId).toBe('sid-a')

    expect(lifecycle.applyCloseResult('sid-b', { closed: true })).toBe(true)
    expect(getOutputStreamsState().streams['sid-b']).toMatchObject({
      entries: [expect.objectContaining({ text: 'final B' })],
      producing: false,
      status: 'closed'
    })
    expect(getOverlayState().approval?.sessionId).toBe('sid-a')
    expect(getOverlayState().controlQueue).toEqual([])

    expect(lifecycle.applyCloseResult('sid-c', { ok: true })).toBe(true)
    expect(getOutputStreamsState().streams['sid-c']?.status).toBe('closed')
  })
})

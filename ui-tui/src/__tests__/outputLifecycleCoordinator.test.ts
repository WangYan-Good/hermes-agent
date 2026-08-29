import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createOutputLifecycleCoordinator } from '../app/outputLifecycleCoordinator.js'
import type { OutputStreamRouter } from '../app/outputStreamRouter.js'
import { getOutputStreamsState, observeOutputEvent, resetOutputStreams } from '../app/outputStreamStore.js'

const router = (): OutputStreamRouter => ({
  disconnect: vi.fn(),
  dispose: vi.fn(),
  flush: vi.fn(),
  route: vi.fn(() => 'ignored'),
  titleForSession: vi.fn(() => '')
})

beforeEach(resetOutputStreams)

describe('single-window output lifecycle coordinator', () => {
  it('commits one active session and synchronizes cached metadata', () => {
    const coordinator = createOutputLifecycleCoordinator(router())

    coordinator.syncActiveSessions(
      [
        { id: 'sid-a', title: 'Alpha' },
        { id: 'sid-b', title: 'Beta' }
      ],
      'sid-a'
    )
    coordinator.commitTransition({ kind: 'activate-live', nextSessionId: 'sid-b', previousSessionId: 'sid-a' })

    const state = getOutputStreamsState()
    expect(state.activeSessionId).toBe('sid-b')
    expect(state.streams['sid-b']?.title).toBe('Beta')
  })

  it('marks the active stream disconnected and readies transport without adding a pane', () => {
    const outputRouter = router()
    const coordinator = createOutputLifecycleCoordinator(outputRouter)
    coordinator.syncActiveSessions([{ id: 'sid-a', session_key: 'stored-a' }], 'sid-a')
    observeOutputEvent({ payload: { text: 'answer' }, type: 'message.interim' }, 'sid-a', { buffer: true, now: 1 })

    expect(coordinator.disconnect('sid-a')).toBe('stored-a')
    expect(outputRouter.disconnect).toHaveBeenCalledOnce()
    expect(coordinator.ready()).toBe(true)
    expect(getOutputStreamsState().activeSessionId).toBe('sid-a')
  })
})

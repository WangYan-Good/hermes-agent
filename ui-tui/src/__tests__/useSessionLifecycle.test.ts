import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getOutputStreamsState, resetOutputStreams, setSecondaryOutput, syncOutputSessions } from '../app/outputStreamStore.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import {
  activateLiveSessionAtomic,
  createSessionIntentGeneration,
  hydrateLiveSessionInflight,
  liveSessionInflightMessages,
  scheduleResumeScrollToBottom,
  signalFreshSessionBoundary,
  writeActiveSessionFile
} from '../app/useSessionLifecycle.js'

const activation = (sessionId = 'sid-b') => ({
  inflight: null,
  messages: [],
  session_id: sessionId,
  status: 'idle' as const
})

const deferred = <T>() => {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(res => {
    resolve = res
  })

  return { promise, resolve }
}

const staleActivationAfter = async (nextIntent: 'new-live' | 'resume') => {
  const intents = createSessionIntentGeneration()
  const pending = deferred<ReturnType<typeof activation>>()
  const commit = vi.fn()

  const result = activateLiveSessionAtomic({
    afterCommit: vi.fn(),
    beforeCommit: vi.fn(),
    commit,
    fail: vi.fn(),
    id: 'sid-b',
    isCurrent: intents.begin('activate-live'),
    previousSessionId: 'sid-a',
    request: () => pending.promise
  })

  intents.begin(nextIntent)
  pending.resolve(activation())

  return { commit, ok: await result }
}

describe('fresh session boundary', () => {
  it('signals only when a live session is replaced by a different session', () => {
    const onFreshSessionStarted = vi.fn()

    expect(signalFreshSessionBoundary('old-session', 'new-session', onFreshSessionStarted)).toBe(true)
    expect(signalFreshSessionBoundary(null, 'first-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('same-session', 'same-session', onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', null, onFreshSessionStarted)).toBe(false)
    expect(signalFreshSessionBoundary('old-session', 'new-session')).toBe(false)
    expect(onFreshSessionStarted).toHaveBeenCalledOnce()
    expect(onFreshSessionStarted).toHaveBeenCalledWith('new-session')
  })
})

describe('writeActiveSessionFile', () => {
  let dir = ''

  afterEach(() => {
    if (dir) {
      rmSync(dir, { force: true, recursive: true })
      dir = ''
    }
  })

  it('writes the actual resumed session id for the shell exit summary', () => {
    dir = mkdtempSync(join(tmpdir(), 'hermes-tui-active-'))
    const path = join(dir, 'active.json')

    writeActiveSessionFile('actual_session', path)

    expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual({ session_id: 'actual_session' })
  })
})

describe('live session activation in-flight state', () => {
  beforeEach(() => {
    resetUiState()
    resetTurnState()
    turnController.fullReset()
    patchUiState({ streaming: true })
  })

  it('keeps the in-flight user prompt in history and hydrates partial assistant text', () => {
    const inflight = { assistant: 'partial answer', streaming: true, user: 'write a long answer' }

    expect(liveSessionInflightMessages(inflight)).toEqual([{ role: 'user', text: 'write a long answer' }])

    hydrateLiveSessionInflight(inflight)

    expect(turnController.bufRef).toBe('partial answer')
    expect(getTurnState().streaming).toBe('partial answer')
  })

  it('ignores empty in-flight payloads', () => {
    expect(liveSessionInflightMessages({ assistant: '', streaming: false, user: '   ' })).toEqual([])

    hydrateLiveSessionInflight({ assistant: '', streaming: false, user: '' })

    expect(turnController.bufRef).toBe('')
    expect(getTurnState().streaming).toBe('')
  })
})


describe('atomic live session activation', () => {
  it('does not run transition hooks for an invalid activation response', async () => {
    const beforeCommit = vi.fn()
    const afterCommit = vi.fn()
    const commit = vi.fn()

    const ok = await activateLiveSessionAtomic({
      afterCommit,
      beforeCommit,
      commit,
      fail: vi.fn(),
      id: 'sid-b',
      previousSessionId: 'sid-a',
      request: vi.fn().mockResolvedValue({})
    })

    expect(ok).toBe(false)
    expect(beforeCommit).not.toHaveBeenCalled()
    expect(commit).not.toHaveBeenCalled()
    expect(afterCommit).not.toHaveBeenCalled()
  })

  it('does not commit a stale activation after a newer switch intent', async () => {
    const beforeCommit = vi.fn()
    const afterCommit = vi.fn()
    const commit = vi.fn()

    const ok = await activateLiveSessionAtomic({
      afterCommit,
      beforeCommit,
      commit,
      fail: vi.fn(),
      id: 'sid-a',
      isCurrent: () => false,
      previousSessionId: 'sid-b',
      request: vi.fn().mockResolvedValue(activation('sid-a'))
    })

    expect(ok).toBe(false)
    expect(beforeCommit).not.toHaveBeenCalled()
    expect(commit).not.toHaveBeenCalled()
    expect(afterCommit).not.toHaveBeenCalled()
  })

  it('commits a validated activation between its transition hooks', async () => {
    const events: string[] = []

    const ok = await activateLiveSessionAtomic({
      afterCommit: () => events.push('after'),
      beforeCommit: () => events.push('before'),
      commit: response => {
        expect(response.session_id).toBe('sid-b')
        events.push('commit')
      },
      fail: message => events.push(`fail:${message}`),
      id: 'sid-b',
      previousSessionId: 'sid-a',
      request: vi.fn().mockResolvedValue(activation())
    })

    expect(ok).toBe(true)
    expect(events).toEqual(['before', 'commit', 'after'])
  })

  it('does not let a deferred activation overwrite a new live intent', async () => {
    const { commit, ok } = await staleActivationAfter('new-live')
    expect(ok).toBe(false)
    expect(commit).not.toHaveBeenCalled()
  })

  it('does not let a deferred activation overwrite a resume intent', async () => {
    const { commit, ok } = await staleActivationAfter('resume')
    expect(ok).toBe(false)
    expect(commit).not.toHaveBeenCalled()
  })


  it('propagates a throwing before hook without changing layout or focus', async () => {
    resetOutputStreams()
    syncOutputSessions([{ id: 'sid-a', title: 'Alpha' }, { id: 'sid-b', title: 'Beta' }], 'sid-a')
    setSecondaryOutput('sid-b')
    patchUiState({ sid: 'sid-a' })
    const afterCommit = vi.fn()

    await expect(activateLiveSessionAtomic({
      afterCommit,
      beforeCommit: () => { throw new Error('before') },
      commit: () => patchUiState({ sid: 'sid-b' }),
      fail: vi.fn(),
      id: 'sid-b',
      previousSessionId: 'sid-a',
      request: vi.fn().mockResolvedValue(activation())
    })).rejects.toThrow('before')

    expect(getUiState().sid).toBe('sid-a')
    expect(getOutputStreamsState().layout).toEqual({ mode: 'split', primarySessionId: 'sid-a', secondarySessionId: 'sid-b' })
    expect(afterCommit).not.toHaveBeenCalled()
  })

  it('propagates a throwing commit without changing layout or focus', async () => {
    resetOutputStreams()
    syncOutputSessions([{ id: 'sid-a', title: 'Alpha' }, { id: 'sid-b', title: 'Beta' }], 'sid-a')
    setSecondaryOutput('sid-b')
    patchUiState({ sid: 'sid-a' })
    const afterCommit = vi.fn()

    await expect(activateLiveSessionAtomic({
      afterCommit,
      beforeCommit: vi.fn(),
      commit: () => { throw new Error('commit') },
      fail: vi.fn(),
      id: 'sid-b',
      previousSessionId: 'sid-a',
      request: vi.fn().mockResolvedValue(activation())
    })).rejects.toThrow('commit')

    expect(getUiState().sid).toBe('sid-a')
    expect(getOutputStreamsState().layout).toEqual({ mode: 'split', primarySessionId: 'sid-a', secondarySessionId: 'sid-b' })
    expect(afterCommit).not.toHaveBeenCalled()
  })
})
describe('resume scroll settle', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('re-snaps while sticky and stops when the user scrolls away', () => {
    vi.useFakeTimers()
    let sticky = true
    let lastManualScrollAt = 0
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => lastManualScrollAt,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80, 240]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    sticky = false
    lastManualScrollAt = Date.now() + 1
    vi.advanceTimersByTime(160)
    expect(scrollToBottom).toHaveBeenCalledTimes(2)

    cancel()
  })

  it('cancels pending resume snaps', () => {
    vi.useFakeTimers()
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => true,
          scrollToBottom
        }
      } as any,
      [20]
    )

    cancel()
    vi.advanceTimersByTime(20)

    expect(scrollToBottom).not.toHaveBeenCalled()
  })

  it('keeps the immediate resume snap even before sticky state settles', () => {
    vi.useFakeTimers()
    let sticky = false
    const scrollToBottom = vi.fn()

    const cancel = scheduleResumeScrollToBottom(
      {
        current: {
          getLastManualScrollAt: () => 0,
          isSticky: () => sticky,
          scrollToBottom
        }
      } as any,
      [0, 80]
    )

    vi.advanceTimersByTime(0)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    vi.advanceTimersByTime(80)
    expect(scrollToBottom).toHaveBeenCalledTimes(1)

    sticky = true
    cancel()
  })
})

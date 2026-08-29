import { beforeEach, describe, expect, it } from 'vitest'

import {
  commitActiveOutputTransition,
  getOutputStreamsState,
  markOutputTransportDisconnected,
  markOutputTransportReady,
  observeOutputEvent,
  OutputSessionIdentityCollisionError,
  resetOutputStreams,
  syncOutputSessions
} from '../app/outputStreamStore.js'

beforeEach(resetOutputStreams)

describe('single-window output stream cache', () => {
  it('keeps background output cached without replacing the active session', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', session_key: 'stored-a', status: 'working' },
        { id: 'sid-b', session_key: 'stored-b', status: 'working' }
      ],
      'sid-a'
    )

    observeOutputEvent({ payload: { text: 'background' }, type: 'message.delta' }, 'sid-b', {
      buffer: true,
      now: 1
    })

    const state = getOutputStreamsState()

    expect(state.activeSessionId).toBe('sid-a')
    expect(state.streams['sid-b']?.entries.at(-1)?.text).toBe('background')
  })

  it('replaces the one active session on every committed switch', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working' },
        { id: 'sid-b', status: 'idle' }
      ],
      'sid-a'
    )

    commitActiveOutputTransition({ kind: 'activate-live', nextSessionId: 'sid-b', previousSessionId: 'sid-a' })

    expect(getOutputStreamsState().activeSessionId).toBe('sid-b')
  })

  it('synchronizes the one active session to the current session', () => {
    syncOutputSessions([{ id: 'sid-a' }, { id: 'sid-b' }], 'sid-a')
    syncOutputSessions([{ id: 'sid-a' }, { id: 'sid-b' }], 'sid-b')

    expect(getOutputStreamsState().activeSessionId).toBe('sid-b')
  })

  it('preserves streamed completion text when completion omits text', () => {
    observeOutputEvent({ payload: { text: 'partial answer' }, type: 'message.delta' }, 'sid-a', {
      buffer: true,
      now: 1
    })
    observeOutputEvent({ payload: { status: 'complete' }, type: 'message.complete' }, 'sid-a', {
      buffer: true,
      now: 2
    })

    expect(getOutputStreamsState().streams['sid-a']?.entries.at(-1)).toMatchObject({
      complete: true,
      text: 'partial answer'
    })
  })

  it('retains only the active cached output when transport recovery completes', () => {
    syncOutputSessions([{ id: 'sid-a' }, { id: 'sid-b' }], 'sid-a')
    observeOutputEvent({ payload: { text: 'active' }, type: 'message.interim' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { text: 'background' }, type: 'message.interim' }, 'sid-b', {
      buffer: true,
      now: 2
    })

    markOutputTransportDisconnected()
    expect(markOutputTransportReady()).toBe(true)

    const state = getOutputStreamsState()
    expect(state.activeSessionId).toBe('sid-a')
    expect(state.streams['sid-a']?.entries.at(-1)?.text).toBe('active')
    expect(state.streams['sid-b']?.entries).toEqual([])
  })

  it('remaps the active cache by durable session key on recovery', () => {
    syncOutputSessions([{ id: 'runtime-old', session_key: 'stored-a' }], 'runtime-old')
    observeOutputEvent({ payload: { text: 'durable output' }, type: 'message.interim' }, 'runtime-old', {
      buffer: true,
      now: 1
    })

    commitActiveOutputTransition({
      kind: 'recover',
      nextSessionId: 'runtime-new',
      previousSessionId: null,
      sessionKey: 'stored-a'
    })

    const state = getOutputStreamsState()
    expect(state.activeSessionId).toBe('runtime-new')
    expect(state.streams['runtime-old']).toBeUndefined()
    expect(state.streams['runtime-new']?.entries.at(-1)?.text).toBe('durable output')
  })

  it('rejects recovery into a runtime id owned by another durable session', () => {
    syncOutputSessions(
      [
        { id: 'runtime-a', session_key: 'stored-a' },
        { id: 'runtime-b', session_key: 'stored-b' }
      ],
      'runtime-a'
    )

    expect(() =>
      commitActiveOutputTransition({
        kind: 'recover',
        nextSessionId: 'runtime-b',
        previousSessionId: null,
        sessionKey: 'stored-a'
      })
    ).toThrow(OutputSessionIdentityCollisionError)
  })
})

import { beforeEach, describe, expect, it } from 'vitest'

import {
  capturePrimaryOutputSnapshot,
  commitOutputPrimaryTransition,
  exitOutputSplit,
  getOutputStreamsState,
  markOutputTransportDisconnected,
  markOutputTransportReady,
  observeOutputEvent,
  removeOutputSession,
  resetOutputStreams,
  resolveOutputConflict,
  setSecondaryOutput,
  syncOutputSessions
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

  it('does not buffer, mark unread, or conflict on a non-painting message start', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ type: 'message.start' }, 'sid-b', { buffer: true, now: 2 })

    const candidate = getOutputStreamsState().streams['sid-b']!
    expect(candidate.entries).toEqual([])
    expect(candidate.hasDisplayOutput).toBe(false)
    expect(candidate.unreadCount).toBe(0)
    expect(getOutputStreamsState().conflict).toBeNull()
  })

  it('starts a new episode only after producing streams drop below two', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    resolveOutputConflict('keep-primary')
    observeOutputEvent({ payload: { text: 'done' }, type: 'message.complete' }, 'sid-b', { buffer: true, now: 3 })
    observeOutputEvent({ type: 'message.start' }, 'sid-b', { buffer: true, now: 4 })
    observeOutputEvent({ payload: { text: 'again' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 5 })
    expect(getOutputStreamsState().conflict).not.toBeNull()
  })

  it('resets a handled episode when a stream drops before its stale conflict is resolved', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    observeOutputEvent({ payload: { text: 'done' }, type: 'message.complete' }, 'sid-b', { buffer: true, now: 3 })

    resolveOutputConflict('keep-primary')
    observeOutputEvent({ type: 'message.start' }, 'sid-b', { buffer: true, now: 4 })
    observeOutputEvent({ payload: { text: 'again' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 5 })

    expect(getOutputStreamsState().conflict?.candidateSessionId).toBe('sid-b')
  })

  it('seals an already streamed interim without duplicating its delta text', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { text: 'same' }, type: 'message.delta' }, 'sid-a', { buffer: true, now: 2 })
    observeOutputEvent({ payload: { already_streamed: true, text: 'same' }, type: 'message.interim' }, 'sid-a', {
      buffer: true,
      now: 3
    })

    const messages = getOutputStreamsState().streams['sid-a']!.entries.filter(entry => entry.kind === 'message')
    expect(messages).toEqual([expect.objectContaining({ complete: true, text: 'same' })])
  })

  it('seals a delta with its matching completion without duplicate text', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { text: 'same' }, type: 'message.delta' }, 'sid-a', { buffer: true, now: 2 })
    observeOutputEvent({ payload: { text: 'same' }, type: 'message.complete' }, 'sid-a', { buffer: true, now: 3 })

    const messages = getOutputStreamsState().streams['sid-a']!.entries.filter(entry => entry.kind === 'message')
    expect(messages).toEqual([expect.objectContaining({ complete: true, text: 'same' })])
  })

  it('seals an empty completion without replacing accumulated deltas with an event name', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { text: 'First. ' }, type: 'message.delta' }, 'sid-a', { buffer: true, now: 2 })
    observeOutputEvent({ payload: { text: 'second.' }, type: 'message.delta' }, 'sid-a', { buffer: true, now: 3 })
    observeOutputEvent({ payload: {}, type: 'message.complete' }, 'sid-a', { buffer: true, now: 4 })

    const messages = getOutputStreamsState().streams['sid-a']!.entries.filter(entry => entry.kind === 'message')
    expect(messages).toEqual([expect.objectContaining({ complete: true, text: 'First. second.' })])
  })

  it('uses rendered completion text when text is unavailable', () => {
    observeOutputEvent({ payload: { text: 'draft' }, type: 'message.delta' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { rendered: 'final' }, type: 'message.complete' }, 'sid-a', { buffer: true, now: 2 })

    const messages = getOutputStreamsState().streams['sid-a']!.entries.filter(entry => entry.kind === 'message')
    expect(messages).toEqual([expect.objectContaining({ complete: true, text: 'final' })])
  })

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
    expect(stream.entries.some(entry => entry.id === 'omitted')).toBe(true)
  })

  it('does not let a late running event overwrite a terminal state', () => {
    observeOutputEvent({ payload: { message: 'failed' }, type: 'error' }, 'sid-b', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { text: 'running' }, type: 'status.update' }, 'sid-b', { buffer: true, now: 2 })
    expect(getOutputStreamsState().streams['sid-b']?.status).toBe('error')
  })

  it('does not let message start overwrite a terminal status', () => {
    observeOutputEvent({ payload: { message: 'failed' }, type: 'error' }, 'sid-b', { buffer: true, now: 1 })
    observeOutputEvent({ type: 'message.start' }, 'sid-b', { buffer: true, now: 2 })
    expect(getOutputStreamsState().streams['sid-b']?.status).toBe('error')
  })

  it('ends a new round after an error without leaving a false competing producer', () => {
    observeOutputEvent({ payload: { message: 'failed' }, type: 'error' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: true, now: 2 })
    observeOutputEvent({ payload: { text: 'done' }, type: 'message.complete' }, 'sid-a', { buffer: true, now: 3 })
    observeOutputEvent({ payload: { text: 'other' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 4 })

    expect(getOutputStreamsState().streams['sid-a']).toMatchObject({ producing: false, status: 'completed' })
    expect(getOutputStreamsState().conflict).toBeNull()
  })

  it('syncs session titles and exits a manually selected split view', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', title: 'Primary' },
        { id: 'sid-b', title: 'Secondary' }
      ],
      'sid-a'
    )
    setSecondaryOutput('sid-b')
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'sid-a',
      secondarySessionId: 'sid-b'
    })
    expect(getOutputStreamsState().streams['sid-b']?.title).toBe('Secondary')

    exitOutputSplit()
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'single',
      primarySessionId: 'sid-a',
      secondarySessionId: null
    })
  })

  it('keeps a primary selection in single layout', () => {
    syncOutputSessions([{ id: 'sid-a', title: 'Primary' }], 'sid-a')
    setSecondaryOutput('sid-a')

    expect(getOutputStreamsState().layout).toEqual({
      mode: 'single',
      primarySessionId: 'sid-a',
      secondarySessionId: null
    })
  })

  it('keeps a pinned secondary when a third session outputs', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', title: 'Alpha' },
        { id: 'sid-b', title: 'Beta' },
        { id: 'sid-c', title: 'Gamma' }
      ],
      'sid-a'
    )
    setSecondaryOutput('sid-b')

    observeOutputEvent({ payload: { text: 'C' }, type: 'message.delta' }, 'sid-c', { buffer: true, now: 3 })

    expect(getOutputStreamsState().layout.secondarySessionId).toBe('sid-b')
  })

  it('seeds the old primary transcript before promoting a live session', () => {
    capturePrimaryOutputSnapshot(
      'sid-a',
      'Alpha',
      'working',
      [
        { role: 'user', text: 'question' },
        { role: 'assistant', text: 'partial' }
      ],
      ' tail'
    )

    commitOutputPrimaryTransition({ kind: 'activate-live', nextSessionId: 'sid-b', previousSessionId: 'sid-a' })

    const state = getOutputStreamsState()
    expect(state.layout.primarySessionId).toBe('sid-b')
    expect(state.layout.secondarySessionId).toBe('sid-a')
    expect(state.streams['sid-a']?.entries.map(entry => entry.text).join(' ')).toContain('partial tail')
  })

  it('keeps a persisted user tail separate from the streaming assistant snapshot', () => {
    capturePrimaryOutputSnapshot('sid-a', 'Alpha', 'working', [{ role: 'user', text: 'question' }], 'partial answer')

    const entries = getOutputStreamsState().streams['sid-a']?.entries
    expect(entries).toEqual([
      expect.objectContaining({ complete: true, kind: 'message', text: 'question' }),
      expect.objectContaining({ complete: false, kind: 'message', text: 'partial answer' })
    ])
  })
  it('syncs active session model, preview, status, and title without changing the selected layout', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', model: 'model-a', preview: 'alpha preview', status: 'working', title: 'Alpha' },
        { id: 'sid-b', model: 'model-b', preview: 'beta preview', status: 'waiting', title: 'Beta' }
      ],
      'sid-a'
    )
    setSecondaryOutput('sid-b')
    syncOutputSessions(
      [
        { id: 'sid-a', model: 'model-a-next', preview: 'alpha next', status: 'idle', title: 'Alpha renamed' },
        { id: 'sid-b', model: 'model-b-next', preview: 'beta next', status: 'idle', title: 'Beta renamed' }
      ],
      'sid-a'
    )

    const state = getOutputStreamsState()
    expect(state.layout).toEqual({ mode: 'split', primarySessionId: 'sid-a', secondarySessionId: 'sid-b' })
    expect(state.streams['sid-a']).toMatchObject({
      model: 'model-a-next',
      preview: 'alpha next',
      status: 'idle',
      title: 'Alpha renamed'
    })
    expect(state.streams['sid-b']).toMatchObject({
      model: 'model-b-next',
      preview: 'beta next',
      status: 'idle',
      title: 'Beta renamed'
    })
    expect(state.conflict).toBeNull()
  })

  it('restores titles and status after reconnect without choosing a new focus', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' }
      ],
      'sid-a'
    )
    observeOutputEvent({ payload: { text: 'stale' }, type: 'message.interim' }, 'sid-b', {
      buffer: true,
      now: 1
    })
    setSecondaryOutput('sid-b')

    markOutputTransportDisconnected()
    expect(getOutputStreamsState().streams['sid-b']).toMatchObject({ producing: false, status: 'disconnected' })

    markOutputTransportReady()
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'idle', title: 'Beta' }
      ],
      'sid-a'
    )

    const state = getOutputStreamsState()
    expect(state.layout.primarySessionId).toBe('sid-a')
    expect(state.layout.secondarySessionId).toBe('sid-b')
    expect(state.streams['sid-b']).toMatchObject({
      entries: [],
      hasDisplayOutput: false,
      preview: '',
      status: 'idle',
      title: 'Beta',
      unreadCount: 0
    })
  })

  it('marks only a closed visible stream ended and preserves its final buffer', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' },
        { id: 'sid-c', status: 'working', title: 'Gamma' }
      ],
      'sid-a'
    )
    observeOutputEvent({ payload: { text: 'final' }, type: 'message.interim' }, 'sid-b', {
      buffer: true,
      now: 2
    })
    observeOutputEvent({ payload: { text: 'still running' }, type: 'message.interim' }, 'sid-c', {
      buffer: true,
      now: 3
    })
    setSecondaryOutput('sid-b')
    const before = getOutputStreamsState().streams['sid-b']?.entries

    removeOutputSession('sid-b')

    const state = getOutputStreamsState()
    expect(state.layout.secondarySessionId).toBe('sid-b')
    expect(state.streams['sid-b']).toMatchObject({ entries: before, producing: false, status: 'closed' })
    expect(state.streams['sid-c']).toMatchObject({ producing: true, status: 'running' })
  })

  it('keeps live fields when a stale active-list poll arrives after a delta', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'idle', title: 'Alpha' },
        { id: 'sid-b', model: 'old-model', preview: 'old preview', status: 'idle', title: 'Beta' }
      ],
      'sid-a'
    )
    observeOutputEvent({ payload: { text: 'new live preview' }, type: 'message.delta' }, 'sid-b', {
      buffer: true,
      now: 10
    })
    const live = getOutputStreamsState().streams['sid-b']!

    syncOutputSessions(
      [
        { id: 'sid-a', status: 'idle', title: 'Alpha' },
        { id: 'sid-b', model: 'new-model', preview: 'stale preview', status: 'idle', title: 'Beta renamed' }
      ],
      'sid-a'
    )

    expect(getOutputStreamsState().streams['sid-b']).toMatchObject({
      entries: live.entries,
      model: 'new-model',
      preview: 'new live preview',
      producing: true,
      status: 'running',
      title: 'Beta renamed',
      unreadCount: 1
    })
  })

  it('keeps ordinary user resume semantics separate from recovery resume', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'idle', title: 'Alpha' },
        { id: 'sid-b', status: 'idle', title: 'Beta' }
      ],
      'sid-a'
    )
    setSecondaryOutput('sid-b')

    commitOutputPrimaryTransition({ kind: 'resume', nextSessionId: 'sid-a', previousSessionId: null })

    expect(getOutputStreamsState().layout).toEqual({
      mode: 'single',
      primarySessionId: 'sid-a',
      secondarySessionId: null
    })
  })

  it.each([
    { disconnect: true, eventType: 'message.complete', producing: false, status: 'completed', text: 'new done' },
    { disconnect: false, eventType: 'error', producing: false, status: 'error', text: 'new failed' },
    { disconnect: true, eventType: 'message.delta', producing: true, status: 'running', text: 'new live' }
  ])('keeps destination event state when recover remaps for each terminal or running event', ({ disconnect, eventType, producing, status, text }) => {
    syncOutputSessions([{ id: 'runtime-old', session_key: 'stored-a', status: 'working' }], 'runtime-old')
    observeOutputEvent({ payload: { text: 'old private' }, type: 'message.interim' }, 'runtime-old', {
      buffer: true,
      now: 10
    })

    if (disconnect) {
      markOutputTransportDisconnected()
    }

    observeOutputEvent({ payload: { text }, type: eventType }, 'runtime-new', { buffer: true, now: 20 })

    commitOutputPrimaryTransition({
      kind: 'recover',
      nextSessionId: 'runtime-new',
      previousSessionId: null,
      sessionKey: 'stored-a'
    })

    const stream = getOutputStreamsState().streams['runtime-new']!
    expect(getOutputStreamsState().streams['runtime-old']).toBeUndefined()
    expect(stream).toMatchObject({ preview: text, producing, sessionKey: 'stored-a', status })
    expect(stream.entries.map(entry => entry.text)).toEqual(['old private', text])
  })

  it('rejects recovery remap before mutating streams when the destination belongs to another durable key', () => {
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
    const before = structuredClone(getOutputStreamsState())

    expect(() =>
      commitOutputPrimaryTransition({
        kind: 'recover',
        nextSessionId: 'runtime-b',
        previousSessionId: 'runtime-a',
        sessionKey: 'stored-a'
      })
    ).toThrow(/session identity collision/i)
    expect(getOutputStreamsState()).toEqual(before)
  })

  it('rejects active-list remap before mutating streams when the target runtime has another durable key', () => {
    syncOutputSessions(
      [
        { id: 'runtime-current', session_key: 'stored-current', status: 'working' },
        { id: 'runtime-a', session_key: 'stored-a', status: 'working' },
        { id: 'runtime-b', session_key: 'stored-b', status: 'working' }
      ],
      'runtime-current'
    )
    observeOutputEvent({ payload: { text: 'A private' }, type: 'message.interim' }, 'runtime-a', {
      buffer: true,
      now: 10
    })
    observeOutputEvent({ payload: { text: 'B private' }, type: 'message.interim' }, 'runtime-b', {
      buffer: true,
      now: 20
    })
    setSecondaryOutput('runtime-a')
    const before = structuredClone(getOutputStreamsState())

    expect(() =>
      syncOutputSessions(
        [{ id: 'runtime-b', session_key: 'stored-a', status: 'working', title: 'Wrong owner' }],
        'runtime-current'
      )
    ).toThrow(/session identity collision/i)
    expect(getOutputStreamsState()).toEqual(before)
  })

  it('preflights an active-list batch before mutating any stream', () => {
    syncOutputSessions(
      [
        { id: 'runtime-current', session_key: 'stored-current', status: 'working', title: 'Before' },
        { id: 'runtime-a', session_key: 'stored-a', status: 'working', title: 'Alpha' },
        { id: 'runtime-b', session_key: 'stored-b', status: 'working', title: 'Beta' }
      ],
      'runtime-current'
    )
    const before = structuredClone(getOutputStreamsState())

    expect(() =>
      syncOutputSessions(
        [
          {
            id: 'runtime-current',
            session_key: 'stored-current',
            status: 'working',
            title: 'MUTATED BEFORE COLLISION'
          },
          { id: 'runtime-b', session_key: 'stored-a', status: 'working', title: 'Wrong owner' }
        ],
        'runtime-current'
      )
    ).toThrow(/session identity collision/i)
    expect(getOutputStreamsState()).toEqual(before)
  })

  it.each([
    { eventType: 'tool.complete', payload: { name: 'search', text: 'tool done' }, text: 'tool done' },
    {
      eventType: 'subagent.progress',
      payload: { message: 'subagent update', name: 'worker' },
      text: 'subagent update'
    },
    { eventType: 'background.complete', payload: { summary: 'background done' }, text: 'background done' }
  ])(
    'does not let lifecycle-neutral $eventType overwrite recovered running state',
    ({ eventType, payload, text }) => {
      syncOutputSessions(
        [{ id: 'runtime-old', session_key: 'stored-a', status: 'working', title: 'Alpha' }],
        'runtime-old'
      )
      observeOutputEvent({ payload: { text: 'old live' }, type: 'message.delta' }, 'runtime-old', {
        buffer: true,
        now: 10
      })
      syncOutputSessions([{ id: 'runtime-new', status: 'working', title: 'Recovered metadata' }], 'runtime-old')
      observeOutputEvent({ payload, type: eventType }, 'runtime-new', { buffer: true, now: 20 })

      commitOutputPrimaryTransition({
        kind: 'recover',
        nextSessionId: 'runtime-new',
        previousSessionId: 'runtime-old',
        sessionKey: 'stored-a'
      })

      const stream = getOutputStreamsState().streams['runtime-new']!
      expect(stream).toMatchObject({ producing: true, sessionKey: 'stored-a', status: 'running' })
      expect(stream.entries.map(entry => entry.text)).toEqual(['old live', text])
    }
  )
})

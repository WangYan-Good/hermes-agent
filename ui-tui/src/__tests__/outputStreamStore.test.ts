import { beforeEach, describe, expect, it } from 'vitest'

import {
  exitOutputSplit,
  getOutputStreamsState,
  observeOutputEvent,
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
    observeOutputEvent(
      { payload: { already_streamed: true, text: 'same' }, type: 'message.interim' },
      'sid-a',
      { buffer: true, now: 3 }
    )

    const messages = getOutputStreamsState().streams['sid-a']!.entries.filter(entry => entry.kind === 'message')
    expect(messages).toEqual([
      expect.objectContaining({ complete: true, text: 'same' })
    ])
  })

  it('seals a delta with its matching completion without duplicate text', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: true, now: 1 })
    observeOutputEvent({ payload: { text: 'same' }, type: 'message.delta' }, 'sid-a', { buffer: true, now: 2 })
    observeOutputEvent({ payload: { text: 'same' }, type: 'message.complete' }, 'sid-a', { buffer: true, now: 3 })

    const messages = getOutputStreamsState().streams['sid-a']!.entries.filter(entry => entry.kind === 'message')
    expect(messages).toEqual([
      expect.objectContaining({ complete: true, text: 'same' })
    ])
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
    syncOutputSessions([{ id: 'sid-a', title: 'Primary' }, { id: 'sid-b', title: 'Secondary' }], 'sid-a')
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
})

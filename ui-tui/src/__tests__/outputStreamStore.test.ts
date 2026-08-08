import { beforeEach, describe, expect, it } from 'vitest'

import {
  getOutputStreamsState,
  observeOutputEvent,
  resetOutputStreams,
  resolveOutputConflict,
  setSecondaryOutput,
  syncOutputSessions,
  exitOutputSplit
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

  it('starts a new episode only after producing streams drop below two', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    resolveOutputConflict('keep-primary')
    observeOutputEvent({ payload: { text: 'done' }, type: 'message.complete' }, 'sid-b', { buffer: true, now: 3 })
    observeOutputEvent({ type: 'message.start' }, 'sid-b', { buffer: true, now: 4 })
    expect(getOutputStreamsState().conflict).not.toBeNull()
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
})

import { beforeEach, describe, expect, it } from 'vitest'

import { getOutputStreamsState, observeOutputEvent, resetOutputStreams } from '../app/outputStreamStore.js'

describe('output stream rendered delta handling', () => {
  beforeEach(resetOutputStreams)

  it('ignores a rendered-only delta instead of painting or competing', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { rendered: '\u001b[31mANSI\u001b[0m' }, type: 'message.delta' }, 'sid-b', {
      buffer: true,
      now: 2
    })

    const stream = getOutputStreamsState().streams['sid-b']!
    expect(stream.entries).toEqual([])
    expect(stream.hasDisplayOutput).toBe(false)
    expect(stream.unreadCount).toBe(0)
    expect(getOutputStreamsState().conflict).toBeNull()
  })
})

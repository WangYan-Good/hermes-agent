import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createOutputStreamRouter } from '../app/outputStreamRouter.js'
import { getOutputStreamsState, resetOutputStreams } from '../app/outputStreamStore.js'
import type { GatewayEvent } from '../gatewayTypes.js'

const delta = (sessionId: string, text: string): GatewayEvent => ({
  payload: { text },
  session_id: sessionId,
  type: 'message.delta'
})

const clarify = (sessionId: string, question: string): GatewayEvent => ({
  payload: { choices: null, question, request_id: `clarify-${sessionId}` },
  session_id: sessionId,
  type: 'clarify.request'
})

describe('output stream router', () => {
  beforeEach(resetOutputStreams)
  afterEach(() => vi.useRealTimers())

  it('keeps standalone TUI filtering unchanged', () => {
    const router = createOutputStreamRouter({ dashboardMode: false })

    expect(router.route(delta('sid-b', 'B'), 'sid-a')).toBe('ignored')
    expect(getOutputStreamsState().streams['sid-b']).toBeUndefined()
  })

  it('buffers inactive display events only in dashboard mode', () => {
    const router = createOutputStreamRouter({ dashboardMode: true })

    expect(router.route(delta('sid-b', 'B'), 'sid-a')).toBe('inactive-output')
    router.flush()

    expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('B')
  })

  it('classifies inactive control requests separately', () => {
    const router = createOutputStreamRouter({ dashboardMode: true })

    expect(router.route(clarify('sid-b', 'choose'), 'sid-a')).toBe('inactive-control')
  })

  it('coalesces inactive message deltas at the configured batch interval', () => {
    vi.useFakeTimers()
    const router = createOutputStreamRouter({ batchMs: 20, dashboardMode: true })

    router.route(delta('sid-b', 'a'), 'sid-a')
    router.route(delta('sid-b', 'b'), 'sid-a')
    expect(getOutputStreamsState().streams['sid-b']?.preview).toBeUndefined()

    vi.advanceTimersByTime(20)

    expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('ab')
    router.dispose()
  })

  it('passes gateway events through and tracks active display metadata without buffering it', () => {
    const router = createOutputStreamRouter({ dashboardMode: true })

    expect(router.route({ payload: { skin: {} }, type: 'gateway.ready' }, 'sid-a')).toBe('active')
    expect(router.route(delta('sid-a', 'A'), 'sid-a')).toBe('active')

    const active = getOutputStreamsState().streams['sid-a']
    expect(active).toMatchObject({ hasDisplayOutput: true, producing: true })
    expect(active?.entries).toEqual([])
  })

  it('discards a pending batch when disposed', () => {
    vi.useFakeTimers()
    const router = createOutputStreamRouter({ batchMs: 20, dashboardMode: true })

    router.route(delta('sid-b', 'later'), 'sid-a')
    router.dispose()
    vi.advanceTimersByTime(20)

    expect(getOutputStreamsState().streams['sid-b']).toBeUndefined()
  })
  it('keeps a pending batch across an ordinary gateway ready event', () => {
    vi.useFakeTimers()
    const router = createOutputStreamRouter({ batchMs: 20, dashboardMode: true })

    router.route(delta('sid-b', 'still live'), 'sid-a')
    expect(router.route({ payload: {}, type: 'gateway.ready' }, 'sid-a')).toBe('active')
    vi.advanceTimersByTime(20)

    expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('still live')
  })

  it('drops disconnected transport deltas on reconnect ready and remains reusable', () => {
    vi.useFakeTimers()
    const router = createOutputStreamRouter({ batchMs: 20, dashboardMode: true })

    router.route(delta('sid-b', 'stale'), 'sid-a')
    router.disconnect()
    router.route(delta('sid-b', 'late stale'), 'sid-a')
    expect(router.route({ payload: {}, type: 'gateway.ready' }, 'sid-a')).toBe('active')
    vi.advanceTimersByTime(20)
    expect(getOutputStreamsState().streams['sid-b']).toBeUndefined()

    router.route(delta('sid-b', 'fresh'), 'sid-a')
    vi.advanceTimersByTime(20)
    expect(getOutputStreamsState().streams['sid-b']?.preview).toBe('fresh')
  })

  it('ignores all events after disposal', () => {
    const router = createOutputStreamRouter({ dashboardMode: true })

    router.dispose()
    expect(router.route({ payload: { text: 'late' }, session_id: 'sid-b', type: 'message.interim' }, 'sid-a')).toBe(
      'ignored'
    )
    expect(getOutputStreamsState().streams['sid-b']).toBeUndefined()
  })
})

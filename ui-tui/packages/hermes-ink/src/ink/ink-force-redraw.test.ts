import { EventEmitter } from 'events'

import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import Text from './components/Text.js'
import Ink from './ink.js'
import { CURSOR_HOME, ERASE_SCREEN } from './termio/csi.js'
import { enableMouseTrackingFor, ENTER_ALT_SCREEN } from './termio/dec.js'

class FakeTty extends EventEmitter {
  chunks: string[] = []
  columns = 80
  rows = 24
  isTTY = true

  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    cb?.()

    return true
  }
}

const makeInk = () => {
  const stdout = new FakeTty()
  const stdin = new FakeTty()
  const stderr = new FakeTty()
  const onFrame = vi.fn()

  const ink = new Ink({
    exitOnCtrlC: false,
    onFrame,
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  ink.render(React.createElement(Text, null, 'repainted frame'))
  ink.onRender()
  stdout.chunks = []
  onFrame.mockClear()

  return { ink, onFrame, stdout }
}

const settleRender = () => new Promise<void>(resolve => setTimeout(resolve, 25))

describe('Ink.forceRedraw', () => {
  it('keeps the existing main-screen clear and repaint behavior', () => {
    const { ink, stdout } = makeInk()

    ink.forceRedraw()

    const output = stdout.chunks.join('')

    expect(output).toContain(ERASE_SCREEN + CURSOR_HOME)
    expect(output).not.toContain(ENTER_ALT_SCREEN)

    ink.unmount()
  })

  it('re-enters alternate screen once, restores wheel tracking, and repaints', async () => {
    const { ink, onFrame, stdout } = makeInk()

    await settleRender()
    onFrame.mockClear()
    ink.setAltScreenActive(true, 'wheel')
    stdout.chunks = []

    ink.forceRedraw()
    await settleRender()

    const output = stdout.chunks.join('')

    expect(output.split(ENTER_ALT_SCREEN)).toHaveLength(2)
    expect(output).toContain(ERASE_SCREEN + CURSOR_HOME)
    expect(output).toContain(enableMouseTrackingFor('wheel'))
    expect(onFrame).toHaveBeenCalled()

    ink.unmount()
  })
})

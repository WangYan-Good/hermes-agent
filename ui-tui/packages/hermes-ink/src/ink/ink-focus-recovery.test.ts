import { EventEmitter } from 'events'

import React, { useContext, useEffect } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { AlternateScreen } from './components/AlternateScreen.js'
import StdinContext from './components/StdinContext.js'
import Text from './components/Text.js'
import Ink from './ink.js'
import instances from './instances.js'
import { csi, CURSOR_HOME, ERASE_SCREEN, FOCUS_IN, FOCUS_OUT } from './termio/csi.js'
import { enableMouseTrackingFor, ENTER_ALT_SCREEN } from './termio/dec.js'

const DA1_REPLY = csi('?62c')

class FakeStdout extends EventEmitter {
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

class FakeStdin extends EventEmitter {
  isTTY = true
  isRaw = false
  private buffer: string[] = []

  get readableLength(): number {
    return this.buffer.reduce((length, chunk) => length + chunk.length, 0)
  }

  ref(): void {}
  unref(): void {}
  setEncoding(): this {
    return this
  }
  setRawMode(mode: boolean): this {
    this.isRaw = mode

    return this
  }
  read(): string | null {
    return this.buffer.shift() ?? null
  }
  feed(data: string): void {
    this.buffer.push(data)
    this.emit('readable')
  }
}

function RawModeConsumer() {
  const { isRawModeSupported, setRawMode } = useContext(StdinContext)

  useEffect(() => {
    if (!isRawModeSupported) {
      return
    }

    setRawMode(true)

    return () => setRawMode(false)
  }, [isRawModeSupported, setRawMode])

  return React.createElement(Text, null, 'focus recovery frame')
}

const flushAsyncWork = async () => {
  await new Promise<void>(resolve => setImmediate(resolve))
  await new Promise<void>(resolve => setImmediate(resolve))
}

const occurrences = (output: string, sequence: string) => output.split(sequence).length - 1

interface Harness {
  ink: Ink
  stdin: FakeStdin
  stdout: FakeStdout
  cleanup: () => void
}

const activeHarnesses = new Set<Harness>()

async function mountInk(alternateScreen: boolean): Promise<Harness> {
  const stdout = new FakeStdout()
  const stdin = new FakeStdin()
  const stderr = new FakeStdout()

  const ink = new Ink({
    exitOnCtrlC: false,
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  const processStdout = process.stdout
  const previousProcessInk = instances.get(processStdout)

  instances.set(processStdout, ink)
  instances.set(stdout as unknown as NodeJS.WriteStream, ink)

  const child = React.createElement(RawModeConsumer)

  ink.render(
    alternateScreen
      ? React.createElement(AlternateScreen, { mouseTracking: 'wheel' }, child)
      : child
  )
  ink.onRender()
  await flushAsyncWork()

  // Resolve the raw-mode startup terminal query so later assertions contain
  // only output caused by the focus transition under test.
  stdin.feed(DA1_REPLY)
  await flushAsyncWork()

  const harness: Harness = {
    ink,
    stdin,
    stdout,
    cleanup: () => {
      if (!activeHarnesses.delete(harness)) {
        return
      }

      ink.unmount()
      instances.delete(stdout as unknown as NodeJS.WriteStream)

      if (previousProcessInk) {
        instances.set(processStdout, previousProcessInk)
      } else {
        instances.delete(processStdout)
      }
    }
  }

  activeHarnesses.add(harness)

  return harness
}

async function sendFocus(harness: Harness, sequence: string): Promise<void> {
  harness.stdin.feed(sequence)
  await flushAsyncWork()
}

afterEach(() => {
  for (const harness of [...activeHarnesses]) {
    harness.cleanup()
  }
})

describe('Ink terminal focus recovery', () => {
  it('does not re-enter or clear alternate screen on the initial focus-in', async () => {
    const harness = await mountInk(true)
    const mountOutput = harness.stdout.chunks.join('')

    expect(occurrences(mountOutput, ENTER_ALT_SCREEN)).toBe(1)
    harness.stdout.chunks = []

    await sendFocus(harness, FOCUS_IN)

    const output = harness.stdout.chunks.join('')

    expect(occurrences(output, ENTER_ALT_SCREEN)).toBe(0)
    expect(occurrences(output, ERASE_SCREEN)).toBe(0)
  })

  it('does not recover for duplicate focus-in events while already focused', async () => {
    const harness = await mountInk(true)

    harness.stdout.chunks = []
    harness.stdin.feed(FOCUS_IN)
    harness.stdin.feed(FOCUS_IN)
    harness.stdin.feed(FOCUS_IN)
    await flushAsyncWork()

    const output = harness.stdout.chunks.join('')

    expect(occurrences(output, ENTER_ALT_SCREEN)).toBe(0)
    expect(occurrences(output, ERASE_SCREEN)).toBe(0)
  })

  it('re-enters alternate screen once and restores its modes after a real blur and focus', async () => {
    const harness = await mountInk(true)

    await sendFocus(harness, FOCUS_IN)
    harness.stdout.chunks = []

    harness.stdin.feed(FOCUS_OUT)
    await sendFocus(harness, FOCUS_IN)

    const output = harness.stdout.chunks.join('')

    expect(occurrences(output, ENTER_ALT_SCREEN)).toBe(1)
    expect(output).toContain(ERASE_SCREEN + CURSOR_HOME)
    expect(output).toContain(enableMouseTrackingFor('wheel'))
  })

  it('recovers once for every completed blur and focus cycle', async () => {
    const harness = await mountInk(true)

    await sendFocus(harness, FOCUS_IN)
    harness.stdout.chunks = []

    harness.stdin.feed(FOCUS_OUT)
    await sendFocus(harness, FOCUS_IN)
    harness.stdin.feed(FOCUS_OUT)
    await sendFocus(harness, FOCUS_IN)

    expect(occurrences(harness.stdout.chunks.join(''), ENTER_ALT_SCREEN)).toBe(2)
  })

  it('cancels a queued recovery when the terminal blurs again before the microtask', async () => {
    const harness = await mountInk(true)

    await sendFocus(harness, FOCUS_IN)
    harness.stdout.chunks = []

    harness.stdin.feed(FOCUS_OUT)
    harness.stdin.feed(FOCUS_IN)
    harness.stdin.feed(FOCUS_OUT)
    await flushAsyncWork()

    expect(occurrences(harness.stdout.chunks.join(''), ENTER_ALT_SCREEN)).toBe(0)

    harness.stdout.chunks = []
    await sendFocus(harness, FOCUS_IN)

    expect(occurrences(harness.stdout.chunks.join(''), ENTER_ALT_SCREEN)).toBe(1)
  })

  it('guards initial focus on the main screen and preserves blur-focus redraw', async () => {
    const harness = await mountInk(false)

    harness.stdout.chunks = []
    await sendFocus(harness, FOCUS_IN)

    let output = harness.stdout.chunks.join('')

    expect(output).not.toContain(ERASE_SCREEN)
    expect(output).not.toContain(ENTER_ALT_SCREEN)

    harness.stdout.chunks = []
    harness.stdin.feed(FOCUS_OUT)
    await sendFocus(harness, FOCUS_IN)
    output = harness.stdout.chunks.join('')

    expect(output).toContain(ERASE_SCREEN + CURSOR_HOME)
    expect(output).not.toContain(ENTER_ALT_SCREEN)
  })
})

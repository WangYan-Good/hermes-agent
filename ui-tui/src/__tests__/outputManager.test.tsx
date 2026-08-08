import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { resetOutputStreams, setSecondaryOutput, syncOutputSessions } from '../app/outputStreamStore.js'
import { outputFocusDirection, OutputManager } from '../components/outputManager.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const renderToText = (node: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 120, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const app = renderSync(node, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  app.unmount()
  app.cleanup()

  return stripAnsi(output)
}

describe('output manager', () => {
  beforeEach(() => {
    resetOutputStreams()
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'idle', title: 'Beta' },
        { id: 'sid-c', status: 'working', title: 'Gamma' }
      ],
      'sid-a'
    )
    setSecondaryOutput('sid-b')
  })

  it('lists the focused, secondary, and waiting streams', () => {
    const output = renderToText(
      <OutputManager
        onActivate={vi.fn()}
        onClose={vi.fn()}
        onExitSplit={vi.fn()}
        onSetSecondary={vi.fn()}
        t={DEFAULT_THEME}
      />
    )

    expect(output).toContain('Alpha')
    expect(output).toContain('primary')
    expect(output).toContain('Beta')
    expect(output).toContain('secondary')
    expect(output).toContain('Gamma')
    expect(output).toContain('waiting')
  })

  it('maps Alt+arrow to output focus direction only', () => {
    expect(outputFocusDirection({ leftArrow: true, meta: true, rightArrow: false })).toBe(-1)
    expect(outputFocusDirection({ leftArrow: false, meta: true, rightArrow: true })).toBe(1)
    expect(outputFocusDirection({ leftArrow: true, meta: false, rightArrow: false })).toBe(0)
  })
})

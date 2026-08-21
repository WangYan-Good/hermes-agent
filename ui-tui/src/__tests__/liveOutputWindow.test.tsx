import { PassThrough } from 'stream'

import { AlternateScreen, Box, renderSync, Text } from '@hermes/ink'
import React, { useLayoutEffect, useRef } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import type { DOMElement } from '../../packages/hermes-ink/src/ink/dom.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { LiveOutputWindow } from '../components/streamingAssistant.js'

interface Geometry {
  footer: DOMElement | null
  root?: DOMElement | null
  transcript: DOMElement | null
}

function Harness({
  expose,
  generation,
  height = 20
}: {
  expose: React.MutableRefObject<Geometry>
  generation: number
  height?: number
}) {
  const transcript = useRef<DOMElement>(null)
  const footer = useRef<DOMElement>(null)

  useLayoutEffect(() => {
    expose.current = { footer: footer.current, transcript: transcript.current }
  }, [expose, generation])

  return (
    <Box flexDirection="column" height={height} width={80}>
      <Box flexGrow={1} flexShrink={1} minHeight={0} ref={transcript}>
        <Text>SETTLED HISTORY</Text>
      </Box>
      <LiveOutputWindow
        cols={80}
        detailsMode="expanded"
        detailsModeCommandOverride={false}
        progress={{ showProgressArea: true }}
      />
      <Box flexShrink={0} height={1} ref={footer}>
        <Text>COMPOSER</Text>
      </Box>
    </Box>
  )
}

const streams = () => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', () => {})

  return { stderr, stdin, stdout }
}

function FixedDashboardHarness({
  expose,
  generation
}: {
  expose: React.MutableRefObject<Geometry>
  generation: number
}) {
  const root = useRef<DOMElement>(null)
  const transcript = useRef<DOMElement>(null)
  const footer = useRef<DOMElement>(null)

  useLayoutEffect(() => {
    expose.current = { footer: footer.current, root: root.current, transcript: transcript.current }
  }, [expose, generation])

  return (
    <AlternateScreen mouseTracking="wheel">
      <Box flexDirection="column" flexGrow={1} minHeight={0} ref={root} width="100%">
        <Box flexGrow={1} flexShrink={1} minHeight={0} ref={transcript}>
          <Text>SETTLED HISTORY</Text>
        </Box>
        <LiveOutputWindow
          cols={169}
          detailsMode="expanded"
          detailsModeCommandOverride={false}
          progress={{ showProgressArea: true }}
        />
        <Box flexShrink={0} height={1} ref={footer}>
          <Text>COMPOSER</Text>
        </Box>
      </Box>
    </AlternateScreen>
  )
}

const dashboardStreams = () => {
  const io = streams()

  Object.assign(io.stdout, { columns: 169, isTTY: true, rows: 45 })
  Object.assign(io.stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })

  return io
}

const heightOf = (element: DOMElement | null | undefined) => element?.yogaNode?.getComputedHeight()
const topOf = (element: DOMElement | null | undefined) => element?.yogaNode?.getComputedTop()

afterEach(() => resetTurnState())

describe('LiveOutputWindow geometry', () => {
  it('keeps a 169x45 dashboard viewport, live pane, and composer stable while streaming grows', () => {
    const expose = { current: { footer: null, root: null, transcript: null } } as React.MutableRefObject<Geometry>
    const io = dashboardStreams()

    patchTurnState({ streaming: 'short' })

    const app = renderSync(<FixedDashboardHarness expose={expose} generation={0} />, {
      patchConsole: false,
      stderr: io.stderr as NodeJS.WriteStream,
      stdin: io.stdin as NodeJS.ReadStream,
      stdout: io.stdout as NodeJS.WriteStream
    })

    const shortRootHeight = heightOf(expose.current.root)
    const shortTranscriptHeight = heightOf(expose.current.transcript)
    const shortFooterTop = topOf(expose.current.footer)
    const shortLiveHeight = (shortFooterTop ?? 0) - (shortTranscriptHeight ?? 0)

    patchTurnState({ streaming: Array.from({ length: 120 }, (_, index) => `live row ${index}`).join('\n') })
    app.rerender(<FixedDashboardHarness expose={expose} generation={1} />)

    const longRootHeight = heightOf(expose.current.root)
    const longTranscriptHeight = heightOf(expose.current.transcript)
    const longFooterTop = topOf(expose.current.footer)
    const longLiveHeight = (longFooterTop ?? 0) - (longTranscriptHeight ?? 0)

    app.unmount()
    app.cleanup()

    expect(shortRootHeight).toBe(45)
    expect(longRootHeight).toBe(45)
    expect(longLiveHeight).toBe(shortLiveHeight)
    expect(longFooterTop).toBe(shortFooterTop)
    expect(shortLiveHeight).toBeGreaterThan(0)
  })

  it('enters the alternate screen once while normal streaming frames rerender', () => {
    const expose = { current: { footer: null, root: null, transcript: null } } as React.MutableRefObject<Geometry>
    const io = dashboardStreams()
    let output = ''

    io.stdout.on('data', chunk => {
      output += chunk.toString()
    })
    patchTurnState({ streaming: 'first token' })

    const app = renderSync(<FixedDashboardHarness expose={expose} generation={0} />, {
      patchConsole: false,
      stderr: io.stderr as NodeJS.WriteStream,
      stdin: io.stdin as NodeJS.ReadStream,
      stdout: io.stdout as NodeJS.WriteStream
    })

    patchTurnState({ streaming: 'first token second token third token' })
    app.rerender(<FixedDashboardHarness expose={expose} generation={1} />)
    patchTurnState({ streaming: Array.from({ length: 120 }, (_, index) => `row ${index}`).join('\n') })
    app.rerender(<FixedDashboardHarness expose={expose} generation={2} />)

    expect(output.split('\x1b[?1049h')).toHaveLength(2)

    app.unmount()
    app.cleanup()
  })

  it('keeps transcript and composer geometry stable while live output grows', () => {
    const expose = { current: { footer: null, transcript: null } } as React.MutableRefObject<Geometry>
    const io = streams()

    patchTurnState({ streaming: 'short' })

    const app = renderSync(<Harness expose={expose} generation={0} />, {
      patchConsole: false,
      stderr: io.stderr as NodeJS.WriteStream,
      stdin: io.stdin as NodeJS.ReadStream,
      stdout: io.stdout as NodeJS.WriteStream
    })

    const shortTranscriptHeight = expose.current.transcript?.yogaNode?.getComputedHeight()
    const shortFooterTop = expose.current.footer?.yogaNode?.getComputedTop()

    patchTurnState({ streaming: Array.from({ length: 30 }, (_, index) => `live row ${index}`).join('\n') })
    app.rerender(<Harness expose={expose} generation={1} />)

    const longTranscriptHeight = expose.current.transcript?.yogaNode?.getComputedHeight()
    const longFooterTop = expose.current.footer?.yogaNode?.getComputedTop()

    app.unmount()
    app.cleanup()

    expect(shortTranscriptHeight).toBe(longTranscriptHeight)
    expect(shortFooterTop).toBe(longFooterTop)
    expect(longFooterTop).toBe(19)
  })

  it('lets the live window shrink without displacing the composer in a very short viewport', () => {
    const expose = { current: { footer: null, transcript: null } } as React.MutableRefObject<Geometry>
    const io = streams()

    patchTurnState({ streaming: Array.from({ length: 20 }, (_, index) => `row ${index}`).join('\n') })

    const app = renderSync(<Harness expose={expose} generation={0} height={4} />, {
      patchConsole: false,
      stderr: io.stderr as NodeJS.WriteStream,
      stdin: io.stdin as NodeJS.ReadStream,
      stdout: io.stdout as NodeJS.WriteStream
    })

    const transcriptHeight = expose.current.transcript?.yogaNode?.getComputedHeight()
    const footerTop = expose.current.footer?.yogaNode?.getComputedTop()

    app.unmount()
    app.cleanup()

    expect(transcriptHeight).toBeGreaterThanOrEqual(0)
    expect(footerTop).toBe(3)
  })
})

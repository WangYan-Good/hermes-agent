import { PassThrough } from 'stream'

import { Box, renderSync, Text } from '@hermes/ink'
import React, { useLayoutEffect, useRef } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import type { DOMElement } from '../../packages/hermes-ink/src/ink/dom.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { LiveOutputWindow } from '../components/streamingAssistant.js'

interface Geometry {
  footer: DOMElement | null
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

afterEach(() => resetTurnState())

describe('LiveOutputWindow geometry', () => {
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

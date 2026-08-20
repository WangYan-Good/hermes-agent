import { PassThrough } from 'stream'

import { AlternateScreen, Box, renderSync, Text } from '@hermes/ink'
import React, { useLayoutEffect, useRef } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { DOMElement, DOMNode } from '../../packages/hermes-ink/src/ink/dom.js'
import instances from '../../packages/hermes-ink/src/ink/instances.js'
import { GatewayProvider } from '../app/gatewayContext.js'
import type { AppLayoutProps } from '../app/interfaces.js'
import {
  $outputLayout,
  commitOutputPrimaryTransition,
  getOutputStreamsState,
  observeOutputEvent,
  resetOutputStreams,
  resolveOutputConflict,
  setSecondaryOutput,
  syncOutputSessions
} from '../app/outputStreamStore.js'
import {
  $isBlocked,
  getOverlayState,
  outputConflictBlocksComposer,
  patchOverlayState,
  resetOverlayState
} from '../app/overlayStore.js'
import { patchTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { decideOutputConflictWithActivation } from '../app/useMainApp.js'
import { AppLayout, appScreenMode, petSpacerAllocation } from '../components/appLayout.js'
import { PromptZone } from '../components/appOverlays.js'
import { outputConflictAction, OutputConflictPrompt } from '../components/outputConflictPrompt.js'
import { outputPaneMode, outputPaneWidths, ReadonlyOutputPane, SplitOutputPane } from '../components/splitOutputPane.js'
import { LiveOutputWindow } from '../components/streamingAssistant.js'
import type * as EnvModule from '../config/env.js'
import type { GatewayClient } from '../gatewayClient.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

// Exercise the defensive production selector: Dashboard must still mount
// AlternateScreen when a surrounding terminal config requests inline mode.
vi.mock('../config/env.js', async importActual => {
  const actual = await importActual<typeof EnvModule>()

  return { ...actual, INLINE_MODE: true }
})

// Keep the renderer and its instance registry on the same source module graph
// so the integration test can inspect real Yoga geometry without adding a
// test-only ref or API to production AppLayout.
vi.mock('@hermes/ink', async () => import('../../packages/hermes-ink/src/entry-exports.js'))

const renderToText = (node: React.ReactElement) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 120, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
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

interface SplitGeometry {
  composer: DOMElement | null
  primary: DOMElement | null
  root: DOMElement | null
}

const nodeText = (node: DOMNode): string =>
  node.nodeName === '#text' ? node.nodeValue : node.childNodes.map(child => nodeText(child)).join('')

const findTextNode = (root: DOMElement | null, text: string): DOMNode | null => {
  if (!root) {
    return null
  }

  for (const child of root.childNodes) {
    if (child.nodeName === '#text' && child.nodeValue.includes(text)) {
      return child
    }

    if (child.nodeName !== '#text') {
      const found = findTextNode(child, text)

      if (found) {
        return found
      }
    }
  }

  return null
}

const findElement = (root: DOMElement | null, predicate: (node: DOMElement) => boolean): DOMElement | null => {
  if (!root) {
    return null
  }

  if (predicate(root)) {
    return root
  }

  for (const child of root.childNodes) {
    if (child.nodeName !== '#text') {
      const found = findElement(child, predicate)

      if (found) {
        return found
      }
    }
  }

  return null
}

const paneAncestor = (node: DOMNode | null): DOMElement | null => {
  let current = node?.parentNode

  while (current) {
    if (current.style.flexGrow === 1 && current.style.flexShrink === 1 && current.style.width != null) {
      return current
    }

    current = current.parentNode
  }

  return null
}

const paneAncestorWithWidth = (node: DOMNode | null, width: number): DOMElement | null => {
  let current = node?.parentNode

  while (current) {
    if (current.style.flexGrow === 1 && current.style.width === width) {
      return current
    }

    current = current.parentNode
  }

  return null
}

interface NodeGeometry {
  bottom: number
  height: number
  top: number
}

const geometryWithin = (node: DOMElement, ancestor: DOMElement): NodeGeometry => {
  let current: DOMElement | undefined = node
  let top = 0

  while (current !== ancestor) {
    top += current.yogaNode?.getComputedTop() ?? 0
    current = current.parentNode

    if (!current) {
      throw new Error('geometry node is not inside the expected production root')
    }
  }

  const height = node.yogaNode?.getComputedHeight() ?? 0

  return { bottom: top + height, height, top }
}

function FixedSplitHarness({
  expose,
  generation
}: {
  expose: React.MutableRefObject<SplitGeometry>
  generation: number
}) {
  const root = useRef<DOMElement>(null)
  const primary = useRef<DOMElement>(null)
  const composer = useRef<DOMElement>(null)

  useLayoutEffect(() => {
    expose.current = { composer: composer.current, primary: primary.current, root: root.current }
  }, [expose, generation])

  return (
    <AlternateScreen mouseTracking="wheel">
      <Box flexDirection="column" flexGrow={1} minHeight={0} ref={root} width="100%">
        <SplitOutputPane
          cols={169}
          onFocusSession={() => {}}
          renderPrimary={width => (
            <Box flexDirection="column" flexGrow={1} flexShrink={1} minHeight={0} ref={primary} width={width}>
              <Box flexGrow={1} flexShrink={1} minHeight={0}>
                <Text>PRIMARY VIEWPORT</Text>
              </Box>
              <LiveOutputWindow
                cols={width}
                detailsMode="expanded"
                detailsModeCommandOverride={false}
                progress={{ showProgressArea: true }}
              />
            </Box>
          )}
        />
        <Box flexShrink={0} height={1} ref={composer}>
          <Text>COMPOSER</Text>
        </Box>
      </Box>
    </AlternateScreen>
  )
}

const dashboardIo = () => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 169, isTTY: true, rows: 45 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', () => {})

  return { stderr, stdin, stdout }
}

const seedThreeStreamsAndSplit = () => {
  syncOutputSessions(
    [
      { id: 'sid-a', status: 'working', title: 'Alpha' },
      { id: 'sid-b', status: 'working', title: 'Beta' },
      { id: 'sid-c', status: 'working', title: 'Gamma' }
    ],
    'sid-a'
  )
  observeOutputEvent({ payload: { text: 'SECONDARY BODY' }, type: 'message.interim' }, 'sid-b', {
    buffer: true,
    now: 1
  })
  observeOutputEvent({ payload: { text: 'WAITING BODY' }, type: 'message.interim' }, 'sid-c', {
    buffer: true,
    now: 2
  })
  setSecondaryOutput('sid-b')
}

beforeEach(() => {
  resetOutputStreams()
  resetOverlayState()
  resetTurnState()
  resetUiState()
})

const appLayoutProps: AppLayoutProps = {
  actions: {
    activateLiveSession: async () => true,
    answerApproval: () => {},
    answerClarify: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    closeLiveSession: async () => null,
    decideOutputConflict: () => {},
    focusOutputSession: async () => true,
    newLiveSession: () => {},
    newPromptSession: () => {},
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {}
  },
  composer: {
    cols: 120,
    compIdx: 0,
    completions: [],
    empty: true,
    handleTextPaste: () => null,
    input: '',
    inputBuf: [],
    pagerPageSize: 10,
    queueEditIdx: null,
    queuedDisplay: [],
    submit: () => {},
    updateInput: () => {},
    voiceRecordKey: DEFAULT_VOICE_RECORD_KEY
  },
  mouseTracking: 'off',
  progress: { showProgressArea: false },
  status: {
    cwdLabel: '~/repo',
    goodVibesTick: 0,
    lastTurnEndedAt: null,
    sessionStartedAt: null,
    showStickyPrompt: false,
    statusColor: DEFAULT_THEME.color.ok,
    stickyPrompt: '',
    turnStartedAt: null,
    voiceLabel: ''
  },
  transcript: {
    historyItems: [{ role: 'user', text: 'PRIMARY TRANSCRIPT' }],
    scrollRef: { current: null },
    virtualHistory: { bottomSpacer: 0, end: 1, measureRef: () => () => {}, offsets: [], start: 0, topSpacer: 0 },
    virtualRows: [{ index: 0, key: 'primary', msg: { role: 'user', text: 'PRIMARY TRANSCRIPT' } }]
  }
}

describe('dashboard output pane layout', () => {
  it('keeps dashboard output on a fixed alternate-screen viewport even when inline mode is requested', () => {
    expect(appScreenMode(true, true)).toBe('alternate')
    expect(appScreenMode(true, false)).toBe('inline')
    expect(appScreenMode(false, true)).toBe('alternate')
    expect(appScreenMode(false, false)).toBe('alternate')
  })

  it('reserves narrow pet rows in whichever dashboard window is visible at the bottom', () => {
    expect(petSpacerAllocation(4, true, false)).toEqual({ live: 0, transcript: 4 })
    expect(petSpacerAllocation(4, true, true)).toEqual({ live: 4, transcript: 0 })
    expect(petSpacerAllocation(4, false, false)).toEqual({ live: 0, transcript: 4 })
  })

  it('uses tabs below 110 columns and two panes at 110 columns', () => {
    expect(outputPaneMode(109)).toBe('tabs')
    expect(outputPaneMode(110)).toBe('split')
    expect(outputPaneWidths(110)).toEqual({ primary: 54, secondary: 55 })
  })

  it('keeps both split panes and the composer inside a 169x45 viewport as streaming grows', () => {
    seedThreeStreamsAndSplit()
    patchTurnState({ streaming: 'short' })

    const expose = {
      current: { composer: null, primary: null, root: null }
    } as React.MutableRefObject<SplitGeometry>

    const io = dashboardIo()

    const app = renderSync(<FixedSplitHarness expose={expose} generation={0} />, {
      patchConsole: false,
      stderr: io.stderr as NodeJS.WriteStream,
      stdin: io.stdin as NodeJS.ReadStream,
      stdout: io.stdout as NodeJS.WriteStream
    })

    const shortComposerTop = expose.current.composer?.yogaNode?.getComputedTop()

    patchTurnState({ streaming: Array.from({ length: 120 }, (_, index) => `stream row ${index}`).join('\n') })
    app.rerender(<FixedSplitHarness expose={expose} generation={1} />)

    const rootHeight = expose.current.root?.yogaNode?.getComputedHeight() ?? 0
    const primaryTop = expose.current.primary?.yogaNode?.getComputedTop() ?? 0
    const primaryHeight = expose.current.primary?.yogaNode?.getComputedHeight() ?? 0
    const composerTop = expose.current.composer?.yogaNode?.getComputedTop() ?? 0
    const renderedText = nodeText(expose.current.root!)
    const secondary = paneAncestor(findTextNode(expose.current.root, 'Beta'))
    const secondaryTop = secondary?.yogaNode?.getComputedTop() ?? 0
    const secondaryHeight = secondary?.yogaNode?.getComputedHeight() ?? 0

    app.unmount()
    app.cleanup()

    expect(renderedText).toContain('PRIMARY VIEWPORT')
    expect(rootHeight).toBe(45)
    expect(composerTop).toBe(shortComposerTop)
    expect(composerTop).toBeLessThan(45)
    expect(primaryTop + primaryHeight).toBeLessThanOrEqual(45)
    expect(secondary).not.toBeNull()
    expect(secondaryTop + secondaryHeight).toBeLessThanOrEqual(45)
  })

  it('keeps production AppLayout split, tabs, and restored split inside one fixed viewport', async () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' }
      ],
      'sid-a'
    )
    observeOutputEvent({ payload: { text: 'SECONDARY BODY' }, type: 'message.interim' }, 'sid-b', {
      buffer: true,
      now: 1
    })
    setSecondaryOutput('sid-b')
    patchTurnState({ streaming: 'short' })
    patchUiState({ mouseTracking: 'wheel', sessionTitle: 'Alpha', sid: 'sid-a', status: 'working' })

    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'sid-a',
      secondarySessionId: 'sid-b'
    })

    const io = dashboardIo()
    let output = ''

    io.stdout.on('data', chunk => {
      output += chunk.toString()
    })

    const gateway = {
      gw: { request: async () => null, send: () => {} } as unknown as GatewayClient,
      rpc: async () => null
    }

    const layout = (generation: number, cols = 169) => (
      <GatewayProvider value={gateway}>
        <AppLayout
          {...appLayoutProps}
          composer={{ ...appLayoutProps.composer, cols, compIdx: generation }}
          dashboardMode
          mouseTracking="wheel"
          progress={{ showProgressArea: true }}
        />
      </GatewayProvider>
    )

    const app = renderSync(layout(0), {
      patchConsole: false,
      stderr: io.stderr as NodeJS.WriteStream,
      stdin: io.stdin as NodeJS.ReadStream,
      stdout: io.stdout as NodeJS.WriteStream
    })

    const ink = instances.get(io.stdout as NodeJS.WriteStream) as unknown as { rootNode: DOMElement }
    const screenRoot = ink.rootNode.childNodes[0] as DOMElement
    const productionRoot = screenRoot.childNodes[0] as DOMElement
    const shortScreenHeight = screenRoot.yogaNode?.getComputedHeight()
    const shortProductionHeight = productionRoot.yogaNode?.getComputedHeight()
    const widths = outputPaneWidths(169)

    const shortPrimary = paneAncestorWithWidth(
      findElement(productionRoot, node => node.nodeName === 'ink-text' && nodeText(node).includes('Alpha · focused')),
      widths.primary
    )

    const shortSecondary = paneAncestorWithWidth(
      findElement(productionRoot, node => node.nodeName === 'ink-text' && nodeText(node).includes('Beta · read-only')),
      widths.secondary
    )

    const shortComposer = findElement(
      productionRoot,
      node => node.parentNode === productionRoot && node.style.noSelect === 'from-left-edge'
    )

    const initialEnterCount = output.split('\x1b[?1049h').length - 1

    expect(shortPrimary).not.toBeNull()
    expect(shortSecondary).not.toBeNull()
    expect(shortComposer).not.toBeNull()

    const shortPrimaryGeometry = geometryWithin(shortPrimary!, screenRoot)
    const shortSecondaryGeometry = geometryWithin(shortSecondary!, screenRoot)
    const shortComposerGeometry = geometryWithin(shortComposer!, screenRoot)

    patchTurnState({ streaming: Array.from({ length: 140 }, (_, index) => `production row ${index}`).join('\n') })
    app.rerender(layout(1))

    const longScreenHeight = screenRoot.yogaNode?.getComputedHeight()
    const longProductionHeight = productionRoot.yogaNode?.getComputedHeight()

    const longPrimary = paneAncestorWithWidth(
      findElement(productionRoot, node => node.nodeName === 'ink-text' && nodeText(node).includes('Alpha · focused')),
      widths.primary
    )

    const longSecondary = paneAncestorWithWidth(
      findElement(productionRoot, node => node.nodeName === 'ink-text' && nodeText(node).includes('Beta · read-only')),
      widths.secondary
    )

    const longComposer = findElement(
      productionRoot,
      node => node.parentNode === productionRoot && node.style.noSelect === 'from-left-edge'
    )

    const finalEnterCount = output.split('\x1b[?1049h').length - 1

    expect(longPrimary).not.toBeNull()
    expect(longSecondary).not.toBeNull()
    expect(longComposer).not.toBeNull()

    const longPrimaryGeometry = geometryWithin(longPrimary!, screenRoot)
    const longSecondaryGeometry = geometryWithin(longSecondary!, screenRoot)
    const longComposerGeometry = geometryWithin(longComposer!, screenRoot)

    const enterCountBeforeResize = output.split('\x1b[?1049h').length - 1
    const exitCountBeforeResize = output.split('\x1b[?1049l').length - 1
    const composerTopBeforeResize = longComposerGeometry.top

    expect(getUiState().mouseTracking).toBe('wheel')

    Object.assign(io.stdout, { columns: 109 })
    io.stdout.emit('resize')
    app.rerender(layout(2, 109))
    await Promise.resolve()

    const narrowText = nodeText(productionRoot)

    const narrowComposer = findElement(
      productionRoot,
      node => node.parentNode === productionRoot && node.style.noSelect === 'from-left-edge'
    )

    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'sid-a',
      secondarySessionId: 'sid-b'
    })
    expect(narrowText).toContain('Alpha · focused')
    expect(narrowText).toContain('Beta · read-only')
    expect(narrowText).not.toContain('SECONDARY BODY')
    expect(screenRoot.yogaNode?.getComputedHeight()).toBe(45)
    expect(productionRoot.yogaNode?.getComputedHeight()).toBe(45)
    expect(narrowComposer).not.toBeNull()

    const narrowComposerGeometry = geometryWithin(narrowComposer!, screenRoot)

    expect(narrowComposerGeometry.bottom).toBeLessThanOrEqual(45)
    expect(getUiState().mouseTracking).toBe('wheel')

    Object.assign(io.stdout, { columns: 169 })
    io.stdout.emit('resize')
    app.rerender(layout(3, 169))
    await Promise.resolve()

    const restoredText = nodeText(productionRoot)

    const restoredPrimary = paneAncestorWithWidth(
      findElement(productionRoot, node => node.nodeName === 'ink-text' && nodeText(node).includes('Alpha · focused')),
      widths.primary
    )

    const restoredSecondary = paneAncestorWithWidth(
      findElement(productionRoot, node => node.nodeName === 'ink-text' && nodeText(node).includes('Beta · read-only')),
      widths.secondary
    )

    const restoredComposer = findElement(
      productionRoot,
      node => node.parentNode === productionRoot && node.style.noSelect === 'from-left-edge'
    )

    expect(restoredText).toContain('SECONDARY BODY')
    expect(restoredPrimary).not.toBeNull()
    expect(restoredSecondary).not.toBeNull()
    expect(restoredComposer).not.toBeNull()
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'split',
      primarySessionId: 'sid-a',
      secondarySessionId: 'sid-b'
    })

    const restoredPrimaryGeometry = geometryWithin(restoredPrimary!, screenRoot)
    const restoredSecondaryGeometry = geometryWithin(restoredSecondary!, screenRoot)
    const restoredComposerGeometry = geometryWithin(restoredComposer!, screenRoot)
    const restoredScreenHeight = screenRoot.yogaNode?.getComputedHeight()
    const restoredProductionHeight = productionRoot.yogaNode?.getComputedHeight()
    const enterCountAfterResize = output.split('\x1b[?1049h').length - 1
    const exitCountAfterResize = output.split('\x1b[?1049l').length - 1

    app.unmount()
    app.cleanup()

    expect(initialEnterCount).toBe(1)
    expect(finalEnterCount).toBe(initialEnterCount)
    expect(shortScreenHeight).toBe(45)
    expect(longScreenHeight).toBe(45)
    expect(shortProductionHeight).toBe(45)
    expect(longProductionHeight).toBe(45)
    expect(shortPrimaryGeometry.top).toBeGreaterThanOrEqual(0)
    expect(shortPrimaryGeometry.bottom).toBeLessThanOrEqual(45)
    expect(longPrimaryGeometry.top).toBeGreaterThanOrEqual(0)
    expect(longPrimaryGeometry.bottom).toBeLessThanOrEqual(45)
    expect(shortSecondaryGeometry.top).toBeGreaterThanOrEqual(0)
    expect(shortSecondaryGeometry.bottom).toBeLessThanOrEqual(45)
    expect(longSecondaryGeometry.top).toBeGreaterThanOrEqual(0)
    expect(longSecondaryGeometry.bottom).toBeLessThanOrEqual(45)
    expect(shortComposerGeometry.top).toBeGreaterThanOrEqual(0)
    expect(shortComposerGeometry.bottom).toBeLessThanOrEqual(45)
    expect(longComposerGeometry.top).toBe(shortComposerGeometry.top)
    expect(longComposerGeometry.height).toBe(shortComposerGeometry.height)
    expect(longComposerGeometry.bottom).toBeLessThanOrEqual(45)
    expect(restoredPrimaryGeometry.bottom).toBeLessThanOrEqual(45)
    expect(restoredSecondaryGeometry.bottom).toBeLessThanOrEqual(45)
    expect(restoredComposerGeometry.bottom).toBeLessThanOrEqual(45)
    expect(restoredComposerGeometry.top).toBe(composerTopBeforeResize)
    expect(restoredScreenHeight).toBe(45)
    expect(restoredProductionHeight).toBe(45)
    expect(enterCountAfterResize).toBe(enterCountBeforeResize)
    expect(exitCountAfterResize).toBe(exitCountBeforeResize)
    expect(getUiState().mouseTracking).toBe('wheel')
  })

  it('renders one readonly secondary and leaves the third output stream waiting', () => {
    seedThreeStreamsAndSplit()

    const output = renderToText(
      <SplitOutputPane cols={120} onFocusSession={vi.fn()} renderPrimary={() => <Text>PRIMARY</Text>} />
    )

    expect(output).toContain('PRIMARY')
    expect(output).toContain('Beta · read-only')
    expect(output).toContain('SECONDARY BODY')
    expect(output).toContain('waiting: Gamma')
  })

  it('renders a bounded readonly tail while preserving older entries in the store', () => {
    seedThreeStreamsAndSplit()

    for (let index = 0; index < 80; index += 1) {
      observeOutputEvent(
        { payload: { text: `TAIL-ROW-${String(index).padStart(3, '0')}` }, type: 'message.interim' },
        'sid-b',
        {
          buffer: true,
          now: 10 + index
        }
      )
    }

    const stream = getOutputStreamsState().streams['sid-b']!

    const output = renderToText(<ReadonlyOutputPane onFocus={vi.fn()} stream={stream} t={DEFAULT_THEME} width={60} />)

    const renderedRows = new Set(output.match(/TAIL-ROW-\d+/g) ?? [])

    expect(renderedRows.size).toBeLessThanOrEqual(40)
    expect(output).toContain('TAIL-ROW-079')
    expect(output).not.toContain('TAIL-ROW-000')
    expect(stream.entries.some(entry => entry.text === 'TAIL-ROW-000')).toBe(true)
  })

  it('clips a readonly output window to its pane and keeps the newest rows visible', () => {
    seedThreeStreamsAndSplit()

    for (let index = 0; index < 80; index += 1) {
      observeOutputEvent(
        { payload: { text: `CLIPPED-ROW-${String(index).padStart(3, '0')}` }, type: 'message.interim' },
        'sid-b',
        {
          buffer: true,
          now: 10 + index
        }
      )
    }

    const stream = getOutputStreamsState().streams['sid-b']!

    const output = renderToText(
      <Box flexDirection="column" height={8}>
        <ReadonlyOutputPane onFocus={vi.fn()} stream={stream} t={DEFAULT_THEME} width={60} />
      </Box>
    )

    expect(output).toContain('CLIPPED-ROW-079')
    expect(output).not.toContain('CLIPPED-ROW-050')
  })

  it('renders tab labels but only the focused transcript below the threshold', () => {
    seedThreeStreamsAndSplit()

    const output = renderToText(
      <SplitOutputPane cols={109} onFocusSession={vi.fn()} renderPrimary={() => <Text>PRIMARY BODY</Text>} />
    )

    expect(output).toContain('Alpha · focused')
    expect(output).toContain('Beta · read-only')
    expect(output).toContain('PRIMARY BODY')
    expect(output).not.toContain('SECONDARY BODY')
  })

  it('keeps completed primary output visible and calls out another running pane', () => {
    seedThreeStreamsAndSplit()
    observeOutputEvent({ payload: { status: 'complete', text: 'done' }, type: 'message.complete' }, 'sid-a', {
      buffer: false,
      now: 3
    })
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'idle', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' },
        { id: 'sid-c', status: 'working', title: 'Gamma' }
      ],
      'sid-a'
    )

    const output = renderToText(
      <SplitOutputPane cols={120} onFocusSession={vi.fn()} renderPrimary={() => <Text>FINAL RESULT</Text>} />
    )

    expect(output).toContain('FINAL RESULT')
    expect(output).toContain('still running: Beta')
    expect(getOutputStreamsState().layout.primarySessionId).toBe('sid-a')
  })

  it('shows running and waiting output after keeping a completed primary in single mode', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' }
      ],
      'sid-a'
    )
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B is live' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    resolveOutputConflict('keep-primary')
    observeOutputEvent({ payload: { text: 'A done' }, type: 'message.complete' }, 'sid-a', {
      buffer: false,
      now: 3
    })

    const output = renderToText(
      <SplitOutputPane cols={120} onFocusSession={vi.fn()} renderPrimary={() => <Text>FINAL PRIMARY</Text>} />
    )

    expect(output).toContain('FINAL PRIMARY')
    expect(output).toContain('still running: Beta')
    expect(output).toContain('waiting: Beta')
    expect(getOutputStreamsState().layout.primarySessionId).toBe('sid-a')
  })

  it('composes the existing transcript with one readonly pane in AppLayout', () => {
    seedThreeStreamsAndSplit()
    patchUiState({ sessionTitle: 'Alpha', sid: 'sid-a', status: 'working' })

    const gateway = {
      gw: { request: async () => null, send: () => {} } as unknown as GatewayClient,
      rpc: async () => null
    }

    const output = renderToText(
      <GatewayProvider value={gateway}>
        <AppLayout {...appLayoutProps} dashboardMode />
      </GatewayProvider>
    )

    expect(output).toContain('PRIMARY TRANSCRIPT')
    expect(output).toContain('Beta · read-only')
    expect(output).toContain('SECONDARY BODY')
  })

  it('isolates dashboard streaming updates in a labelled live-output window', () => {
    syncOutputSessions([{ id: 'sid-a', status: 'working', title: 'Alpha' }], 'sid-a')
    patchTurnState({ streaming: 'LIVE CHINESE 输出进度 6%' })
    patchUiState({ sessionTitle: 'Alpha', sid: 'sid-a', status: 'working' })

    const gateway = {
      gw: { request: async () => null, send: () => {} } as unknown as GatewayClient,
      rpc: async () => null
    }

    const output = renderToText(
      <GatewayProvider value={gateway}>
        <AppLayout {...appLayoutProps} dashboardMode />
      </GatewayProvider>
    )

    expect(output).toContain('PRIMARY TRANSCRIPT')
    expect(output).toContain('Live output')
    expect(output).toContain('LIVE CHINESE 输出进度 6%')
  })

  it('keeps standalone AppLayout on the original single transcript surface', () => {
    seedThreeStreamsAndSplit()
    patchUiState({ sessionTitle: 'Alpha', sid: 'sid-a', status: 'working' })

    const gateway = {
      gw: { request: async () => null, send: () => {} } as unknown as GatewayClient,
      rpc: async () => null
    }

    const output = renderToText(
      <GatewayProvider value={gateway}>
        <AppLayout {...appLayoutProps} dashboardMode={false} />
      </GatewayProvider>
    )

    expect(output).toContain('PRIMARY TRANSCRIPT')
    expect(output).not.toContain('Beta · read-only')
    expect(output).not.toContain('SECONDARY BODY')
    expect($isBlocked.get()).toBe(false)
    expect(outputConflictBlocksComposer(getOutputStreamsState().conflict, true)).toBe(true)
  })
})

describe('output conflict prompt', () => {
  const conflict = { candidateSessionId: 'sid-b', episode: 1, primarySessionId: 'sid-a' }

  it('supports arrows, number keys, enter, and escape without a second input surface', () => {
    expect(outputConflictAction('', { downArrow: true }, 0)).toEqual({ delta: 1, kind: 'move' })
    expect(outputConflictAction('', { return: true }, 2)).toEqual({ decision: 'split', kind: 'choose' })
    expect(outputConflictAction('2', {}, 0)).toEqual({ decision: 'prioritize-candidate', kind: 'choose' })
    expect(outputConflictAction('', { escape: true }, 2)).toEqual({ decision: 'keep-primary', kind: 'choose' })
  })

  it('renders clearly labelled current, new, split, and other decisions', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', title: 'Alpha' },
        { id: 'sid-b', title: 'Beta' }
      ],
      'sid-a'
    )

    const output = renderToText(<OutputConflictPrompt conflict={conflict} onDecision={vi.fn()} t={DEFAULT_THEME} />)

    expect(output).toContain('concurrent output')
    expect(output).toContain('Current · Alpha')
    expect(output).toContain('New output · Beta')
    expect(output).toContain('Split')
    expect(output).toContain('Other…')
  })

  it('keeps a global secret prompt above the output conflict prompt', () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    patchOverlayState({
      secret: {
        envVar: 'TOKEN',
        prompt: 'Enter token',
        requestId: 'secret-b',
        sessionId: 'sid-b',
        sessionTitle: 'Beta'
      }
    })

    const output = renderToText(
      <PromptZone
        cols={120}
        onApprovalChoice={vi.fn()}
        onClarifyAnswer={vi.fn()}
        onOutputConflictDecision={vi.fn()}
        onSecretSubmit={vi.fn()}
        onSudoSubmit={vi.fn()}
        showOutputConflict
      />
    )

    expect(output).toContain('Enter token')
    expect(output).toContain('from: Beta')
    expect(output).not.toContain('concurrent output')
  })

  it('opens the manager and resolves the conflict without activating or changing the layout', async () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    const before = structuredClone(getOutputStreamsState())
    const activate = vi.fn().mockResolvedValue(true)

    const ok = await decideOutputConflictWithActivation({
      activate,
      conflict: before.conflict!,
      decision: 'open-manager'
    })

    const after = getOutputStreamsState()

    expect(ok).toBe(true)
    expect(activate).not.toHaveBeenCalled()
    expect(after.conflict).toBeNull()
    expect(after.layout).toEqual(before.layout)
    expect(getOverlayState().outputs).toBe(true)
  })

  it('does not change layout or dismiss the conflict when candidate activation fails', async () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    const before = getOutputStreamsState()

    const ok = await decideOutputConflictWithActivation({
      activate: vi.fn().mockResolvedValue(false),
      conflict: before.conflict!,
      decision: 'prioritize-candidate'
    })

    expect(ok).toBe(false)
    expect(getOutputStreamsState().layout).toEqual(before.layout)
    expect(getOutputStreamsState().conflict).toEqual(before.conflict)
  })

  it('leaves split layout, focus, and episode unchanged when primary activation fails', async () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    const before = structuredClone(getOutputStreamsState())
    const activated: string[] = []

    const ok = await decideOutputConflictWithActivation({
      activate: async sessionId => {
        activated.push(sessionId)

        return false
      },
      conflict: before.conflict!,
      decision: 'split'
    })

    expect(ok).toBe(false)
    expect(activated).toEqual(['sid-a'])
    expect(getOutputStreamsState()).toEqual(before)
  })

  it('leaves split layout, focus, and episode unchanged when primary activation rejects', async () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    const before = structuredClone(getOutputStreamsState())

    const ok = await decideOutputConflictWithActivation({
      activate: async () => {
        throw new Error('activation failed')
      },
      conflict: before.conflict!,
      decision: 'split'
    })

    expect(ok).toBe(false)
    expect(getOutputStreamsState()).toEqual(before)
  })

  it('commits split once only after primary activation succeeds', async () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    const before = structuredClone(getOutputStreamsState())
    const activated: string[] = []
    const layouts: unknown[] = []

    const unsubscribe = $outputLayout.subscribe(layout => {
      layouts.push(layout)
    })

    const ok = await decideOutputConflictWithActivation({
      activate: async sessionId => {
        activated.push(sessionId)

        return true
      },
      conflict: before.conflict!,
      decision: 'split'
    })

    const after = getOutputStreamsState()
    unsubscribe()

    expect(ok).toBe(true)
    expect(activated).toEqual(['sid-a'])
    expect(after.layout).toEqual({
      mode: 'split',
      primarySessionId: 'sid-a',
      secondarySessionId: 'sid-b'
    })
    expect(after.conflict).toBeNull()
    expect(after.conflictHandled).toBe(true)
    expect(after.episode).toBe(before.episode)
    expect(layouts).toEqual([before.layout, after.layout])
  })

  it('commits candidate priority only after a successful activation', async () => {
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'B' }, type: 'message.delta' }, 'sid-b', { buffer: true, now: 2 })
    const conflict = getOutputStreamsState().conflict!

    const ok = await decideOutputConflictWithActivation({
      activate: async sessionId => {
        commitOutputPrimaryTransition({
          kind: 'activate-live',
          nextSessionId: sessionId,
          previousSessionId: 'sid-a'
        })

        return true
      },
      conflict,
      decision: 'prioritize-candidate'
    })

    expect(ok).toBe(true)
    expect(getOutputStreamsState().layout).toEqual({
      mode: 'single',
      primarySessionId: 'sid-b',
      secondarySessionId: null
    })
    expect(getOutputStreamsState().conflict).toBeNull()
  })
})

import { PassThrough } from 'stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

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
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { decideOutputConflictWithActivation } from '../app/useMainApp.js'
import { AppLayout } from '../components/appLayout.js'
import { PromptZone } from '../components/appOverlays.js'
import { outputConflictAction, OutputConflictPrompt } from '../components/outputConflictPrompt.js'
import { outputPaneMode, outputPaneWidths, ReadonlyOutputPane, SplitOutputPane } from '../components/splitOutputPane.js'
import type { GatewayClient } from '../gatewayClient.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

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
  it('uses tabs below 110 columns and two panes at 110 columns', () => {
    expect(outputPaneMode(109)).toBe('tabs')
    expect(outputPaneMode(110)).toBe('split')
    expect(outputPaneWidths(110)).toEqual({ primary: 54, secondary: 55 })
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
    observeOutputEvent({ payload: { text: 'done' }, type: 'message.complete' }, 'sid-a', {
      buffer: false,
      now: 3
    })

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

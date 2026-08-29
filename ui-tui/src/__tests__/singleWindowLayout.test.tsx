import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { GatewayProvider } from '../app/gatewayContext.js'
import type { AppLayoutProps } from '../app/interfaces.js'
import { observeOutputEvent, resetOutputStreams, syncOutputSessions } from '../app/outputStreamStore.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { AppLayout } from '../components/appLayout.js'
import type * as EnvModule from '../config/env.js'
import type { GatewayClient } from '../gatewayClient.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

vi.mock('../config/env.js', async importActual => {
  const actual = await importActual<typeof EnvModule>()

  return { ...actual, INLINE_MODE: true }
})

vi.mock('@hermes/ink', async () => import('../../packages/hermes-ink/src/entry-exports.js'))

const props: AppLayoutProps = {
  actions: {
    activateLiveSession: async () => true,
    answerApproval: () => {},
    answerClarify: () => {},
    answerSecret: () => {},
    answerSudo: () => {},
    clearSelection: () => {},
    closeLiveSession: async () => null,
    newLiveSession: () => {},
    newPromptSession: () => {},
    onModelSelect: () => {},
    resumeById: () => {},
    setStickyPrompt: () => {}
  },
  composer: {
    cols: 160,
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
  dashboardMode: true,
  mouseTracking: 'off',
  progress: { showProgressArea: false },
  status: {
    cwdLabel: '~/repo',
    goodVibesTick: 0,
    lastTurnEndedAt: null,
    sessionStartedAt: null,
    sessionTitle: 'Alpha',
    showStickyPrompt: false,
    statusColor: DEFAULT_THEME.color.ok,
    stickyPrompt: '',
    turnStartedAt: null,
    voiceLabel: ''
  },
  transcript: {
    historyItems: [{ role: 'user', text: 'ACTIVE TRANSCRIPT' }],
    scrollRef: { current: null },
    virtualHistory: { bottomSpacer: 0, end: 1, measureRef: () => () => {}, offsets: [], start: 0, topSpacer: 0 },
    virtualRows: [{ index: 0, key: 'active', msg: { role: 'user', text: 'ACTIVE TRANSCRIPT' } }]
  }
}

const renderDashboard = () => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 160, isTTY: true, rows: 40 })
  Object.assign(stdin, { isTTY: true, ref: () => {}, setRawMode: () => {}, unref: () => {} })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const gateway = {
    gw: { request: async () => null, send: () => {} } as unknown as GatewayClient,
    rpc: async () => null
  }

  const app = renderSync(
    <GatewayProvider value={gateway}>
      <AppLayout {...props} />
    </GatewayProvider>,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  app.unmount()
  app.cleanup()

  return stripAnsi(output)
}

beforeEach(() => {
  resetOutputStreams()
  resetOverlayState()
  resetTurnState()
  resetUiState()
  patchUiState({ sessionTitle: 'Alpha', sid: 'sid-a', status: 'working' })
})

describe('single-window dashboard layout', () => {
  it('does not show a concurrent-output window when another session streams', () => {
    syncOutputSessions(
      [
        { id: 'sid-a', status: 'working', title: 'Alpha' },
        { id: 'sid-b', status: 'working', title: 'Beta' }
      ],
      'sid-a'
    )
    observeOutputEvent({ type: 'message.start' }, 'sid-a', { buffer: false, now: 1 })
    observeOutputEvent({ payload: { text: 'BACKGROUND OUTPUT' }, type: 'message.delta' }, 'sid-b', {
      buffer: true,
      now: 2
    })

    const output = renderDashboard()

    expect(output).toContain('ACTIVETRANSCRIPT')
    expect(output).not.toContain('concurrentoutput')
    expect(output).not.toContain('BACKGROUND OUTPUT')
    expect(output).not.toContain('Split')
  })
})

import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const inputHarness = vi.hoisted(() => ({
  calls: 0,
  handler: undefined as undefined | ((input: string, key: Record<string, boolean>) => void)
}))

vi.mock('@hermes/ink', async importOriginal => {
  const mod = await importOriginal()

  return {
    ...mod,
    useInput: (handler: (input: string, key: Record<string, boolean>) => void) => {
      inputHarness.calls += 1
      inputHarness.handler = handler
    }
  }
})

import { GatewayProvider } from '../app/gatewayContext.js'
import type { SubscriptionOverlayState } from '../app/interfaces.js'
import { getOutputStreamsState, resetOutputStreams, setSecondaryOutput, syncOutputSessions } from '../app/outputStreamStore.js'
import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { resetUiState } from '../app/uiStore.js'
import { FloatingOverlays } from '../components/appOverlays.js'
import { outputFocusDirection, OutputManager } from '../components/outputManager.js'
import type { GatewayClient } from '../gatewayClient.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const gatewayValue = { gw: {} as GatewayClient, rpc: vi.fn() }

const mountNode = (node: React.ReactElement) => {
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

  return {
    app,
    output: () => stripAnsi(output)
  }
}

const renderToText = (node: React.ReactElement) => {
  const mounted = mountNode(node)
  const output = mounted.output()

  mounted.app.unmount()
  mounted.app.cleanup()

  return output
}

const subscriptionPrompt = (): SubscriptionOverlayState => ({
  ctx: {
    fetchCard: vi.fn().mockResolvedValue(null),
    openManageLink: vi.fn().mockResolvedValue(true),
    openPortal: vi.fn(),
    preview: vi.fn().mockResolvedValue(null),
    refreshState: vi.fn().mockResolvedValue(null),
    requestRemoteSpending: vi.fn().mockResolvedValue({ granted: true }),
    resume: vi.fn().mockResolvedValue(null),
    scheduleCancellation: vi.fn().mockResolvedValue(null),
    scheduleChange: vi.fn().mockResolvedValue(null),
    sys: vi.fn(),
    upgrade: vi.fn().mockResolvedValue(null)
  },
  screen: 'overview',
  state: {
    can_change_plan: true,
    current: null,
    is_admin: true,
    logged_in: true,
    ok: true,
    org_id: 'org-acme',
    org_name: 'Acme',
    portal_url: 'https://example.test/billing',
    role: 'OWNER',
    tiers: []
  }
})

const floatingOverlays = (onOutputFocus: (sessionId: string) => Promise<boolean>, onRawActivate = vi.fn()) => (
  <GatewayProvider value={gatewayValue}>
    <FloatingOverlays
      cols={120}
      compIdx={0}
      completions={[]}
      onActiveSessionClose={vi.fn()}
      onActiveSessionSelect={onRawActivate}
      onModelSelect={vi.fn()}
      onNewLiveSession={vi.fn()}
      onNewPromptSession={vi.fn()}
      onOutputFocus={onOutputFocus}
      onResumeSelect={vi.fn()}
      pagerPageSize={18}
    />
  </GatewayProvider>
)

describe('output manager', () => {
  beforeEach(() => {
    inputHarness.calls = 0
    inputHarness.handler = undefined
    resetOverlayState()
    resetOutputStreams()
    resetUiState()
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

  it.each([
    [
      'confirmation',
      {
        confirm: {
          onConfirm: vi.fn(),
          title: 'Confirm operation'
        }
      }
    ],
    ['subscription', { subscription: subscriptionPrompt() }]
  ])('does not mount or register manager input behind a %s prompt', (_name, prompt) => {
    patchOverlayState({ outputs: true, ...prompt })

    const mounted = mountNode(floatingOverlays(vi.fn().mockResolvedValue(true)))

    expect(mounted.output()).not.toContain('Output streams')
    expect(inputHarness.calls).toBe(0)
    expect(inputHarness.handler).toBeUndefined()

    mounted.app.unmount()
    mounted.app.cleanup()
  })

  it('routes Enter on the current primary through output focus without collapsing split', async () => {
    const before = structuredClone(getOutputStreamsState().layout)
    const onOutputFocus = vi.fn().mockResolvedValue(true)
    const onRawActivate = vi.fn().mockResolvedValue(true)

    patchOverlayState({ outputs: true })
    const mounted = mountNode(floatingOverlays(onOutputFocus, onRawActivate))

    expect(inputHarness.calls).toBe(1)
    inputHarness.handler?.('', { return: true })
    await vi.waitFor(() => expect(onOutputFocus).toHaveBeenCalledWith('sid-a'))

    expect(onRawActivate).not.toHaveBeenCalled()
    expect(getOutputStreamsState().layout).toEqual(before)
    expect(getOverlayState().outputs).toBe(false)

    mounted.app.unmount()
    mounted.app.cleanup()
  })

  it('keeps manager and split state unchanged when focusing another session fails', async () => {
    const before = structuredClone(getOutputStreamsState().layout)
    const onOutputFocus = vi.fn().mockResolvedValue(false)
    const onRawActivate = vi.fn().mockResolvedValue(true)

    patchOverlayState({ outputs: true })
    const mounted = mountNode(floatingOverlays(onOutputFocus, onRawActivate))

    inputHarness.handler?.('', { downArrow: true })
    mounted.app.rerender(floatingOverlays(onOutputFocus, onRawActivate))
    inputHarness.handler?.('', { return: true })
    await vi.waitFor(() => expect(onOutputFocus).toHaveBeenCalledWith('sid-b'))

    expect(onRawActivate).not.toHaveBeenCalled()
    expect(getOutputStreamsState().layout).toEqual(before)
    expect(getOverlayState().outputs).toBe(true)

    mounted.app.unmount()
    mounted.app.cleanup()
  })

  it('maps Alt+arrow to output focus direction only', () => {
    expect(outputFocusDirection({ leftArrow: true, meta: true, rightArrow: false })).toBe(-1)
    expect(outputFocusDirection({ leftArrow: false, meta: true, rightArrow: true })).toBe(1)
    expect(outputFocusDirection({ leftArrow: true, meta: false, rightArrow: false })).toBe(0)
  })
})

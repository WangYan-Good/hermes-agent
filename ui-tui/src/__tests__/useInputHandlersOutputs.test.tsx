import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const inputHarness = vi.hoisted(() => ({
  dashboardMode: true,
  handler: undefined as undefined | ((input: string, key: Record<string, boolean>) => void)
}))

vi.mock('@hermes/ink', async importOriginal => {
  const mod = await importOriginal()

  return {
    ...mod,
    useInput: (handler: (input: string, key: Record<string, boolean>) => void) => {
      inputHarness.handler = handler
    }
  }
})

vi.mock('../config/env.js', async importOriginal => {
  const mod = await importOriginal()

  return {
    ...mod,
    get DASHBOARD_TUI_MODE() {
      return inputHarness.dashboardMode
    }
  }
})

import type { InputHandlerContext } from '../app/interfaces.js'
import { resetOverlayState } from '../app/overlayStore.js'
import { resetUiState } from '../app/uiStore.js'
import { useInputHandlers } from '../app/useInputHandlers.js'
import type { GatewayClient } from '../gatewayClient.js'
import { DEFAULT_VOICE_RECORD_KEY } from '../lib/platform.js'

const mountInputHandlers = (input: string, inputBuf: string[], cycleOutputFocus: (direction: -1 | 1) => void) => {
  const noop = vi.fn()

  const context = {
    actions: {
      answerClarify: noop,
      appendMessage: noop,
      cycleOutputFocus,
      die: noop,
      dispatchSubmission: noop,
      guardBusySessionSwitch: vi.fn(() => false),
      newSession: noop,
      sys: noop
    },
    composer: {
      actions: {
        attachClipboardImage: noop,
        attachImagePath: noop,
        clearIn: noop,
        dequeue: vi.fn(),
        enqueue: noop,
        handleTextPaste: vi.fn().mockResolvedValue(null),
        openEditor: vi.fn().mockResolvedValue(undefined),
        prependQueue: noop,
        pushHistory: noop,
        removeQueue: noop,
        setCompIdx: noop,
        setComposerTokens: noop,
        setHistoryIdx: noop,
        setInput: noop,
        setInputBuf: noop,
        setQueueEdit: noop,
        syncTokens: noop,
        takeQueue: vi.fn()
      },
      refs: {
        historyDraftRef: { current: '' },
        historyRef: { current: [] },
        queueEditRef: { current: null },
        queueRef: { current: [] },
        submitRef: { current: noop },
        tokensRef: { current: [] }
      },
      state: {
        compIdx: 0,
        compReplace: 0,
        completions: [],
        historyIdx: null,
        input,
        inputBuf,
        queueEditIdx: null,
        queuedDisplay: [],
        tokens: []
      }
    },
    gateway: { gw: {} as GatewayClient, rpc: vi.fn().mockResolvedValue(null) },
    terminal: {
      hasSelection: false,
      scrollRef: { current: null },
      scrollWithSelection: noop,
      selection: {
        captureScrolledRows: noop,
        clearSelection: noop,
        copySelection: vi.fn().mockResolvedValue(''),
        copySelectionNoClear: vi.fn().mockResolvedValue(''),
        getState: vi.fn(),
        shiftAnchor: noop,
        shiftSelection: noop,
        version: vi.fn(() => 0)
      }
    },
    voice: {
      enabled: false,
      recordKey: DEFAULT_VOICE_RECORD_KEY,
      recording: false,
      setProcessing: noop,
      setRecording: noop,
      setVoiceEnabled: noop,
      setVoiceTts: noop
    },
    wheelStep: 3
  } satisfies InputHandlerContext

  const Harness = () => {
    useInputHandlers(context)

    return null
  }

  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 120, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })

  return renderSync(<Harness />, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })
}

describe('output focus shortcuts', () => {
  beforeEach(() => {
    inputHarness.handler = undefined
    resetOverlayState()
    resetUiState()
  })

  it.each([
    ['Dashboard with an empty composer', true, '', [], 1],
    ['standalone with an empty composer', false, '', [], 0],
    ['Dashboard with composer text', true, 'draft', [], 0],
    ['Dashboard with a buffered composer line', true, '', ['draft'], 0]
  ])('%s cycles the visible output exactly as gated', (_name, dashboardMode, input, inputBuf, calls) => {
    inputHarness.dashboardMode = dashboardMode
    const cycleOutputFocus = vi.fn()
    const app = mountInputHandlers(input, inputBuf, cycleOutputFocus)

    inputHarness.handler?.('', { leftArrow: false, meta: true, rightArrow: true })

    expect(cycleOutputFocus).toHaveBeenCalledTimes(calls)

    if (calls) {
      expect(cycleOutputFocus).toHaveBeenCalledWith(1)
    }

    app.unmount()
    app.cleanup()
  })
})

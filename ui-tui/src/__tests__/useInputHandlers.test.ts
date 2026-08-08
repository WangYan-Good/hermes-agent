import { describe, expect, it, vi } from 'vitest'

import { enqueueControlPrompt } from '../app/controlPromptQueue.js'
import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import {
  applyVoiceRecordResponse,
  dismissApprovalPrompt,
  dismissSensitivePrompt,
  handleIdleHotkeyExit,
  shouldAllowIdleHotkeyExit,
  shouldFallThroughForScroll
} from '../app/useInputHandlers.js'

const baseKey = {
  downArrow: false,
  pageDown: false,
  pageUp: false,
  shift: false,
  upArrow: false,
  wheelDown: false,
  wheelUp: false
}

describe('shouldFallThroughForScroll — keep transcript scrolling alive during prompt overlays', () => {
  it('falls through for wheel scrolls', () => {
    expect(shouldFallThroughForScroll({ ...baseKey, wheelUp: true })).toBe(true)
    expect(shouldFallThroughForScroll({ ...baseKey, wheelDown: true })).toBe(true)
  })

  it('falls through for PageUp / PageDown', () => {
    expect(shouldFallThroughForScroll({ ...baseKey, pageUp: true })).toBe(true)
    expect(shouldFallThroughForScroll({ ...baseKey, pageDown: true })).toBe(true)
  })

  it('falls through for Shift+ArrowUp / Shift+ArrowDown', () => {
    expect(shouldFallThroughForScroll({ ...baseKey, shift: true, upArrow: true })).toBe(true)
    expect(shouldFallThroughForScroll({ ...baseKey, shift: true, downArrow: true })).toBe(true)
  })

  it('does NOT fall through for plain arrows — those drive in-prompt selection', () => {
    expect(shouldFallThroughForScroll({ ...baseKey, upArrow: true })).toBe(false)
    expect(shouldFallThroughForScroll({ ...baseKey, downArrow: true })).toBe(false)
  })

  it('does NOT fall through for plain Shift — without an arrow it is a no-op', () => {
    expect(shouldFallThroughForScroll({ ...baseKey, shift: true })).toBe(false)
  })

  it('does NOT fall through for unrelated state (no scroll keys held)', () => {
    expect(shouldFallThroughForScroll(baseKey)).toBe(false)
  })
})

describe('shouldAllowIdleHotkeyExit', () => {
  it('keeps idle exit hotkeys enabled in normal terminals', () => {
    expect(shouldAllowIdleHotkeyExit(false)).toBe(true)
  })

  it('disables idle exit hotkeys in dashboard chat', () => {
    expect(shouldAllowIdleHotkeyExit(true)).toBe(false)
  })
})

describe('handleIdleHotkeyExit', () => {
  it('exits in normal terminals', () => {
    const actions = { die: vi.fn(), sys: vi.fn() }

    handleIdleHotkeyExit(actions, false)

    expect(actions.die).toHaveBeenCalledTimes(1)
    expect(actions.sys).not.toHaveBeenCalled()
  })

  it('asks the dashboard for a fresh chat instead of leaving a ghost session', () => {
    const actions = { die: vi.fn(), sys: vi.fn() }
    const requestDashboardNewSession = vi.fn()

    handleIdleHotkeyExit(actions, true, requestDashboardNewSession)

    expect(actions.die).not.toHaveBeenCalled()
    expect(requestDashboardNewSession).toHaveBeenCalledTimes(1)
    expect(actions.sys).toHaveBeenCalledWith('starting a fresh dashboard chat...')
  })
})

describe('applyVoiceRecordResponse', () => {
  it('reverts optimistic REC state when the gateway reports voice busy', () => {
    const setProcessing = vi.fn()
    const setRecording = vi.fn()
    const sys = vi.fn()

    applyVoiceRecordResponse({ status: 'busy' }, true, { setProcessing, setRecording }, sys)

    expect(setRecording).toHaveBeenCalledWith(false)
    expect(setProcessing).toHaveBeenCalledWith(true)
    expect(sys).toHaveBeenCalledWith('voice: still transcribing; try again shortly')
  })

  it('keeps optimistic REC state for successful recording starts', () => {
    const setProcessing = vi.fn()
    const setRecording = vi.fn()

    applyVoiceRecordResponse({ status: 'recording' }, true, { setProcessing, setRecording }, vi.fn())

    expect(setRecording).not.toHaveBeenCalled()
    expect(setProcessing).not.toHaveBeenCalled()
  })

  it('reverts optimistic REC state when the gateway returns null', () => {
    const setProcessing = vi.fn()
    const setRecording = vi.fn()

    applyVoiceRecordResponse(null, true, { setProcessing, setRecording }, vi.fn())

    expect(setRecording).toHaveBeenCalledWith(false)
    expect(setProcessing).toHaveBeenCalledWith(false)
  })
})

describe('dismissSensitivePrompt', () => {
  it('clears a sudo overlay before a stale cancel RPC resolves', async () => {
    resetOverlayState()
    patchOverlayState({ sudo: { requestId: 'sudo-1', sessionId: 'sid-b', sessionTitle: 'Beta' } })
    const rpc = vi.fn().mockResolvedValue(null)
    const sys = vi.fn()

    const pending = dismissSensitivePrompt(getOverlayState(), rpc, sys)

    expect(getOverlayState().sudo).toBeNull()
    expect(sys).toHaveBeenCalledWith('sudo cancelled')
    expect(rpc).toHaveBeenCalledWith('sudo.respond', { password: '', request_id: 'sudo-1', session_id: 'sid-b' })
    await pending
  })

  it('clears a secret overlay before a stale cancel RPC resolves', async () => {
    resetOverlayState()
    patchOverlayState({ secret: { envVar: 'API_KEY', prompt: 'Enter API key', requestId: 'secret-1', sessionId: 'sid-b', sessionTitle: 'Beta' } })
    const rpc = vi.fn().mockResolvedValue(null)
    const sys = vi.fn()

    const pending = dismissSensitivePrompt(getOverlayState(), rpc, sys)

    expect(getOverlayState().secret).toBeNull()
    expect(sys).toHaveBeenCalledWith('secret entry cancelled')
    expect(rpc).toHaveBeenCalledWith('secret.respond', { request_id: 'secret-1', session_id: 'sid-b', value: '' })
    await pending
  })
})

describe('dismissApprovalPrompt', () => {
  it('denies the approval source session and promotes the next queued prompt', async () => {
    resetOverlayState()
    enqueueControlPrompt({
      kind: 'approval',
      request: { command: 'deploy', description: 'approve deploy', sessionId: 'sid-a', sessionTitle: 'Alpha' }
    })
    enqueueControlPrompt({
      kind: 'secret',
      request: { envVar: 'TOKEN', prompt: 'Enter TOKEN', requestId: 'secret-b', sessionId: 'sid-b', sessionTitle: 'Beta' }
    })
    const rpc = vi.fn().mockResolvedValue({ ok: true })

    const pending = dismissApprovalPrompt(getOverlayState(), rpc)

    expect(rpc).toHaveBeenCalledWith('approval.respond', { choice: 'deny', session_id: 'sid-a' })
    expect(getOverlayState().approval).not.toBeNull()
    await pending
    expect(getOverlayState().secret?.sessionId).toBe('sid-b')
  })
})

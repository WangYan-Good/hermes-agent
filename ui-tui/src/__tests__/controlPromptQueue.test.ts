import { beforeEach, describe, expect, it } from 'vitest'

import {
  completeControlPrompt,
  type ControlPrompt,
  enqueueControlPrompt,
  expireControlPrompt
} from '../app/controlPromptQueue.js'
import { $isBlocked, getOverlayState, resetOverlayState } from '../app/overlayStore.js'

const clarifyPrompt = (sessionId: string): ControlPrompt => ({
  kind: 'clarify',
  request: {
    choices: null,
    question: 'Clarify?',
    requestId: 'clarify-a',
    sessionId,
    sessionTitle: 'Alpha'
  }
})

const approvalPrompt = (sessionId: string): ControlPrompt => ({
  kind: 'approval',
  request: {
    command: 'deploy',
    description: 'Approve deploy',
    sessionId,
    sessionTitle: 'Beta'
  }
})

const sudoPrompt = (sessionId: string): ControlPrompt => ({
  kind: 'sudo',
  request: { requestId: 'sudo-c', sessionId, sessionTitle: 'Gamma' }
})

const secretPrompt = (sessionId: string): ControlPrompt => ({
  kind: 'secret',
  request: {
    envVar: 'TOKEN',
    prompt: 'Enter TOKEN',
    requestId: 'secret-d',
    sessionId,
    sessionTitle: 'Delta'
  }
})

describe('control prompt queue', () => {
  beforeEach(resetOverlayState)

  it('keeps one active cross-session prompt, blocks input, and promotes all request kinds FIFO', () => {
    enqueueControlPrompt(clarifyPrompt('sid-a'))
    enqueueControlPrompt(approvalPrompt('sid-b'))
    enqueueControlPrompt(sudoPrompt('sid-c'))
    enqueueControlPrompt(secretPrompt('sid-d'))

    expect(getOverlayState().clarify?.sessionId).toBe('sid-a')
    expect(getOverlayState().controlQueue).toHaveLength(3)
    expect($isBlocked.get()).toBe(true)

    completeControlPrompt('clarify', 'clarify-a')
    expect(getOverlayState().approval?.sessionId).toBe('sid-b')
    completeControlPrompt('approval')
    expect(getOverlayState().sudo?.sessionId).toBe('sid-c')
    completeControlPrompt('sudo', 'sudo-c')
    expect(getOverlayState().secret?.sessionId).toBe('sid-d')
  })

  it('expires only the matching queued request and leaves the active source untouched', () => {
    enqueueControlPrompt(sudoPrompt('sid-a'))
    enqueueControlPrompt(secretPrompt('sid-b'))

    expireControlPrompt('secret', 'secret-d')

    expect(getOverlayState().sudo?.sessionId).toBe('sid-a')
    expect(getOverlayState().controlQueue).toEqual([])
  })
})

import { beforeEach, describe, expect, it } from 'vitest'

import {
  completeControlPrompt,
  type ControlPrompt,
  enqueueControlPrompt,
  expireControlPrompt,
  removeControlPromptsForSession
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

    completeControlPrompt('clarify', 'clarify-a', 'sid-a')
    expect(getOverlayState().approval?.sessionId).toBe('sid-b')
    completeControlPrompt('approval', undefined, 'sid-b')
    expect(getOverlayState().sudo?.sessionId).toBe('sid-c')
    completeControlPrompt('sudo', 'sudo-c', 'sid-c')
    expect(getOverlayState().secret?.sessionId).toBe('sid-d')
  })

  it('expires only the matching queued request and leaves the active source untouched', () => {
    enqueueControlPrompt(sudoPrompt('sid-a'))
    enqueueControlPrompt(secretPrompt('sid-b'))

    expireControlPrompt('secret', 'secret-d', 'sid-b')

    expect(getOverlayState().sudo?.sessionId).toBe('sid-a')
    expect(getOverlayState().controlQueue).toEqual([])
  })
  it('completes an active clarify by source when request IDs collide', () => {
    enqueueControlPrompt(clarifyPrompt('sid-a'))
    enqueueControlPrompt(clarifyPrompt('sid-b'))

    completeControlPrompt('clarify', 'clarify-a', 'sid-a')

    expect(getOverlayState().clarify?.sessionId).toBe('sid-b')
    expect(getOverlayState().controlQueue).toEqual([])
  })

  it('expires a queued clarify by source without clearing the same-ID active prompt', () => {
    enqueueControlPrompt(clarifyPrompt('sid-b'))
    enqueueControlPrompt(clarifyPrompt('sid-a'))

    expireControlPrompt('clarify', 'clarify-a', 'sid-a')

    expect(getOverlayState().clarify?.sessionId).toBe('sid-b')
    expect(getOverlayState().controlQueue).toEqual([])
  })

  it('removes only prompts owned by the closed session and preserves global and sibling requests', () => {
    enqueueControlPrompt(approvalPrompt('sid-a'))
    enqueueControlPrompt(sudoPrompt('sid-b'))
    enqueueControlPrompt(secretPrompt('default'))
    enqueueControlPrompt(clarifyPrompt('sid-b'))

    removeControlPromptsForSession('sid-b')

    expect(getOverlayState().approval?.sessionId).toBe('sid-a')
    expect(getOverlayState().controlQueue).toEqual([
      expect.objectContaining({ kind: 'secret', request: expect.objectContaining({ sessionId: 'default' }) })
    ])

    removeControlPromptsForSession('sid-a')
    expect(getOverlayState().approval).toBeNull()
    expect(getOverlayState().secret?.sessionId).toBe('default')
  })

})

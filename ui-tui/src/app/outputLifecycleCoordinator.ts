import type { SessionCloseResponse } from '../gatewayTypes.js'

import {
  clearControlPrompts,
  removeControlPromptsForSession
} from './controlPromptQueue.js'
import type { OutputStreamRouter } from './outputStreamRouter.js'
import {
  commitOutputPrimaryTransition,
  markOutputTransportDisconnected,
  markOutputTransportReady,
  removeOutputSession,
  type SessionTransition,
  syncOutputSessions
} from './outputStreamStore.js'
import { turnController } from './turnController.js'

export interface OutputLifecycleCoordinator {
  applyCloseResult: (sessionId: string, result: null | SessionCloseResponse) => boolean
  commitTransition: (transition: SessionTransition) => void
  disconnect: () => void
  ready: () => boolean
  syncActiveSessions: (items: readonly unknown[], currentSessionId: null | string) => void
}

export const createOutputLifecycleCoordinator = (
  outputRouter: OutputStreamRouter
): OutputLifecycleCoordinator => ({
  applyCloseResult: (sessionId, result) => {
    if (!result?.closed && !result?.ok) {
      return false
    }

    removeOutputSession(sessionId)
    removeControlPromptsForSession(sessionId)

    return true
  },
  commitTransition: commitOutputPrimaryTransition,
  disconnect: () => {
    outputRouter.disconnect()
    markOutputTransportDisconnected()
    clearControlPrompts()
    turnController.fullReset()
  },
  ready: () => {
    const reconnected = markOutputTransportReady()

    if (reconnected) {
      clearControlPrompts()
    }

    return reconnected
  },
  syncActiveSessions: syncOutputSessions
})

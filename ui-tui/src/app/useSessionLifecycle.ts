import { writeFileSync } from 'node:fs'

import type { ScrollBoxHandle } from '@hermes/ink'
import { evictInkCaches } from '@hermes/ink'
import { type RefObject, useCallback, useEffect, useMemo, useRef } from 'react'

import { buildSetupRequiredSections, SETUP_REQUIRED_TITLE } from '../content/setup.js'
import { introMsg, toTranscriptMessages } from '../domain/messages.js'
import { ZERO } from '../domain/usage.js'
import { type GatewayClient } from '../gatewayClient.js'
import type {
  SessionActivateResponse,
  SessionCloseResponse,
  SessionCreateResponse,
  SessionInflightTurn,
  SessionResumeResponse,
  SessionTitleResponse,
  SetupStatusResponse
} from '../gatewayTypes.js'
import { asRpcResult } from '../lib/rpc.js'
import type { Msg, PanelSection, SessionInfo, Usage } from '../types.js'

import type { ComposerActions, GatewayRpc, StateSetter } from './interfaces.js'
import type { SessionTransition, SessionTransitionHooks, SessionTransitionKind } from './outputStreamStore.js'
import { patchOverlayState } from './overlayStore.js'
import { turnController } from './turnController.js'
import { patchTurnState } from './turnStore.js'
import { getUiState, patchUiState } from './uiStore.js'

const usageFrom = (info: null | SessionInfo): Usage => (info?.usage ? { ...ZERO, ...info.usage } : ZERO)

const statusFromLiveSession = (status?: string, running = false) => {
  if (status === 'waiting') {
    return 'waiting for input…'
  }

  if (status === 'starting') {
    return 'starting agent…'
  }

  return running || status === 'working' ? 'running…' : 'ready'
}

export interface ActivateLiveSessionAtomicOptions {
  afterCommit: (transition: SessionTransition) => void
  beforeCommit: (transition: SessionTransition) => void
  commit: (response: SessionActivateResponse) => void
  fail: (message: string) => void
  id: string
  isCurrent?: () => boolean
  previousSessionId: null | string
  request: (id: string) => Promise<unknown>
}

export async function activateLiveSessionAtomic(options: ActivateLiveSessionAtomicOptions): Promise<boolean> {
  let raw: unknown

  try {
    raw = await options.request(options.id)

  } catch (error) {
    options.fail(error instanceof Error ? error.message : String(error))

    return false
  }

    if (options.isCurrent && !options.isCurrent()) {
      return false
    }

    const response = asRpcResult<SessionActivateResponse>(raw)

    if (!response || typeof response.session_id !== 'string' || !Array.isArray(response.messages)) {
      options.fail('invalid response: session.activate')

      return false
    }

    const transition: SessionTransition = {
      kind: 'activate-live',
      nextSessionId: response.session_id,
      previousSessionId: options.previousSessionId
    }

    options.beforeCommit(transition)
    options.commit(response)
    options.afterCommit(transition)

    return true
}

export const createSessionIntentGeneration = () => {
  let generation = 0

  return {
    begin: (_kind: SessionTransitionKind) => {
      const current = ++generation

      return () => current === generation
    }
  }
}

const noopSessionTransitionHooks: SessionTransitionHooks = {
  afterCommit: () => {},
  beforeCommit: () => {}
}

export const writeActiveSessionFile = (sessionId: null | string, file = process.env.HERMES_TUI_ACTIVE_SESSION_FILE) => {
  if (!file || !sessionId) {
    return
  }

  try {
    writeFileSync(file, JSON.stringify({ session_id: sessionId }), { mode: 0o600 })
  } catch {
    // Best-effort shell epilogue hint only; never break live session changes.
  }
}

export const liveSessionInflightMessages = (inflight?: null | SessionInflightTurn): Msg[] => {
  const user = String(inflight?.user ?? '').trim()

  return user ? [{ role: 'user', text: user }] : []
}

export const hydrateLiveSessionInflight = (inflight?: null | SessionInflightTurn) => {
  const assistant = String(inflight?.assistant ?? '')

  if (!assistant && !inflight?.streaming) {
    return
  }

  turnController.hydrateStreamingText(assistant)
}

export const signalFreshSessionBoundary = (
  previousSid: null | string,
  nextSid: null | string,
  onFreshSessionStarted?: (sessionId: string) => void
) => {
  if (!previousSid || !nextSid || previousSid === nextSid || !onFreshSessionStarted) {
    return false
  }

  onFreshSessionStarted(nextSid)

  return true
}

export const scheduleResumeScrollToBottom = (
  scrollRef: RefObject<null | ScrollBoxHandle>,
  delays: readonly number[] = [0, 80, 240]
) => {
  const startedAt = Date.now()

  const timers = delays.map((delay, index) =>
    setTimeout(() => {
      const scroll = scrollRef.current

      if (!scroll) {
        return
      }

      const manuallyScrolledAfterResume = scroll.getLastManualScrollAt() > startedAt

      if (!manuallyScrolledAfterResume && (index === 0 || scroll.isSticky())) {
        scroll.scrollToBottom()
      }
    }, delay)
  )

  return () => {
    for (const timer of timers) {
      clearTimeout(timer)
    }
  }
}

const trimTail = (items: Msg[]) => {
  const q = [...items]

  while (q.at(-1)?.role === 'assistant' || q.at(-1)?.role === 'tool') {
    q.pop()
  }

  if (q.at(-1)?.role === 'user') {
    q.pop()
  }

  return q
}

export interface UseSessionLifecycleOptions {
  colsRef: { current: number }
  composerActions: ComposerActions
  gw: GatewayClient
  transitionHooks?: SessionTransitionHooks
  onFreshSessionStarted?: (sessionId: string) => void
  panel: (title: string, sections: PanelSection[]) => void
  rpc: GatewayRpc
  scrollRef: RefObject<null | ScrollBoxHandle>
  setHistoryItems: StateSetter<Msg[]>
  setLastUserMsg: StateSetter<string>
  setSessionStartedAt: StateSetter<number>
  setStickyPrompt: StateSetter<string>
  setVoiceProcessing: StateSetter<boolean>
  setVoiceRecording: StateSetter<boolean>
  sys: (text: string) => void
}

export function useSessionLifecycle(opts: UseSessionLifecycleOptions) {
  const {
    colsRef,
    composerActions,
    gw,
    onFreshSessionStarted,
    panel,
    rpc,
    scrollRef,
    setHistoryItems,
    setLastUserMsg,
    setSessionStartedAt,
    setStickyPrompt,

    setVoiceProcessing,
    setVoiceRecording,
    sys
  } = opts

  const transitionHooks = opts.transitionHooks ?? noopSessionTransitionHooks

  const closeSession = useCallback(
    (targetSid?: null | string) =>
      targetSid ? rpc<SessionCloseResponse>('session.close', { session_id: targetSid }) : Promise.resolve(null),
    [rpc]
  )

  const cancelResumeScrollRef = useRef<null | (() => void)>(null)
  const sessionIntentRef = useRef(createSessionIntentGeneration())

  const resetSession = useCallback(() => {
    cancelResumeScrollRef.current?.()
    cancelResumeScrollRef.current = null
    turnController.fullReset()
    setVoiceRecording(false)
    setVoiceProcessing(false)
    patchUiState({ bgTasks: new Set(), info: null, sid: null, usage: ZERO })
    setHistoryItems([])
    setLastUserMsg('')
    setStickyPrompt('')
    composerActions.setComposerTokens([])
    // Half-prune: new session has new keys, but keep a warm pool in case
    // the user resumes back to the prior session.
    evictInkCaches('half')
  }, [composerActions, setHistoryItems, setLastUserMsg, setStickyPrompt, setVoiceProcessing, setVoiceRecording])

  useEffect(
    () => () => {
      cancelResumeScrollRef.current?.()
      cancelResumeScrollRef.current = null
    },
    []
  )

  const resetVisibleHistory = useCallback(
    (info: null | SessionInfo = null) => {
      turnController.idle()
      turnController.clearReasoning()
      turnController.turnTools = []
      turnController.persistedToolLabels.clear()

      setHistoryItems(info ? [introMsg(info)] : [])
      setStickyPrompt('')
      setLastUserMsg('')
      composerActions.setComposerTokens([])
      patchTurnState({ activity: [] })
      patchUiState({ info, usage: usageFrom(info) })
    },
    [composerActions, setHistoryItems, setLastUserMsg, setStickyPrompt]
  )

  const startNewSession = useCallback(
    async (msg?: string, title?: string, keepCurrent = false, transitionKind: SessionTransitionKind = 'replace') => {
      const isCurrent = sessionIntentRef.current.begin(transitionKind)
      const setup = await rpc<SetupStatusResponse>('setup.status', {})

      if (!isCurrent()) {
        return null
      }

      if (setup?.provider_configured === false) {
        panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections())
        patchUiState({ status: 'setup required' })

        return null
      }

      const previousSid = getUiState().sid

      if (!keepCurrent) {
        await closeSession(previousSid)
      }

      if (!isCurrent()) {
        return null
      }

      const r = await rpc<SessionCreateResponse>('session.create', { cols: colsRef.current })

      if (!isCurrent()) {
        return null
      }

      if (!r) {
        patchUiState({ status: 'ready' })

        return null
      }

      const info = r.info ?? null
      const requestedTitle = title?.trim() ?? ''

      const transition: SessionTransition = {
        kind: transitionKind,
        nextSessionId: r.session_id,
        previousSessionId: previousSid
      }

      transitionHooks.beforeCommit(transition)


      resetSession()
      setSessionStartedAt(Date.now())

      writeActiveSessionFile(r.session_id)
      patchUiState({
        info,
        sid: r.session_id,
        status: info?.version ? 'ready' : 'starting agent…',
        usage: usageFrom(info)
      })

      if (info) {
        setHistoryItems([introMsg(info)])
      }

      if (info?.credential_warning) {
        sys(`warning: ${info.credential_warning}`)
      }

      if (info?.config_warning) {
        sys(`warning: ${info.config_warning}`)
      }

      if (msg) {
        sys(msg)
      }

      if (requestedTitle) {
        rpc<SessionTitleResponse>('session.title', {
          session_id: r.session_id,
          title: requestedTitle
        })
          .then(result => {
            if (!result || getUiState().sid !== r.session_id) {
              return
            }

            const nextTitle = (result.title ?? requestedTitle).trim()
            const suffix = result.pending ? ' (queued while session initializes)' : ''
            sys(`session title set: ${nextTitle}${suffix}`)
          })
          .catch((err: unknown) => {
            if (getUiState().sid !== r.session_id) {
              return
            }

            const message = err instanceof Error ? err.message : String(err)
            sys(`warning: failed to set session title: ${message}`)
          })
      }

      signalFreshSessionBoundary(previousSid, r.session_id, onFreshSessionStarted)
      transitionHooks.afterCommit(transition)

      return r.session_id
    },
    [closeSession, colsRef, onFreshSessionStarted, panel, resetSession, rpc, setHistoryItems, setSessionStartedAt, sys, transitionHooks]
  )

  const newSession = useCallback(
    (msg?: string, title?: string) => startNewSession(msg, title, false),
    [startNewSession]
  )

  const newLiveSession = useCallback(
    (msg = 'new live session started', title?: string) => {
      patchOverlayState({ sessions: false })

      return startNewSession(msg, title, true, 'new-live')
    },
    [startNewSession]
  )

  const activateLiveSession = useCallback(
    async (id: string): Promise<boolean> => {
      const isCurrent = sessionIntentRef.current.begin('activate-live')
      const previousSessionId = getUiState().sid

      return activateLiveSessionAtomic({
        afterCommit: transitionHooks.afterCommit,
        beforeCommit: transitionHooks.beforeCommit,
        commit: r => {
          patchOverlayState({ sessions: false })
          const info = r.info ?? null
          const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')

          resetSession()
          setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())
          const transcript = [...toTranscriptMessages(r.messages), ...liveSessionInflightMessages(r.inflight)]
          setHistoryItems(info ? [introMsg(info), ...transcript] : transcript)
          writeActiveSessionFile(r.session_key ?? r.session_id)
          patchUiState({
            busy: running,
            info,
            sid: r.session_id,
            status: statusFromLiveSession(r.status, running),
            usage: usageFrom(info)
          })
          hydrateLiveSessionInflight(r.inflight)
          cancelResumeScrollRef.current?.()
          cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)
        },
        fail: message => sys(`error: ${message}`),
        id,
        isCurrent,
        previousSessionId,
        request: sessionId => gw.request<SessionActivateResponse>('session.activate', { session_id: sessionId })
      })
    },
    [gw, resetSession, scrollRef, setHistoryItems, setSessionStartedAt, sys, transitionHooks]
  )

  const resumeById = useCallback(
    (id: string) => {
      const isCurrent = sessionIntentRef.current.begin('resume')
      patchOverlayState({ sessions: false })
      patchUiState({ status: 'resuming…' })

      rpc<SetupStatusResponse>('setup.status', {}).then(setup => {
        if (!isCurrent()) {
          return
        }

        if (setup?.provider_configured === false) {
          panel(SETUP_REQUIRED_TITLE, buildSetupRequiredSections())
          patchUiState({ status: 'setup required' })

          return
        }

        const previousSid = getUiState().sid

        gw.request<SessionResumeResponse>('session.resume', { cols: colsRef.current, session_id: id })
          .then(raw => {
            const r = asRpcResult<SessionResumeResponse>(raw)

            if (!isCurrent()) {
              return
            }


            if (!r) {
              sys('error: invalid response: session.resume')

              return patchUiState({ status: 'ready' })
            }

            const info = r.info ?? null
            const running = Boolean(r.running || r.status === 'working' || r.status === 'waiting')

            const transition: SessionTransition = {
              kind: 'resume',
              nextSessionId: r.session_id,
              previousSessionId: previousSid
            }

            transitionHooks.beforeCommit(transition)


            resetSession()
            setSessionStartedAt(r.started_at ? r.started_at * 1000 : Date.now())

            const resumed = [...toTranscriptMessages(r.messages), ...liveSessionInflightMessages(r.inflight)]

            setHistoryItems(info ? [introMsg(info), ...resumed] : resumed)
            writeActiveSessionFile(r.resumed ?? r.session_id)
            patchUiState({
              busy: running,
              info,
              sid: r.session_id,
              status: statusFromLiveSession(r.status, running),
              usage: usageFrom(info)
            })
            hydrateLiveSessionInflight(r.inflight)
            cancelResumeScrollRef.current?.()
            cancelResumeScrollRef.current = scheduleResumeScrollToBottom(scrollRef)
            transitionHooks.afterCommit(transition)

            if (previousSid && previousSid !== r.session_id) {
              void closeSession(previousSid)
            }
          }, (e: Error) => {
            sys(`error: ${e.message}`)
            patchUiState({ status: 'ready' })
          })
      })
    },
    [closeSession, colsRef, gw, panel, resetSession, rpc, scrollRef, setHistoryItems, setSessionStartedAt, sys, transitionHooks]
  )

  const guardBusySessionSwitch = useCallback(
    (what = 'switch sessions') => {
      if (!getUiState().busy) {
        return false
      }

      sys(`interrupt the current turn before trying to ${what}`)

      return true
    },
    [sys]
  )

  return useMemo(
    () => ({
      activateLiveSession,
      closeSession,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById,
      trimLastExchange: trimTail
    }),
    [
      activateLiveSession,
      closeSession,
      guardBusySessionSwitch,
      newLiveSession,
      newSession,
      resetSession,
      resetVisibleHistory,
      resumeById
    ]
  )
}

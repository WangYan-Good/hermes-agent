import { atom } from 'nanostores'

import type { SessionActiveItem } from '../gatewayTypes.js'

import type { GatewayRpc } from './interfaces.js'

export type OutputSessionRole = 'Closed' | 'Inactive' | 'Owned' | 'Watched'
export type DesiredWatchStatus = 'pending' | 'rejected' | 'subscribed'

export interface DesiredOutputWatch {
  reason?: string
  runtimeSessionId: string
  sessionKey: string
  status: DesiredWatchStatus
}

export interface EffectiveOutputSubscription {
  anchorSessionId: string
  sessionKey: string
}

export interface OutputSubscriptionState {
  awaitingOwnedReattach: boolean
  controlledSessionId: null | string
  desired: Record<string, DesiredOutputWatch>
  effective: Record<string, EffectiveOutputSubscription>
  focusedSessionId: null | string
  generation: number
  sessions: Record<string, SessionActiveItem>
}

const initialState = (): OutputSubscriptionState => ({
  awaitingOwnedReattach: false,
  controlledSessionId: null,
  desired: {},
  effective: {},
  focusedSessionId: null,
  generation: 0,
  sessions: {}
})

export const $outputSubscriptionState = atom<OutputSubscriptionState>(initialState())
export const getOutputSubscriptionState = () => $outputSubscriptionState.get()
export const resetOutputSubscriptionState = () => $outputSubscriptionState.set(initialState())

interface SubscribeResponse {
  rejected?: { reason?: string; session_id?: string }[]
  subscriptions?: { session_id?: string }[]
}

export interface OutputSnapshotResponse {
  messages: { role: 'assistant' | 'user'; text: string }[]
  mode: 'active_live_only' | 'snapshot'
  session_id: string
  status: string
  stored_session_id?: string
}

export interface OutputSubscriptionHooks {
  mergeSnapshot?: (snapshot: OutputSnapshotResponse) => void
  removeLayoutReference?: (sessionId: string) => void
}

const durableKey = (item: SessionActiveItem) => item.session_key || item.id

export interface OutputSubscriptionCoordinator {
  focus: (
    sessionId: string,
    controlledSessionId: null | string,
    activateOwned: (sessionId: string) => Promise<boolean>
  ) => Promise<boolean>
  focusedSessionId: () => null | string
  gatewayReady: () => void
  isDesired: (sessionId: string) => boolean
  isOwned: (sessionId: string) => boolean
  isWatched: (sessionId: string) => boolean
  ownerLost: (sessionId: string) => void
  roleFor: (sessionId: string, closed?: boolean) => OutputSessionRole
  setFocusedSession: (sessionId: null | string) => void
  settled: () => Promise<void>
  syncActiveSessions: (items: readonly SessionActiveItem[], currentSessionId: null | string) => void
  takeControlSucceeded: (sessionId: string) => Promise<void>
  unwatch: (sessionId: string) => Promise<boolean>
  watch: (sessionId: string) => Promise<boolean>
}

export const createOutputSubscriptionCoordinator = (
  rpc: GatewayRpc,
  hooks: OutputSubscriptionHooks = {}
): OutputSubscriptionCoordinator => {
  const inflight = new Map<string, Promise<boolean>>()

  const patch = (update: (state: OutputSubscriptionState) => OutputSubscriptionState) => {
    $outputSubscriptionState.set(update(getOutputSubscriptionState()))
  }

  const ownedAnchors = () => {
    const state = getOutputSubscriptionState()

    const ids = Object.values(state.sessions)
      .filter(item => item.owned)
      .map(item => item.id)

    if (!state.controlledSessionId || !ids.includes(state.controlledSessionId)) {
      return ids
    }

    return [state.controlledSessionId, ...ids.filter(id => id !== state.controlledSessionId)]
  }

  const subscribe = (sessionKey: string, runtimeSessionId: string): Promise<boolean> => {
    const existing = inflight.get(sessionKey)

    if (existing) {
      return existing
    }

    const anchors = ownedAnchors()

    if (!anchors.length) {
      return Promise.resolve(false)
    }

    const generation = getOutputSubscriptionState().generation

    const request = (async () => {
      let acceptedAnchor = ''
      let rejectionReason = 'rejected'

      for (const anchor of anchors) {
        const result = await rpc<SubscribeResponse>('session.output_subscribe', {
          subscriber_session_id: anchor,
          session_ids: [runtimeSessionId]
        })

        const accepted = Boolean(result?.subscriptions?.some(item => item.session_id === runtimeSessionId))
        const rejection = result?.rejected?.find(item => item.session_id === runtimeSessionId)

        if (accepted) {
          acceptedAnchor = anchor

          break
        }

        rejectionReason = rejection?.reason || (result ? 'rejected' : 'transport_error')

        if (rejectionReason !== 'profile_mismatch') {
          break
        }
      }

        const current = getOutputSubscriptionState()
        const desired = current.desired[sessionKey]

        if (!desired || current.generation !== generation || desired.runtimeSessionId !== runtimeSessionId) {
          return false
        }

        const accepted = Boolean(acceptedAnchor)

        patch(state => ({
          ...state,
          desired: {
            ...state.desired,
            [sessionKey]: {
              ...state.desired[sessionKey],
              reason: accepted ? undefined : rejectionReason,
              status: accepted ? 'subscribed' : 'rejected'
            }
          },
          effective: accepted
            ? {
                ...state.effective,
                [runtimeSessionId]: { anchorSessionId: acceptedAnchor, sessionKey }
              }
            : Object.fromEntries(
                Object.entries(state.effective).filter(([, subscription]) => subscription.sessionKey !== sessionKey)
              )
        }))

        if (accepted && hooks.mergeSnapshot) {
          try {
            const snapshot = await rpc<OutputSnapshotResponse>('session.output_snapshot', {
              session_id: runtimeSessionId,
              subscriber_session_id: acceptedAnchor
            })

            const latest = getOutputSubscriptionState()
            const effective = latest.effective[runtimeSessionId]

            if (
              snapshot?.mode === 'snapshot' &&
              snapshot.session_id === runtimeSessionId &&
              latest.generation === generation &&
              latest.desired[sessionKey]?.runtimeSessionId === runtimeSessionId &&
              effective?.anchorSessionId === acceptedAnchor &&
              effective.sessionKey === sessionKey
            ) {
              hooks.mergeSnapshot(snapshot)
            }
          } catch {
            // Snapshot is optional history hydration. The exact live
            // subscription remains authoritative for future output.
          }
        }

        return accepted
      })()
      .catch(() => {
        const desired = getOutputSubscriptionState().desired[sessionKey]

        if (desired?.runtimeSessionId === runtimeSessionId) {
          patch(state => ({
            ...state,
            desired: {
              ...state.desired,
              [sessionKey]: { ...state.desired[sessionKey], reason: 'transport_error', status: 'rejected' }
            }
          }))
        }

        return false
      })
      .finally(() => {
        if (inflight.get(sessionKey) === request) {
          inflight.delete(sessionKey)
        }
      })

    inflight.set(sessionKey, request)

    return request
  }

  const coordinator: OutputSubscriptionCoordinator = {
    focus: async (sessionId, controlledSessionId, activateOwned) => {
      if (sessionId === controlledSessionId) {
        coordinator.setFocusedSession(null)

        return true
      }

      if (coordinator.isWatched(sessionId)) {
        coordinator.setFocusedSession(sessionId)

        return true
      }

      if (!coordinator.isOwned(sessionId)) {
        return false
      }

      const activated = await activateOwned(sessionId)

      if (activated) {
        coordinator.setFocusedSession(null)
      }

      return activated
    },
    focusedSessionId: () => getOutputSubscriptionState().focusedSessionId,
    gatewayReady: () => {
      patch(state => ({
        ...state,
        awaitingOwnedReattach: true,
        desired: Object.fromEntries(
          Object.entries(state.desired).map(([key, watch]) => [key, { ...watch, reason: undefined, status: 'pending' }])
        ),
        effective: {},
        generation: state.generation + 1
      }))
      inflight.clear()
    },
    isDesired: sessionId => {
      const state = getOutputSubscriptionState()
      const item = state.sessions[sessionId]

      return Boolean(item && state.desired[durableKey(item)])
    },
    isOwned: sessionId => Boolean(getOutputSubscriptionState().sessions[sessionId]?.owned),
    isWatched: sessionId => Boolean(getOutputSubscriptionState().effective[sessionId]),
    ownerLost: sessionId => {
      patch(state => {
        const item = state.sessions[sessionId]

        return {
          ...state,
          focusedSessionId: sessionId,
          sessions: item
            ? { ...state.sessions, [sessionId]: { ...item, owned: false, watchable: false } }
            : state.sessions
        }
      })
    },
    roleFor: (sessionId, closed = false) => {
      if (closed) {
        return 'Closed'
      }

      if (coordinator.isOwned(sessionId)) {
        return 'Owned'
      }

      return coordinator.isWatched(sessionId) ? 'Watched' : 'Inactive'
    },
    setFocusedSession: focusedSessionId => patch(state => ({ ...state, focusedSessionId })),
    settled: async () => {
      await Promise.all([...inflight.values()])
    },
    syncActiveSessions: (items, currentSessionId) => {
      const sessions = Object.fromEntries(items.map(item => [item.id, { ...item }]))

      // The active-list contract is requester-relative. Preserve that source
      // of truth, but tolerate an older gateway omitting `owned` for the
      // currently controlled session.
      if (currentSessionId && sessions[currentSessionId] && items.every(item => item.owned === undefined)) {
        sessions[currentSessionId] = { ...sessions[currentSessionId], owned: true }
      }

      const removedLayoutReferences = new Set<string>()

      patch(state => {
        const desired: Record<string, DesiredOutputWatch> = {}
        const effective: Record<string, EffectiveOutputSubscription> = {}

        for (const [key, watch] of Object.entries(state.desired)) {
          const runtime = items.find(item => durableKey(item) === key)

          if (!runtime) {
            removedLayoutReferences.add(watch.runtimeSessionId)

            continue
          }

          if (runtime.owned) {
            continue
          }

          if (runtime.watchable === false) {
            removedLayoutReferences.add(runtime.id)

            if (runtime.id !== watch.runtimeSessionId) {
              removedLayoutReferences.add(watch.runtimeSessionId)
            }

            continue
          }

          const remapped = runtime.id !== watch.runtimeSessionId

          const becameWatchable =
            state.sessions[watch.runtimeSessionId]?.watchable === false && runtime.watchable === true

          desired[key] = {
            ...watch,
            reason: remapped || becameWatchable ? undefined : watch.reason,
            runtimeSessionId: runtime.id,
            status: remapped || becameWatchable ? 'pending' : watch.status
          }

          if (!remapped && state.effective[runtime.id]?.sessionKey === key) {
            effective[runtime.id] = state.effective[runtime.id]
          }
        }

        const awaitingOwnedReattach =
          state.awaitingOwnedReattach && !(currentSessionId && sessions[currentSessionId]?.owned)

        const focusedSessionId =
          state.focusedSessionId && removedLayoutReferences.has(state.focusedSessionId)
            ? null
            : state.focusedSessionId

        return {
          ...state,
          awaitingOwnedReattach,
          controlledSessionId: currentSessionId,
          desired,
          effective,
          focusedSessionId,
          sessions
        }
      })

      for (const sessionId of removedLayoutReferences) {
        hooks.removeLayoutReference?.(sessionId)
      }

      const current = getOutputSubscriptionState()

      if (current.awaitingOwnedReattach) {
        return
      }

      for (const [key, watch] of Object.entries(current.desired)) {
        const item = sessions[watch.runtimeSessionId]

        if (watch.status === 'pending' && item?.watchable !== false && !item?.owned) {
          void subscribe(key, watch.runtimeSessionId)
        }
      }
    },
    takeControlSucceeded: async sessionId => {
      const before = getOutputSubscriptionState()
      const wasEffective = Boolean(before.effective[sessionId])
      const anchor = before.effective[sessionId]?.anchorSessionId ?? ''

      patch(state => {
        const key =
          state.effective[sessionId]?.sessionKey ||
          (state.sessions[sessionId] ? durableKey(state.sessions[sessionId]) : '')

        const desired = { ...state.desired }
        const effective = { ...state.effective }

        if (key) {
          delete desired[key]
        }

        delete effective[sessionId]

        const sessions = {
          ...state.sessions,
          ...(state.sessions[sessionId]
            ? { [sessionId]: { ...state.sessions[sessionId], owned: true, watchable: false } }
            : {})
        }

        return { ...state, controlledSessionId: sessionId, desired, effective, focusedSessionId: null, sessions }
      })

      if (wasEffective && anchor) {
        await rpc('session.output_unsubscribe', {
          subscriber_session_id: anchor,
          session_ids: [sessionId]
        })
      }
    },
    unwatch: async sessionId => {
      const state = getOutputSubscriptionState()
      const item = state.sessions[sessionId]
      const key = state.effective[sessionId]?.sessionKey || (item ? durableKey(item) : '')
      const anchor = state.effective[sessionId]?.anchorSessionId ?? ownedAnchors()[0] ?? ''

      if (!key) {
        return false
      }

      patch(current => {
        const desired = { ...current.desired }
        const effective = { ...current.effective }

        delete desired[key]

        for (const [runtimeId, subscription] of Object.entries(effective)) {
          if (subscription.sessionKey === key) {
            delete effective[runtimeId]
          }
        }

        return {
          ...current,
          desired,
          effective,
          focusedSessionId: current.focusedSessionId === sessionId ? null : current.focusedSessionId
        }
      })

      if (!anchor) {
        return true
      }

      const result = await rpc('session.output_unsubscribe', {
        subscriber_session_id: anchor,
        session_ids: [sessionId]
      })

      if (result === null) {
        patch(current => ({
          ...current,
          desired: { ...current.desired, [key]: state.desired[key] },
          effective: state.effective[sessionId]
            ? { ...current.effective, [sessionId]: state.effective[sessionId] }
            : current.effective
        }))

        return false
      }

      return true
    },
    watch: async sessionId => {
      const item = getOutputSubscriptionState().sessions[sessionId]

      if (!item || item.owned || item.watchable === false) {
        return false
      }

      const key = durableKey(item)

      patch(state => ({
        ...state,
        desired: {
          ...state.desired,
          [key]: { runtimeSessionId: sessionId, sessionKey: key, status: 'pending' }
        }
      }))

      return subscribe(key, sessionId)
    }
  }

  return coordinator
}

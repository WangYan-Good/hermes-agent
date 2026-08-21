import { atom } from 'nanostores'

import type { Msg } from '../types.js'

export const OUTPUT_ENTRY_LIMIT = 200
export const OUTPUT_BYTE_LIMIT = 64 * 1024
export const OUTPUT_SPLIT_MIN_COLS = 110

export type OutputEntryKind = 'message' | 'status' | 'subagent' | 'system' | 'tool'
export type OutputTerminalStatus = 'closed' | 'completed' | 'disconnected' | 'error' | 'interrupted'
export type OutputConflictDecision = 'keep-primary' | 'open-manager' | 'prioritize-candidate' | 'split'

export interface OutputEntry {
  complete: boolean
  id: string
  kind: OutputEntryKind
  label?: string
  text: string
  timestamp: number
  tone?: 'error' | 'info' | 'warn'
}
export interface OutputStream {
  bytes: number
  entries: OutputEntry[]
  hasDisplayOutput: boolean
  hasLifecycleEvent: boolean
  lastOutputAt: number
  model: string
  omitted: boolean
  preview: string
  producing: boolean
  sessionId: string
  sessionKey: string
  status: string
  title: string
  unreadCount: number
}
export interface OutputConflict {
  candidateSessionId: string
  episode: number
  primarySessionId: string
}
export interface OutputLayout {
  mode: 'single' | 'split'
  primarySessionId: null | string
  secondarySessionId: null | string
}
export interface OutputStreamsState {
  conflict: null | OutputConflict
  conflictHandled: boolean
  episode: number
  layout: OutputLayout
  streams: Record<string, OutputStream>
}
export type SessionTransitionKind = 'activate-live' | 'new-live' | 'recover' | 'replace' | 'resume'
export interface SessionTransition {
  kind: SessionTransitionKind
  nextSessionId: string
  previousSessionId: null | string
  sessionKey?: string
}
export interface SessionTransitionHooks {
  afterCommit: (transition: SessionTransition) => void
  beforeCommit: (transition: SessionTransition) => void
}

export class OutputSessionIdentityCollisionError extends Error {
  readonly code = 'OUTPUT_SESSION_IDENTITY_COLLISION'

  constructor(sessionId: string, expectedSessionKey: string, actualSessionKey: string) {
    super(
      'session identity collision for runtime ' + sessionId +
      ': expected ' + expectedSessionKey + ', found ' + actualSessionKey
    )
    this.name = 'OutputSessionIdentityCollisionError'
  }
}
export interface OutputEvent {
  payload?: Record<string, unknown>
  type: string
}
export interface ObserveOutputOptions {
  buffer: boolean
  now?: number
}

interface EventRule {
  complete: boolean
  kind: OutputEntryKind
  paints: boolean
  producing: boolean | null
  status: null | string
  terminal: boolean
}

const TERMINAL_STATUSES = new Set<OutputTerminalStatus>([
  'closed',
  'completed',
  'disconnected',
  'error',
  'interrupted'
])

const OMITTED_ENTRY_ID = 'omitted'
const OMITTED_TEXT = '[Earlier output omitted]'

const EVENT_RULES: Record<string, EventRule> = {
  'background.complete': {
    complete: true,
    kind: 'system',
    paints: true,
    producing: null,
    status: null,
    terminal: false
  },
  error: { complete: true, kind: 'system', paints: true, producing: false, status: 'error', terminal: true },
  'message.complete': {
    complete: true,
    kind: 'message',
    paints: true,
    producing: false,
    status: 'completed',
    terminal: true
  },
  'message.delta': {
    complete: false,
    kind: 'message',
    paints: true,
    producing: true,
    status: 'running',
    terminal: false
  },
  'message.interim': {
    complete: true,
    kind: 'message',
    paints: true,
    producing: true,
    status: 'running',
    terminal: false
  },
  'message.start': {
    complete: false,
    kind: 'status',
    paints: false,
    producing: true,
    status: 'running',
    terminal: false
  },
  'status.update': {
    complete: false,
    kind: 'status',
    paints: true,
    producing: true,
    status: 'running',
    terminal: false
  },
  'subagent.complete': {
    complete: true,
    kind: 'subagent',
    paints: true,
    producing: null,
    status: null,
    terminal: false
  },
  'subagent.progress': {
    complete: false,
    kind: 'subagent',
    paints: true,
    producing: null,
    status: null,
    terminal: false
  },
  'subagent.spawn_requested': {
    complete: false,
    kind: 'subagent',
    paints: true,
    producing: null,
    status: null,
    terminal: false
  },
  'subagent.start': { complete: false, kind: 'subagent', paints: true, producing: null, status: null, terminal: false },
  'tool.complete': { complete: true, kind: 'tool', paints: true, producing: null, status: null, terminal: false },
  'tool.progress': { complete: false, kind: 'tool', paints: true, producing: true, status: 'running', terminal: false },
  'tool.start': { complete: false, kind: 'tool', paints: true, producing: true, status: 'running', terminal: false }
}

const buildState = (): OutputStreamsState => ({
  conflict: null,
  conflictHandled: false,
  episode: 0,
  layout: { mode: 'single', primarySessionId: null, secondarySessionId: null },
  streams: {}
})

export const $outputStreams = atom<Record<string, OutputStream>>({})
export const $outputConflict = atom<null | OutputConflict>(null)
export const $outputLayout = atom<OutputLayout>(buildState().layout)

let state = buildState()
let entrySequence = 0
let hadMultipleProducers = false
let transportDisconnected = false

export const getOutputStreamsState = (): OutputStreamsState => state

export const getOutputSessionKey = (sessionId: null | string): null | string =>
  sessionId ? state.streams[sessionId]?.sessionKey || null : null

export const validateOutputPrimaryTransition = (transition: SessionTransition) => {
  if (transition.sessionKey) {
    assertOutputSessionProvenance(transition.nextSessionId, transition.sessionKey)
  }
}

export const captureOutputStreamsState = (): OutputStreamsState => structuredClone(state)

export const restoreOutputStreamsState = (snapshot: OutputStreamsState) => {
  state = structuredClone(snapshot)
  publish()
}

export const resetOutputStreams = () => {
  state = buildState()
  entrySequence = 0
  hadMultipleProducers = false
  transportDisconnected = false
  publish()
}

export const markOutputTransportDisconnected = () => {
  transportDisconnected = true

  const streams = Object.fromEntries(
    Object.entries(state.streams).map(([sessionId, stream]) => [
      sessionId,
      {
        ...stream,
        hasLifecycleEvent: true,
        producing: false,
        status: isTerminal(stream.status) ? stream.status : 'disconnected'
      }
    ])
  )

  state = { ...state, conflict: null, conflictHandled: false, streams }
  hadMultipleProducers = false
  publish()
}

export const markOutputTransportReady = () => {
  if (!transportDisconnected) {
    return false
  }

  transportDisconnected = false
  const primarySessionId = state.layout.primarySessionId

  const streams = Object.fromEntries(
    Object.entries(state.streams).map(([sessionId, stream]) => [
      sessionId,
      sessionId === primarySessionId
        ? stream
        : {
            ...stream,
            bytes: 0,
            entries: [],
            hasDisplayOutput: false,
            lastOutputAt: 0,
            omitted: false,
            preview: '',
            producing: false,
            unreadCount: 0
          }
    ])
  )

  state = { ...state, conflict: null, conflictHandled: false, streams }
  hadMultipleProducers = false
  publish()

  return true
}

export const removeOutputSession = (sessionId: string) => {
  const stream = state.streams[sessionId]

  if (!stream) {return}

  updateStream({ ...stream, hasLifecycleEvent: true, producing: false, status: 'closed', unreadCount: 0 })

  if (state.conflict?.candidateSessionId === sessionId || state.conflict?.primarySessionId === sessionId) {
    state = { ...state, conflict: null }
  }

  hadMultipleProducers = Object.values(state.streams).filter(item => item.producing).length >= 2

  if (!hadMultipleProducers) {
    state = { ...state, conflictHandled: false }
  }

  publish()
}

export const removeOutputLayoutReference = (sessionId: string) => {
  if (!sessionId) {return}

  const { primarySessionId, secondarySessionId } = state.layout
  const referencesLayout = primarySessionId === sessionId || secondarySessionId === sessionId

  const referencesConflict =
    state.conflict?.candidateSessionId === sessionId || state.conflict?.primarySessionId === sessionId

  if (!referencesLayout && !referencesConflict) {return}

  const remainingSessionId = referencesLayout
    ? primarySessionId === sessionId
      ? secondarySessionId
      : primarySessionId
    : primarySessionId

  state = {
    ...state,
    conflict: referencesConflict ? null : state.conflict,
    layout: referencesLayout
      ? { mode: 'single', primarySessionId: remainingSessionId, secondarySessionId: null }
      : state.layout
  }
  publish()
}

export const observeOutputEvent = (event: OutputEvent, sessionId: string, options: ObserveOutputOptions) => {
  const rule = EVENT_RULES[event.type]

  if (!rule || !sessionId) {return}

  const now = options.now ?? Date.now()
  const paints = rule.paints && (event.type !== 'message.delta' || Boolean(getString(event.payload, ['text'])))
  const stream = getOrCreateStream(sessionId)

  if (state.layout.primarySessionId == null && stream.status !== 'closed')
    {state = { ...state, layout: { ...state.layout, primarySessionId: sessionId } }}

  const terminal = isTerminal(stream.status)
  const startsNewRound = event.type === 'message.start'
  const reopensTerminal = startsNewRound && (stream.status === 'completed' || stream.status === 'interrupted')
  const nextStatus = getStatus(event.payload, rule.status)

  let nextStream: OutputStream = {
    ...stream,
    hasLifecycleEvent: stream.hasLifecycleEvent || nextStatus != null || rule.producing != null,
    hasDisplayOutput: stream.hasDisplayOutput || paints,
    lastOutputAt: paints ? now : stream.lastOutputAt,
    producing:
      rule.producing == null
        ? stream.producing
        : rule.terminal || !terminal || reopensTerminal
          ? rule.producing
          : stream.producing,
    status: nextStatus != null && (rule.terminal || !terminal || reopensTerminal) ? nextStatus : stream.status
  }

  if (paints && options.buffer) {
    nextStream = appendEntry(nextStream, makeEntry(event, rule, now), event.type, hasCompletionText(event.payload))
  }

  if (paints && sessionId !== state.layout.primarySessionId)
    {nextStream = { ...nextStream, unreadCount: nextStream.unreadCount + 1 }}

  updateStream(nextStream)
  updateConflict(sessionId, paints)
  publish()
}

export const syncOutputSessions = (items: readonly unknown[], currentSessionId: null | string) => {
  const detailsList = items.map(readSession)

  const sessionKeysByRuntime = new Map(
    Object.values(state.streams)
      .filter(stream => stream.sessionKey)
      .map(stream => [stream.sessionId, stream.sessionKey])
  )

  for (const details of detailsList) {
    if (!details.sessionId) {continue}

    if (details.sessionKey) {
      const existingSessionKey = sessionKeysByRuntime.get(details.sessionId)

      if (existingSessionKey && existingSessionKey !== details.sessionKey) {
        throw new OutputSessionIdentityCollisionError(details.sessionId, details.sessionKey, existingSessionKey)
      }

      sessionKeysByRuntime.set(details.sessionId, details.sessionKey)
    }
  }

  for (const details of detailsList) {
    if (!details.sessionId) {continue}

    let sessionId = details.sessionId

    if (details.sessionKey) {
      const previousRuntimeId = Object.values(state.streams).find(
        stream => stream.sessionKey === details.sessionKey && stream.sessionId !== details.sessionId
      )?.sessionId

      if (previousRuntimeId === currentSessionId) {
        sessionId = previousRuntimeId
      } else if (previousRuntimeId) {
        remapOutputSession(previousRuntimeId, details.sessionId, details.sessionKey)
      }
    }

    const stream = getOrCreateStream(sessionId)
    const sessionStatus = normalizeOutputStatus(details.status)
    const status = stream.producing ? stream.status : (sessionStatus ?? stream.status)
    updateStream({
      ...stream,
      model: details.model ?? stream.model,
      preview: stream.producing ? stream.preview : (details.preview ?? stream.preview),
      sessionKey: details.sessionKey ?? stream.sessionKey,
      status:
        !stream.producing && stream.status !== 'disconnected' && isTerminal(stream.status) && !isTerminal(status)
          ? stream.status
          : status,
      title: details.title ?? stream.title
    })
  }

  if (state.layout.primarySessionId == null && currentSessionId) {
    const current = getOrCreateStream(currentSessionId)

    if (current.status !== 'closed')
      {state = { ...state, layout: { ...state.layout, primarySessionId: currentSessionId } }}
  }

  publish()
}

export const resolveOutputConflict = (decision: OutputConflictDecision, resolvedConflict?: OutputConflict) => {
  if (resolvedConflict && state.conflict && state.conflict.episode !== resolvedConflict.episode) {return}
  const conflict = state.conflict ?? resolvedConflict

  if (!conflict) {return}
  let layout = state.layout

  if (decision === 'prioritize-candidate')
    {layout = { mode: 'single', primarySessionId: conflict.candidateSessionId, secondarySessionId: null }}
  else if (decision === 'split')
    {layout = {
      mode: 'split',
      primarySessionId: conflict.primarySessionId,
      secondarySessionId: conflict.candidateSessionId
    }}

  state = { ...state, conflict: null, conflictHandled: hadMultipleProducers, layout }
  publish()
}

export const setSecondaryOutput = (sessionId: string) => {
  if (!sessionId) {return}
  const stream = getOrCreateStream(sessionId)

  if (stream.status === 'closed') {return}
  const primarySessionId = state.layout.primarySessionId ?? sessionId
  state =
    primarySessionId === sessionId
      ? { ...state, layout: { mode: 'single', primarySessionId, secondarySessionId: null } }
      : { ...state, layout: { mode: 'split', primarySessionId, secondarySessionId: sessionId } }
  publish()
}

export const exitOutputSplit = () => {
  state = {
    ...state,
    layout: { mode: 'single', primarySessionId: state.layout.primarySessionId, secondarySessionId: null }
  }
  publish()
}

export const capturePrimaryOutputSnapshot = (
  sessionId: string,
  title: string,
  status: string,
  history: readonly Msg[],
  streamingText: string
) => {
  if (!sessionId) {
    return
  }

  const now = Date.now()
  const candidateEntries = snapshotEntries(history, streamingText, now)
  const stream = getOrCreateStream(sessionId)
  const terminal = isTerminal(stream.status)
  const entries = terminal ? mergeSnapshotEntries(stream.entries, candidateEntries) : candidateEntries
  const candidateStatus = normalizeOutputStatus(status) || stream.status

  const snapshot = limitEntries({
    ...stream,
    bytes: 0,
    entries,
    hasDisplayOutput: stream.hasDisplayOutput || entries.length > 0,
    lastOutputAt: entries.length ? now : stream.lastOutputAt,
    omitted: terminal ? stream.omitted : false,
    preview: candidateEntries.at(-1)?.text ?? stream.preview,
    status: terminal ? stream.status : candidateStatus,
    title: title || stream.title,
    unreadCount: 0
  })

  updateStream(snapshot)
  publish()
}

export const mergeWatchedOutputSnapshot = (
  sessionId: string,
  sessionKey: string,
  title: string,
  status: string,
  history: readonly Msg[]
) => {
  if (!sessionId) {return}

  const stream = getOrCreateStream(sessionId)

  if (stream.sessionKey && sessionKey && stream.sessionKey !== sessionKey) {
    throw new OutputSessionIdentityCollisionError(sessionId, sessionKey, stream.sessionKey)
  }

  const now = Date.now()
  const candidateEntries = snapshotEntries(history, '', now)
  const entries = mergeSnapshotEntries(candidateEntries, stream.entries)
  const candidateStatus = normalizeOutputStatus(status) || stream.status

  const snapshot = limitEntries({
    ...stream,
    bytes: 0,
    entries,
    hasDisplayOutput: stream.hasDisplayOutput || entries.length > 0,
    lastOutputAt: entries.length ? Math.max(stream.lastOutputAt, now) : stream.lastOutputAt,
    preview: stream.preview || candidateEntries.at(-1)?.text || '',
    sessionKey: sessionKey || stream.sessionKey,
    status: stream.producing || isTerminal(stream.status) ? stream.status : candidateStatus,
    title: title || stream.title
  })

  updateStream(snapshot)
  publish()
}

export const commitOutputPrimaryTransition = (transition: SessionTransition) => {
  validateOutputPrimaryTransition(transition)

  if (transition.kind === 'recover' && transition.sessionKey) {
    const previousRuntimeId = Object.values(state.streams).find(
      stream => stream.sessionKey === transition.sessionKey && stream.sessionId !== transition.nextSessionId
    )?.sessionId

    if (previousRuntimeId) {
      remapOutputSession(previousRuntimeId, transition.nextSessionId, transition.sessionKey)
    }
  }

  const next = getOrCreateStream(transition.nextSessionId)
  const previousSessionId = transition.previousSessionId

  const nextWithIdentity = {
    ...next,
    sessionKey: transition.sessionKey ?? next.sessionKey,
    unreadCount: 0
  }

  if (transition.kind === 'recover') {
    updateStream(nextWithIdentity)
    state = {
      ...state,
      conflict: null,
      layout: state.layout.primarySessionId
        ? state.layout
        : { mode: 'single', primarySessionId: transition.nextSessionId, secondarySessionId: null }
    }
    publish()

    return
  }

  updateStream(nextWithIdentity)

  const preservesLivePair = transition.kind === 'activate-live' || transition.kind === 'new-live'

  const partnerSessionId = preservesLivePair
    ? selectTransitionPartner(transition.nextSessionId, previousSessionId)
    : null

  state = {
    ...state,
    conflict: null,
    layout: {
      mode: partnerSessionId ? 'split' : 'single',
      primarySessionId: transition.nextSessionId,
      secondarySessionId: partnerSessionId
    }
  }
  publish()
}

function assertOutputSessionProvenance(sessionId: string, sessionKey: string) {
  const destinationSessionKey = state.streams[sessionId]?.sessionKey

  if (destinationSessionKey && destinationSessionKey !== sessionKey) {
    throw new OutputSessionIdentityCollisionError(sessionId, sessionKey, destinationSessionKey)
  }
}

function remapOutputSession(previousSessionId: string, nextSessionId: string, sessionKey: string) {
  const previous = state.streams[previousSessionId]

  if (!previous || previousSessionId === nextSessionId) {return}

  assertOutputSessionProvenance(nextSessionId, sessionKey)
  const destination = state.streams[nextSessionId]
  const destinationEntryIds = new Set(previous.entries.map(entry => entry.id))

  const entries = [
    ...previous.entries,
    ...(destination?.entries.filter(entry => !destinationEntryIds.has(entry.id)) ?? [])
  ]

  const destinationHasLifecycleEvent = Boolean(destination?.hasLifecycleEvent)

  const stream = limitEntries({
    ...previous,
    entries,
    hasDisplayOutput: previous.hasDisplayOutput || Boolean(destination?.hasDisplayOutput),
    hasLifecycleEvent: previous.hasLifecycleEvent || destinationHasLifecycleEvent,
    lastOutputAt: Math.max(previous.lastOutputAt, destination?.lastOutputAt ?? 0),
    model: destination?.model || previous.model,
    omitted: previous.omitted || Boolean(destination?.omitted),
    preview: destination?.preview || previous.preview,
    producing: destinationHasLifecycleEvent ? destination!.producing : previous.producing,
    sessionId: nextSessionId,
    sessionKey,
    status: destinationHasLifecycleEvent ? destination!.status : previous.status,
    title: destination && destination.title !== nextSessionId ? destination.title : previous.title,
    unreadCount: Math.max(previous.unreadCount, destination?.unreadCount ?? 0)
  })

  const streams = { ...state.streams }
  delete streams[previousSessionId]
  streams[nextSessionId] = stream
  const remap = (sessionId: null | string) => sessionId === previousSessionId ? nextSessionId : sessionId

  const conflict = state.conflict
    ? {
        ...state.conflict,
        candidateSessionId: remap(state.conflict.candidateSessionId)!,
        primarySessionId: remap(state.conflict.primarySessionId)!
      }
    : null

  state = {
    ...state,
    conflict: conflict?.candidateSessionId === conflict?.primarySessionId ? null : conflict,
    layout: {
      ...state.layout,
      primarySessionId: remap(state.layout.primarySessionId),
      secondarySessionId: remap(state.layout.secondarySessionId)
    },
    streams
  }
}

function getOrCreateStream(sessionId: string): OutputStream {
  const existing = state.streams[sessionId]

  if (existing) {return existing}

  const stream: OutputStream = {
    bytes: 0,
    entries: [],
    hasDisplayOutput: false,
    lastOutputAt: 0,
    hasLifecycleEvent: false,
    model: '',
    omitted: false,
    preview: '',
    producing: false,
    sessionId,
    sessionKey: '',
    status: 'idle',
    title: sessionId,
    unreadCount: 0
  }

  updateStream(stream)

  return stream
}

function updateStream(stream: OutputStream) {
  state = { ...state, streams: { ...state.streams, [stream.sessionId]: stream } }
}

function selectTransitionPartner(nextSessionId: string, previousSessionId: null | string): null | string {
  if (
    previousSessionId !== nextSessionId &&
    isLayoutEligibleSession(previousSessionId, state.streams)
  ) {
    return previousSessionId
  }

  for (const sessionId of [state.layout.primarySessionId, state.layout.secondarySessionId]) {
    if (sessionId !== nextSessionId && isLayoutEligibleSession(sessionId, state.streams)) {
      return sessionId
    }
  }

  return null
}

function updateConflict(sessionId: string, paints: boolean) {
  const producing = Object.values(state.streams).filter(stream => stream.producing)

  if (producing.length < 2) {
    if (hadMultipleProducers) {
      state = { ...state, conflict: null, conflictHandled: false }
      hadMultipleProducers = false
    }

    return
  }

  hadMultipleProducers = true

  if (!paints || state.conflict || state.conflictHandled) {return}
  const primarySessionId = state.layout.primarySessionId

  if (!primarySessionId || primarySessionId === sessionId) {return}
  const episode = state.episode + 1
  state = { ...state, conflict: { candidateSessionId: sessionId, episode, primarySessionId }, episode }
}

function appendEntry(
  stream: OutputStream,
  entry: OutputEntry,
  eventType: string,
  completionHasText: boolean
): OutputStream {
  const entries = [...stream.entries]
  const last = entries.at(-1)

  if (eventType === 'message.delta' && last?.kind === 'message' && !last.complete) {
    entries[entries.length - 1] = { ...last, text: `${last.text}${entry.text}`, timestamp: entry.timestamp }
  } else if (
    (eventType === 'message.interim' || eventType === 'message.complete') &&
    last?.kind === 'message' &&
    !last.complete
  ) {
    entries[entries.length - 1] = {
      ...last,
      complete: true,
      text: eventType === 'message.complete' && !completionHasText ? last.text : entry.text,
      timestamp: entry.timestamp
    }
  } else if (eventType !== 'message.complete' || completionHasText) {
    entries.push(entry)
  }

  return limitEntries({
    ...stream,
    entries,
    preview: completionHasText || eventType !== 'message.complete' ? entry.text || stream.preview : stream.preview
  })
}

function snapshotEntries(history: readonly Msg[], streamingText: string, timestamp: number): OutputEntry[] {
  const entries: OutputEntry[] = []
  let sequence = 0

  for (const item of history) {
    const text = item.text.trim()

    if (!text) {
      continue
    }

    sequence += 1
    entries.push({
      complete: true,
      id: `snapshot-${timestamp}-${sequence}`,
      kind: item.role === 'assistant' || item.role === 'user' ? 'message' : 'system',
      text,
      timestamp
    })
  }

  const tail = streamingText.trim()

  if (!tail) {
    return entries
  }

  sequence += 1
  entries.push({ complete: false, id: `snapshot-${timestamp}-${sequence}`, kind: 'message', text: tail, timestamp })

  return entries
}

function mergeSnapshotEntries(existing: readonly OutputEntry[], candidate: readonly OutputEntry[]): OutputEntry[] {
  const durable = existing.filter(entry => entry.id !== OMITTED_ENTRY_ID)

  if (!durable.length) {
    return [...candidate]
  }

  if (!candidate.length) {
    return [...durable]
  }

  const candidateKeys = new Set(candidate.map(snapshotEntryKey))
  const candidateCoversDurable = durable.every(entry => candidateKeys.has(snapshotEntryKey(entry)))

  if (candidateCoversDurable && candidate.length >= durable.length) {
    return [...candidate]
  }

  const merged = [...durable]
  const mergedKeys = new Set(durable.map(snapshotEntryKey))

  for (const entry of candidate) {
    const key = snapshotEntryKey(entry)

    if (!mergedKeys.has(key)) {
      merged.push(entry)
      mergedKeys.add(key)
    }
  }

  return merged
}

function snapshotEntryKey(entry: OutputEntry): string {
  return `${entry.kind}\u0000${entry.text}`
}

function limitEntries(stream: OutputStream): OutputStream {
  let entries = stream.entries.filter(entry => entry.id !== OMITTED_ENTRY_ID)
  let omitted = stream.omitted
  let bytes = entries.reduce((total, entry) => total + entryBytes(entry), 0)

  while (entries.length > OUTPUT_ENTRY_LIMIT || bytes > OUTPUT_BYTE_LIMIT) {
    const removed = entries.shift()

    if (!removed) {break}
    bytes -= entryBytes(removed)
    omitted = true
  }

  if (omitted) {
    const marker: OutputEntry = {
      complete: true,
      id: OMITTED_ENTRY_ID,
      kind: 'system',
      text: OMITTED_TEXT,
      timestamp: entries[0]?.timestamp ?? 0,
      tone: 'warn'
    }

    entries.unshift(marker)
    bytes += entryBytes(marker)

    while (entries.length > OUTPUT_ENTRY_LIMIT || bytes > OUTPUT_BYTE_LIMIT) {
      const removed = entries.splice(1, 1)[0]

      if (!removed) {break}
      bytes -= entryBytes(removed)
    }
  }

  return { ...stream, bytes: Math.max(0, bytes), entries, omitted }
}

function makeEntry(event: OutputEvent, rule: EventRule, timestamp: number): OutputEntry {
  const payload = event.payload ?? {}
  const label = getString(payload, ['name', 'label', 'goal', 'title'])
  entrySequence += 1

  return {
    complete: rule.complete,
    id: `${event.type}-${timestamp}-${entrySequence}`,
    kind: rule.kind,
    ...(label ? { label } : {}),
    text: getText(payload, event.type),
    timestamp,
    ...(event.type === 'error' ? { tone: 'error' as const } : {})
  }
}

function getStatus(payload: Record<string, unknown> | undefined, fallback: null | string): null | string {
  return normalizeOutputStatus(getString(payload, ['status']) ?? fallback)
}

function normalizeOutputStatus(status: null | string | undefined): null | string {
  if (!status) {
    return null
  }

  return status === 'complete' ? 'completed' : status
}

function getText(payload: Record<string, unknown>, fallback: string): string {
  return (
    getString(payload, ['text', 'rendered', 'message', 'preview', 'summary', 'status', 'name', 'title']) ?? fallback
  )
}

function hasCompletionText(payload: Record<string, unknown> | undefined): boolean {
  return Boolean(getString(payload, ['text', 'rendered']))
}

function getString(payload: Record<string, unknown> | undefined, keys: readonly string[]): string | undefined {
  if (!payload) {return undefined}

  for (const key of keys) {
    const value = payload[key]

    if (typeof value === 'string' && value) {return value}
  }

  return undefined
}

function entryBytes(entry: OutputEntry): number {
  return new TextEncoder().encode(`${entry.label ?? ''}${entry.text}`).byteLength
}

function isTerminal(status: string): status is OutputTerminalStatus {
  return TERMINAL_STATUSES.has(status as OutputTerminalStatus)
}

function isLayoutEligibleSession(
  sessionId: null | string,
  streams: Record<string, OutputStream>
): sessionId is string {
  return Boolean(sessionId && streams[sessionId] && streams[sessionId].status !== 'closed')
}

function normalizeOutputLayout(
  layout: OutputLayout,
  streams: Record<string, OutputStream>
): OutputLayout {
  const primarySessionId = isLayoutEligibleSession(layout.primarySessionId, streams)
    ? layout.primarySessionId
    : null

  const secondarySessionId = isLayoutEligibleSession(layout.secondarySessionId, streams) &&
    layout.secondarySessionId !== primarySessionId
    ? layout.secondarySessionId
    : null

  if (primarySessionId && secondarySessionId) {
    return { mode: 'split', primarySessionId, secondarySessionId }
  }

  const remainingSessionId = primarySessionId ?? secondarySessionId

  return { mode: 'single', primarySessionId: remainingSessionId, secondarySessionId: null }
}

function readSession(item: unknown): {
  model?: string
  preview?: string
  sessionId?: string
  sessionKey?: string
  status?: string
  title?: string
} {
  if (!item || typeof item !== 'object') {return {}}
  const record = item as Record<string, unknown>

  return {
    model: getString(record, ['model']),
    preview: getString(record, ['preview']),
    sessionId: getString(record, ['sessionId', 'session_id', 'id']),
    sessionKey: getString(record, ['sessionKey', 'session_key', 'stored_session_id']),
    status: getString(record, ['status']),
    title: getString(record, ['title', 'name', 'label'])
  }
}

function publish() {
  state = { ...state, layout: normalizeOutputLayout(state.layout, state.streams) }
  $outputStreams.set(state.streams)
  $outputConflict.set(state.conflict)
  $outputLayout.set(state.layout)
}

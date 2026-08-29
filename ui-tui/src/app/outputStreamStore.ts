import type { Msg } from '../types.js'

export const OUTPUT_ENTRY_LIMIT = 200
export const OUTPUT_BYTE_LIMIT = 64 * 1024
export type OutputEntryKind = 'message' | 'status' | 'subagent' | 'system' | 'tool'
export type OutputTerminalStatus = 'closed' | 'completed' | 'disconnected' | 'error' | 'interrupted'

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
}
export interface OutputStreamsState {
  activeSessionId: null | string
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
      'session identity collision for runtime ' +
        sessionId +
        ': expected ' +
        expectedSessionKey +
        ', found ' +
        actualSessionKey
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

const TERMINAL_STATUSES = new Set<OutputTerminalStatus>(['closed', 'completed', 'disconnected', 'error', 'interrupted'])

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
  activeSessionId: null,
  streams: {}
})

let state = buildState()
let entrySequence = 0
let transportDisconnected = false

export const getOutputStreamsState = (): OutputStreamsState => state

export const getOutputSessionKey = (sessionId: null | string): null | string =>
  sessionId ? state.streams[sessionId]?.sessionKey || null : null

export const validateActiveOutputTransition = (transition: SessionTransition) => {
  if (transition.sessionKey) {
    assertOutputSessionProvenance(transition.nextSessionId, transition.sessionKey)
  }
}

export const captureOutputStreamsState = (): OutputStreamsState => structuredClone(state)

export const restoreOutputStreamsState = (snapshot: OutputStreamsState) => {
  state = structuredClone(snapshot)
}

export const resetOutputStreams = () => {
  state = buildState()
  entrySequence = 0
  transportDisconnected = false
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

  state = { ...state, streams }
}

export const markOutputTransportReady = () => {
  if (!transportDisconnected) {
    return false
  }

  transportDisconnected = false
  const activeSessionId = state.activeSessionId

  const streams = Object.fromEntries(
    Object.entries(state.streams).map(([sessionId, stream]) => [
      sessionId,
      sessionId === activeSessionId
        ? stream
        : {
            ...stream,
            bytes: 0,
            entries: [],
            hasDisplayOutput: false,
            lastOutputAt: 0,
            omitted: false,
            preview: '',
            producing: false
          }
    ])
  )

  state = { ...state, streams }

  return true
}

export const removeOutputSession = (sessionId: string) => {
  const stream = state.streams[sessionId]

  if (!stream) {
    return
  }

  updateStream({ ...stream, hasLifecycleEvent: true, producing: false, status: 'closed' })
}

export const observeOutputEvent = (event: OutputEvent, sessionId: string, options: ObserveOutputOptions) => {
  const rule = EVENT_RULES[event.type]

  if (!rule || !sessionId) {
    return
  }

  const now = options.now ?? Date.now()
  const paints = rule.paints && (event.type !== 'message.delta' || Boolean(getString(event.payload, ['text'])))
  const stream = getOrCreateStream(sessionId)

  if (state.activeSessionId == null && stream.status !== 'closed' && !options.buffer) {
    state = { ...state, activeSessionId: sessionId }
  }

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

  updateStream(nextStream)
}

export const syncOutputSessions = (items: readonly unknown[], currentSessionId: null | string) => {
  const detailsList = items.map(readSession)

  const sessionKeysByRuntime = new Map(
    Object.values(state.streams)
      .filter(stream => stream.sessionKey)
      .map(stream => [stream.sessionId, stream.sessionKey])
  )

  for (const details of detailsList) {
    if (!details.sessionId) {
      continue
    }

    if (details.sessionKey) {
      const existingSessionKey = sessionKeysByRuntime.get(details.sessionId)

      if (existingSessionKey && existingSessionKey !== details.sessionKey) {
        throw new OutputSessionIdentityCollisionError(details.sessionId, details.sessionKey, existingSessionKey)
      }

      sessionKeysByRuntime.set(details.sessionId, details.sessionKey)
    }
  }

  for (const details of detailsList) {
    if (!details.sessionId) {
      continue
    }

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

  if (currentSessionId) {
    const current = getOrCreateStream(currentSessionId)

    if (current.status !== 'closed' && state.activeSessionId !== currentSessionId) {
      state = { ...state, activeSessionId: currentSessionId }
    }
  }
}

export const captureActiveOutputSnapshot = (
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
    title: title || stream.title
  })

  updateStream(snapshot)
}

export const commitActiveOutputTransition = (transition: SessionTransition) => {
  validateActiveOutputTransition(transition)

  if (transition.kind === 'recover' && transition.sessionKey) {
    const previousRuntimeId = Object.values(state.streams).find(
      stream => stream.sessionKey === transition.sessionKey && stream.sessionId !== transition.nextSessionId
    )?.sessionId

    if (previousRuntimeId) {
      remapOutputSession(previousRuntimeId, transition.nextSessionId, transition.sessionKey)
    }
  }

  const next = getOrCreateStream(transition.nextSessionId)

  const nextWithIdentity = {
    ...next,
    sessionKey: transition.sessionKey ?? next.sessionKey
  }

  if (transition.kind === 'recover') {
    updateStream(nextWithIdentity)
    state = { ...state, activeSessionId: transition.nextSessionId }

    return
  }

  updateStream(nextWithIdentity)

  state = { ...state, activeSessionId: transition.nextSessionId }
}

function assertOutputSessionProvenance(sessionId: string, sessionKey: string) {
  const destinationSessionKey = state.streams[sessionId]?.sessionKey

  if (destinationSessionKey && destinationSessionKey !== sessionKey) {
    throw new OutputSessionIdentityCollisionError(sessionId, sessionKey, destinationSessionKey)
  }
}

function remapOutputSession(previousSessionId: string, nextSessionId: string, sessionKey: string) {
  const previous = state.streams[previousSessionId]

  if (!previous || previousSessionId === nextSessionId) {
    return
  }

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
    title: destination && destination.title !== nextSessionId ? destination.title : previous.title
  })

  const streams = { ...state.streams }
  delete streams[previousSessionId]
  streams[nextSessionId] = stream
  state = {
    ...state,
    activeSessionId: state.activeSessionId === previousSessionId ? nextSessionId : state.activeSessionId,
    streams
  }
}

function getOrCreateStream(sessionId: string): OutputStream {
  const existing = state.streams[sessionId]

  if (existing) {
    return existing
  }

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
    title: sessionId
  }

  updateStream(stream)

  return stream
}

function updateStream(stream: OutputStream) {
  state = { ...state, streams: { ...state.streams, [stream.sessionId]: stream } }
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

    if (!removed) {
      break
    }
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

      if (!removed) {
        break
      }
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
  if (!payload) {
    return undefined
  }

  for (const key of keys) {
    const value = payload[key]

    if (typeof value === 'string' && value) {
      return value
    }
  }

  return undefined
}

function entryBytes(entry: OutputEntry): number {
  return new TextEncoder().encode(`${entry.label ?? ''}${entry.text}`).byteLength
}

function isTerminal(status: string): status is OutputTerminalStatus {
  return TERMINAL_STATUSES.has(status as OutputTerminalStatus)
}

function readSession(item: unknown): {
  model?: string
  preview?: string
  sessionId?: string
  sessionKey?: string
  status?: string
  title?: string
} {
  if (!item || typeof item !== 'object') {
    return {}
  }
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

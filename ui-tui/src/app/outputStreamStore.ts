import { atom } from 'nanostores'

export const OUTPUT_ENTRY_LIMIT = 200
export const OUTPUT_BYTE_LIMIT = 64 * 1024
export const OUTPUT_SPLIT_MIN_COLS = 110

export type OutputEntryKind = 'message' | 'status' | 'subagent' | 'system' | 'tool'
export type OutputTerminalStatus = 'closed' | 'completed' | 'disconnected' | 'error'
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
  lastOutputAt: number
  omitted: boolean
  preview: string
  producing: boolean
  sessionId: string
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

const TERMINAL_STATUSES = new Set<OutputTerminalStatus>(['closed', 'completed', 'disconnected', 'error'])
const OMITTED_ENTRY_ID = 'omitted'
const OMITTED_TEXT = '[Earlier output omitted]'

const EVENT_RULES: Record<string, EventRule> = {
  'background.complete': { complete: true, kind: 'system', paints: true, producing: null, status: null, terminal: false },
  error: { complete: true, kind: 'system', paints: true, producing: false, status: 'error', terminal: true },
  'message.complete': { complete: true, kind: 'message', paints: true, producing: false, status: 'completed', terminal: true },
  'message.delta': { complete: false, kind: 'message', paints: true, producing: true, status: 'running', terminal: false },
  'message.interim': { complete: true, kind: 'message', paints: true, producing: true, status: 'running', terminal: false },
  'message.start': { complete: false, kind: 'status', paints: false, producing: true, status: 'running', terminal: false },
  'status.update': { complete: false, kind: 'status', paints: true, producing: true, status: 'running', terminal: false },
  'subagent.complete': { complete: true, kind: 'subagent', paints: true, producing: null, status: null, terminal: false },
  'subagent.progress': { complete: false, kind: 'subagent', paints: true, producing: null, status: null, terminal: false },
  'subagent.spawn_requested': { complete: false, kind: 'subagent', paints: true, producing: null, status: null, terminal: false },
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

export const getOutputStreamsState = (): OutputStreamsState => state

export const resetOutputStreams = () => {
  state = buildState()
  entrySequence = 0
  hadMultipleProducers = false
  publish()
}

export const observeOutputEvent = (event: OutputEvent, sessionId: string, options: ObserveOutputOptions) => {
  const rule = EVENT_RULES[event.type]
  if (!rule || !sessionId) return

  const now = options.now ?? Date.now()
  const stream = getOrCreateStream(sessionId)
  if (state.layout.primarySessionId == null) {
    state = { ...state, layout: { ...state.layout, primarySessionId: sessionId } }
  }

  const terminal = isTerminal(stream.status)
  const startsNewRound = event.type === 'message.start'
  const nextStatus = getStatus(event.payload, rule.status)
  let nextStream: OutputStream = {
    ...stream,
    hasDisplayOutput: stream.hasDisplayOutput || rule.paints,
    lastOutputAt: rule.paints ? now : stream.lastOutputAt,
    producing: rule.producing == null ? stream.producing : (!terminal || startsNewRound ? rule.producing : stream.producing),
    status: nextStatus != null && (rule.terminal || !terminal) ? nextStatus : stream.status
  }

  if (rule.paints && options.buffer) nextStream = appendEntry(nextStream, makeEntry(event, rule, now), event.type)
  if (rule.paints && sessionId !== state.layout.primarySessionId) {
    nextStream = { ...nextStream, unreadCount: nextStream.unreadCount + 1 }
  }

  updateStream(nextStream)
  updateConflict(sessionId, rule.paints)
  publish()
}

export const syncOutputSessions = (items: readonly unknown[], currentSessionId: null | string) => {
  for (const item of items) {
    const details = readSession(item)
    if (!details.sessionId) continue
    const stream = getOrCreateStream(details.sessionId)
    const status = details.status ?? stream.status
    updateStream({
      ...stream,
      status: isTerminal(stream.status) && !isTerminal(status) ? stream.status : status,
      title: details.title ?? stream.title
    })
  }
  if (state.layout.primarySessionId == null && currentSessionId) {
    getOrCreateStream(currentSessionId)
    state = { ...state, layout: { ...state.layout, primarySessionId: currentSessionId } }
  }
  publish()
}

export const resolveOutputConflict = (decision: OutputConflictDecision) => {
  const conflict = state.conflict
  if (!conflict) return

  let layout = state.layout
  if (decision === 'prioritize-candidate') {
    layout = { mode: 'single', primarySessionId: conflict.candidateSessionId, secondarySessionId: null }
  } else if (decision === 'split') {
    layout = { mode: 'split', primarySessionId: conflict.primarySessionId, secondarySessionId: conflict.candidateSessionId }
  }
  state = { ...state, conflict: null, conflictHandled: hadMultipleProducers, layout }
  publish()
}

export const setSecondaryOutput = (sessionId: string) => {
  if (!sessionId) return
  getOrCreateStream(sessionId)
  const primarySessionId = state.layout.primarySessionId ?? sessionId
  if (primarySessionId === sessionId) {
    state = { ...state, layout: { mode: 'single', primarySessionId, secondarySessionId: null } }
  } else {
    state = { ...state, layout: { mode: 'split', primarySessionId, secondarySessionId: sessionId } }
  }
  publish()
}

export const exitOutputSplit = () => {
  state = { ...state, layout: { mode: 'single', primarySessionId: state.layout.primarySessionId, secondarySessionId: null } }
  publish()
}

function getOrCreateStream(sessionId: string): OutputStream {
  const existing = state.streams[sessionId]
  if (existing) return existing
  const stream: OutputStream = {
    bytes: 0,
    entries: [],
    hasDisplayOutput: false,
    lastOutputAt: 0,
    omitted: false,
    preview: '',
    producing: false,
    sessionId,
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
  if (!paints || state.conflict || state.conflictHandled) return

  const primarySessionId = state.layout.primarySessionId
  if (!primarySessionId || primarySessionId === sessionId) return

  const episode = state.episode + 1
  state = { ...state, conflict: { candidateSessionId: sessionId, episode, primarySessionId }, episode }
}

function appendEntry(stream: OutputStream, entry: OutputEntry, eventType: string): OutputStream {
  const entries = [...stream.entries]
  const last = entries.at(-1)
  if (eventType === 'message.delta' && last?.kind === 'message' && !last.complete) {
    entries[entries.length - 1] = { ...last, text: `${last.text}${entry.text}`, timestamp: entry.timestamp }
  } else if (eventType === 'message.interim' && last?.kind === 'message' && !last.complete) {
    entries[entries.length - 1] = { ...last, complete: true, text: entry.text, timestamp: entry.timestamp }
  } else {
    entries.push(entry)
  }
  return limitEntries({ ...stream, entries, preview: entry.text || stream.preview })
}

function limitEntries(stream: OutputStream): OutputStream {
  let entries = stream.entries.filter(entry => entry.id !== OMITTED_ENTRY_ID)
  let omitted = stream.omitted
  let bytes = entries.reduce((total, entry) => total + entryBytes(entry), 0)
  while (entries.length > OUTPUT_ENTRY_LIMIT || bytes > OUTPUT_BYTE_LIMIT) {
    const removed = entries.shift()
    if (!removed) break
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
      if (!removed) break
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
  return getString(payload, ['status']) ?? fallback
}

function getText(payload: Record<string, unknown>, fallback: string): string {
  return getString(payload, ['text', 'message', 'preview', 'summary', 'status', 'name', 'title']) ?? fallback
}

function getString(payload: Record<string, unknown> | undefined, keys: readonly string[]): string | undefined {
  if (!payload) return undefined
  for (const key of keys) {
    const value = payload[key]
    if (typeof value === 'string' && value) return value
  }
  return undefined
}

function entryBytes(entry: OutputEntry): number {
  return new TextEncoder().encode(`${entry.label ?? ''}${entry.text}`).byteLength
}

function isTerminal(status: string): status is OutputTerminalStatus {
  return TERMINAL_STATUSES.has(status as OutputTerminalStatus)
}

function readSession(item: unknown): { sessionId?: string; status?: string; title?: string } {
  if (!item || typeof item !== 'object') return {}
  const record = item as Record<string, unknown>
  return {
    sessionId: getString(record, ['sessionId', 'session_id', 'id']),
    status: getString(record, ['status']),
    title: getString(record, ['title', 'name', 'label'])
  }
}

function publish() {
  $outputStreams.set(state.streams)
  $outputConflict.set(state.conflict)
  $outputLayout.set(state.layout)
}

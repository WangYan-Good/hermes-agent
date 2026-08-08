import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { type ReactNode, useMemo } from 'react'

import {
  $outputLayout,
  $outputStreams,
  OUTPUT_SPLIT_MIN_COLS,
  type OutputEntry,
  type OutputLayout,
  type OutputStream
} from '../app/outputStreamStore.js'
import { $uiTheme } from '../app/uiStore.js'
import type { Theme } from '../theme.js'

export type OutputPaneMode = 'split' | 'tabs'
export const READONLY_OUTPUT_RENDER_LIMIT = 40

export const outputPaneMode = (cols: number): OutputPaneMode => (cols >= OUTPUT_SPLIT_MIN_COLS ? 'split' : 'tabs')

export const outputPaneWidths = (cols: number) => {
  const usable = Math.max(2, cols - 1)
  const primary = Math.floor(usable / 2)

  return { primary, secondary: usable - primary }
}

export interface ReadonlyOutputPaneProps {
  onFocus: () => void
  stream: OutputStream
  t: Theme
  width: number
}

const entryText = (entry: OutputEntry) => (entry.label ? entry.label + ': ' + entry.text : entry.text)

export function readonlyOutputTail(
  entries: readonly OutputEntry[],
  omitted: boolean,
  limit = READONLY_OUTPUT_RENDER_LIMIT
): OutputEntry[] {
  const safeLimit = Math.max(1, limit)
  const marker = entries.find(entry => entry.id === 'omitted')
  const content = entries.filter(entry => entry.id !== 'omitted')
  const contentLimit = omitted ? Math.max(0, safeLimit - 1) : safeLimit
  const tail = contentLimit > 0 ? content.slice(-contentLimit) : []

  return marker ? [marker, ...tail] : tail
}

export function ReadonlyOutputPane({ onFocus, stream, t, width }: ReadonlyOutputPaneProps) {
  const entries = useMemo(
    () => readonlyOutputTail(stream.entries, stream.omitted),
    [stream.entries, stream.omitted]
  )

  const hasOmissionEntry = entries.some(entry => entry.id === 'omitted')

  return (
    <Box flexDirection="column" flexGrow={1} onClick={onFocus} width={width}>
      <Text bold color={t.color.label} wrap="truncate-end">
        {stream.title} · read-only
      </Text>
      <Text color={t.color.muted} wrap="truncate-end">
        {stream.status}
      </Text>
      {stream.omitted && !hasOmissionEntry ? <Text color={t.color.warn}>[Earlier output omitted]</Text> : null}
      {entries.map(entry => (
        <Text
          color={entry.tone === 'error' ? t.color.error : entry.tone === 'warn' ? t.color.warn : t.color.text}
          key={entry.id}
          wrap="wrap"
        >
          {entryText(entry)}
        </Text>
      ))}
      <Text color={t.color.muted}>click to focus</Text>
    </Box>
  )
}

export interface OverflowBarProps {
  layout: OutputLayout
  streams: Record<string, OutputStream>
  t: Theme
}

export function OverflowBar({ layout, streams, t }: OverflowBarProps) {
  const waiting = Object.values(streams)
    .filter(
      stream =>
        stream.sessionId !== layout.primarySessionId &&
        stream.sessionId !== layout.secondarySessionId &&
        stream.hasDisplayOutput
    )
    .sort((left, right) => right.lastOutputAt - left.lastOutputAt)

  if (!waiting.length) {
    return null
  }

  const labels = waiting.map(stream => stream.title + (stream.unreadCount ? ' (' + stream.unreadCount + ')' : ''))

  return (
    <Box flexShrink={0} paddingX={1}>
      <Text color={t.color.warn} wrap="truncate-end">
        waiting: {labels.join(' · ')}
      </Text>
    </Box>
  )
}

export interface SplitOutputPaneProps {
  cols: number
  onFocusSession: (sessionId: string) => Promise<boolean> | void
  renderPrimary: (width: number) => ReactNode
}

const terminalStatuses = new Set(['closed', 'completed', 'disconnected', 'error'])

export function SplitOutputPane({ cols, onFocusSession, renderPrimary }: SplitOutputPaneProps) {
  const layout = useStore($outputLayout)
  const streams = useStore($outputStreams)
  const t = useStore($uiTheme)
  const primary = layout.primarySessionId ? streams[layout.primarySessionId] : undefined
  const secondary = layout.secondarySessionId ? streams[layout.secondarySessionId] : undefined

  const runningOthers = Object.values(streams).filter(
    stream => stream.sessionId !== layout.primarySessionId && stream.producing
  )

  const showStillRunning = Boolean(primary && terminalStatuses.has(primary.status) && runningOthers.length)

  const notices = (
    <>
      {showStillRunning ? (
        <Box flexShrink={0} paddingX={1}>
          <Text color={t.color.warn}>still running: {runningOthers.map(stream => stream.title).join(' · ')}</Text>
        </Box>
      ) : null}
      <OverflowBar layout={layout} streams={streams} t={t} />
    </>
  )

  if (layout.mode !== 'split' || !secondary) {
    return (
      <Box flexDirection="column" flexGrow={1}>
        <Box flexDirection="column" flexGrow={1}>
          {renderPrimary(cols)}
        </Box>
        {notices}
      </Box>
    )
  }

  const mode = outputPaneMode(cols)
  const primaryTitle = primary?.title || layout.primarySessionId || 'Current'

  return (
    <Box flexDirection="column" flexGrow={1}>
      {mode === 'tabs' ? (
        <>
          <Box flexShrink={0} paddingX={1}>
            <Text bold color={t.color.label}>
              {primaryTitle} · focused
            </Text>
            <Text color={t.color.muted}> │ </Text>
            <Text color={t.color.muted} onClick={() => void onFocusSession(secondary.sessionId)}>
              {secondary.title} · read-only
            </Text>
          </Box>
          <Box flexDirection="column" flexGrow={1}>
            {renderPrimary(cols)}
          </Box>
        </>
      ) : (
        <Box flexDirection="row" flexGrow={1}>
          <Box flexDirection="column" flexGrow={1} width={outputPaneWidths(cols).primary}>
            <Text bold color={t.color.label} wrap="truncate-end">
              {primaryTitle} · focused
            </Text>
            {renderPrimary(outputPaneWidths(cols).primary)}
          </Box>
          <Box flexShrink={0} width={1}>
            <Text color={t.color.border}>│</Text>
          </Box>
          <ReadonlyOutputPane
            onFocus={() => void onFocusSession(secondary.sessionId)}
            stream={secondary}
            t={t}
            width={outputPaneWidths(cols).secondary}
          />
        </Box>
      )}
      {notices}
    </Box>
  )
}

import { Box, Text, useInput } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { $outputLayout, $outputStreams, type OutputStream } from '../app/outputStreamStore.js'
import type { Theme } from '../theme.js'

import { listRowStyle } from './overlayPrimitives.js'

export interface OutputManagerProps {
  onActivate: (sessionId: string) => Promise<boolean>
  onClose: () => void
  onExitSplit: () => void
  onSetSecondary: (sessionId: string) => void
  t: Theme
}

export const outputFocusDirection = (key: {
  leftArrow: boolean
  meta: boolean
  rightArrow: boolean
}): -1 | 0 | 1 => (key.meta && key.leftArrow ? -1 : key.meta && key.rightArrow ? 1 : 0)

interface OutputManagerRow {
  role: 'primary' | 'secondary' | 'waiting'
  stream: OutputStream
}

export function OutputManager({ onActivate, onClose, onExitSplit, onSetSecondary, t }: OutputManagerProps) {
  const streams = useStore($outputStreams)
  const layout = useStore($outputLayout)
  const [selected, setSelected] = useState(0)

  const rows: OutputManagerRow[] = Object.values(streams)
    .sort((a, b) => {
      const rank = (stream: OutputStream) =>
        stream.sessionId === layout.primarySessionId ? 0 : stream.sessionId === layout.secondarySessionId ? 1 : 2

      return rank(a) - rank(b) || b.lastOutputAt - a.lastOutputAt
    })
    .map(stream => ({
      role:
        stream.sessionId === layout.primarySessionId
          ? 'primary'
          : stream.sessionId === layout.secondarySessionId
            ? 'secondary'
            : 'waiting',
      stream
    }))

  const current = rows[Math.min(selected, Math.max(0, rows.length - 1))]

  useInput((ch, key) => {
    if (key.escape) {
      return onClose()
    }

    if (key.upArrow && !key.meta) {
      return setSelected(index => Math.max(0, index - 1))
    }

    if (key.downArrow && !key.meta) {
      return setSelected(index => Math.min(rows.length - 1, index + 1))
    }

    if (key.return && current) {
      return void onActivate(current.stream.sessionId).then(ok => {
        if (ok !== false) {
          onClose()
        }
      })
    }

    if (ch.toLowerCase() === 's' && current) {
      return onSetSecondary(current.stream.sessionId)
    }

    if (ch.toLowerCase() === 'x') {
      return onExitSplit()
    }
  })

  return (
    <Box flexDirection="column" paddingX={1} paddingY={1}>
      <Text bold color={t.color.primary}>Output streams</Text>
      {rows.map((row, index) => {
        const style = listRowStyle(t, index === selected)
        const status = row.stream.producing ? 'running' : row.stream.status

        return (
          <Box flexDirection="column" key={row.stream.sessionId}>
            <Text backgroundColor={style.backgroundColor} bold={index === selected} color={style.color}>
              {`${index === selected ? '▸ ' : '  '}${row.stream.title} · ${status} · ${row.role}`}
            </Text>
            <Text color={t.color.muted} wrap="truncate-end">
              {`${row.stream.unreadCount ? `${row.stream.unreadCount} unread · ` : ''}${row.stream.preview || '(no output yet)'}`}
            </Text>
          </Box>
        )
      })}
      <Text color={t.color.muted}>↑↓ select · Enter activate · s secondary · x exit split · Esc close</Text>
    </Box>
  )
}

import { Box, Text, useInput } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { $outputStreams, type OutputConflict, type OutputConflictDecision } from '../app/outputStreamStore.js'
import type { Theme } from '../theme.js'

import { MenuRow } from './overlayPrimitives.js'

const DECISIONS: readonly OutputConflictDecision[] = ['keep-primary', 'prioritize-candidate', 'split', 'open-manager']

interface ConflictKey {
  downArrow?: boolean
  escape?: boolean
  return?: boolean
  upArrow?: boolean
}

type ConflictAction =
  { decision: OutputConflictDecision; kind: 'choose' } | { delta: -1 | 1; kind: 'move' } | { kind: 'noop' }

export function outputConflictAction(ch: string, key: ConflictKey, selection: number): ConflictAction {
  if (key.escape) {
    return { decision: 'keep-primary', kind: 'choose' }
  }

  const shortcut = Number.parseInt(ch, 10)

  if (shortcut >= 1 && shortcut <= DECISIONS.length) {
    return { decision: DECISIONS[shortcut - 1]!, kind: 'choose' }
  }

  if (key.return) {
    return { decision: DECISIONS[selection]!, kind: 'choose' }
  }

  if (key.upArrow && selection > 0) {
    return { delta: -1, kind: 'move' }
  }

  if (key.downArrow && selection < DECISIONS.length - 1) {
    return { delta: 1, kind: 'move' }
  }

  return { kind: 'noop' }
}

export interface OutputConflictPromptProps {
  conflict: OutputConflict
  onDecision: (decision: OutputConflictDecision) => void
  t: Theme
}

export function OutputConflictPrompt({ conflict, onDecision, t }: OutputConflictPromptProps) {
  const streams = useStore($outputStreams)
  const [selection, setSelection] = useState(0)
  const currentTitle = streams[conflict.primarySessionId]?.title || conflict.primarySessionId
  const candidateTitle = streams[conflict.candidateSessionId]?.title || conflict.candidateSessionId
  const labels = ['Current · ' + currentTitle, 'New output · ' + candidateTitle, 'Split', 'Other…']

  useInput((ch, key) => {
    const action = outputConflictAction(ch, key, selection)

    if (action.kind === 'choose') {
      onDecision(action.decision)
    } else if (action.kind === 'move') {
      setSelection(value => value + action.delta)
    }
  })

  return (
    <Box borderColor={t.color.warn} borderStyle="double" flexDirection="column" paddingX={1}>
      <Text bold color={t.color.warn}>
        concurrent output
      </Text>
      <Text color={t.color.text}>Another live session produced visible output. Choose what stays in focus.</Text>
      {labels.map((label, index) => (
        <MenuRow active={selection === index} index={index + 1} key={DECISIONS[index]} label={label} t={t} />
      ))}
      <Text color={t.color.muted}>↑/↓ select · Enter confirm · 1-4 quick pick · Esc keep current</Text>
    </Box>
  )
}

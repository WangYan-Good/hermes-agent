export type DiffKind = 'add' | 'context' | 'remove'

export interface DiffLine {
  kind: DiffKind
  text: string
  /** 1-based line number in the old/new file (absent on the "other" side of an
   *  add/remove, and on hunk-separator blanks). Only used when line numbers are
   *  shown (the preview's full diff). */
  newNo?: number
  oldNo?: number
}

interface ParsedHunk {
  lines: Array<{ kind: DiffKind; text: string }>
  newStart: number
  oldStart: number
}

export function diffKind(line: string): DiffKind {
  if (line.startsWith('+') && !line.startsWith('+++')) {
    return 'add'
  }

  if (line.startsWith('-') && !line.startsWith('---')) {
    return 'remove'
  }

  return 'context'
}

// Drop the leading +/-/space gutter so changes read by color alone, keeping the
// rest of the indentation intact.
function stripDiffMarker(line: string): string {
  if (diffKind(line) !== 'context' || line.startsWith(' ')) {
    return line.slice(1)
  }

  return line
}

// Git-style unified diffs arrive with a file-header preamble — `diff --git`,
// `index …`, `--- a/path`, `+++ b/path`, and Hermes' own `a/path → b/path`
// arrow line. That preamble just repeats the path (which the tool row already
// shows) and reads especially badly for absolute paths (`a//Users/…`). Strip
// the leading header zone up to the first hunk.
const DIFF_HEADER_PREFIXES = [
  'diff --git',
  'index ',
  '--- ',
  '+++ ',
  'similarity ',
  'rename ',
  'new file',
  'deleted file'
]

function isArrowHeaderLine(line: string): boolean {
  const trimmed = line.trim()

  return trimmed.includes('→') && /^\S.*→\s*\S+$/.test(trimmed) && !/^[+\-@]/.test(trimmed)
}

/** Exported for tests. */
export function stripDiffFileHeaders(diff: string): string {
  const lines = diff.split('\n')
  let start = 0

  for (; start < lines.length; start += 1) {
    const line = lines[start]

    if (line.startsWith('@@')) {
      break
    }

    if (line.trim() === '' || isArrowHeaderLine(line) || DIFF_HEADER_PREFIXES.some(prefix => line.startsWith(prefix))) {
      continue
    }

    break
  }

  return lines.slice(start).join('\n')
}

function parseHunks(diff: string): ParsedHunk[] {
  const hunks: ParsedHunk[] = []
  let active: null | ParsedHunk = null

  for (const line of stripDiffFileHeaders(diff).split('\n')) {
    if (line.startsWith('@@')) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line)

      if (!match) {
        active = null

        continue
      }

      active = { oldStart: Number(match[1]), newStart: Number(match[2]), lines: [] }
      hunks.push(active)

      continue
    }

    if (!active || line.startsWith('\\')) {
      continue
    }

    active.lines.push({ kind: diffKind(line), text: stripDiffMarker(line) })
  }

  return hunks
}

// Cleaned diff → renderable lines: file-headers + `@@` hunks dropped (a blank
// separator kept between hunks), markers stripped, kind recorded. Old/new line
// numbers are tracked from each `@@ -a,b +c,d @@` header so a caller that wants
// a gutter (the preview) can render them; the blank separator carries none.
export function parseDiff(diff: string): DiffLine[] {
  const hunks = parseHunks(diff)

  if (hunks.length === 0) {
    // Fallback for unexpected non-hunk payloads.
    return stripDiffFileHeaders(diff)
      .split('\n')
      .map(line => ({ kind: diffKind(line), text: stripDiffMarker(line) }))
  }

  const out: DiffLine[] = []
  let emitted = false
  let oldNo = 1
  let newNo = 1

  for (const hunk of hunks) {
    oldNo = hunk.oldStart
    newNo = hunk.newStart

    if (emitted) {
      out.push({ kind: 'context', text: '' })
    }

    for (const line of hunk.lines) {
      const entry: DiffLine = { kind: line.kind, text: line.text }

      if (line.kind === 'add') {
        entry.newNo = newNo++
      } else if (line.kind === 'remove') {
        entry.oldNo = oldNo++
      } else {
        entry.oldNo = oldNo++
        entry.newNo = newNo++
      }

      out.push(entry)
      emitted = true
    }
  }

  return out
}

// Build a full-file diff view anchored to the CURRENT file text. Every current
// line is emitted from `fullText` with its real new-file line number; hunks only
// mark those rows as added and insert deleted rows between them. That keeps the
// preview's SOURCE and DIFF views on the same line map even when git returns
// compact hunks or removed-only rows.
export function parseFullFileDiff(diff: string, fullText: string): DiffLine[] {
  const hunks = parseHunks(diff)
  const fullLines = fullText.split('\n')

  if (hunks.length === 0) {
    return fullLines.map((text, index) => ({ kind: 'context', newNo: index + 1, oldNo: index + 1, text }))
  }

  const added = new Set<number>()
  const oldNoByNewNo = new Map<number, number>()
  const removalsByNewNo = new Map<number, DiffLine[]>()
  const out: DiffLine[] = []

  for (const hunk of hunks) {
    let oldNo = hunk.oldStart
    let newNo = hunk.newStart

    for (const line of hunk.lines) {
      if (line.kind === 'add') {
        added.add(newNo)
        newNo += 1
      } else if (line.kind === 'remove') {
        const anchor = Math.max(1, Math.min(newNo, fullLines.length + 1))
        const bucket = removalsByNewNo.get(anchor) ?? []

        bucket.push({ kind: 'remove', oldNo, text: line.text })
        removalsByNewNo.set(anchor, bucket)
        oldNo += 1
      } else {
        oldNoByNewNo.set(newNo, oldNo)
        oldNo += 1
        newNo += 1
      }
    }
  }

  for (let index = 0; index < fullLines.length; index += 1) {
    const newNo = index + 1
    const removals = removalsByNewNo.get(newNo)

    if (removals) {
      out.push(...removals)
    }

    out.push({
      kind: added.has(newNo) ? 'add' : 'context',
      newNo,
      oldNo: oldNoByNewNo.get(newNo),
      text: fullLines[index] ?? ''
    })
  }

  const trailingRemovals = removalsByNewNo.get(fullLines.length + 1)

  if (trailingRemovals) {
    out.push(...trailingRemovals)
  }

  return out
}

import { lazy, Suspense, useMemo, useState } from 'react'

import { diffKind } from './diff'

const Highlight = lazy(() => import('./shiki-block'))

function utf8Width(code: number) {
  return code < 128 ? 1 : code < 2048 ? 2 : code < 65536 ? 3 : 4
}

export function exceedsMarkdownBudget(text: string): boolean {
  if (text.length > 262144) {
    return true
  }

  let bytes = 0

  for (const char of text) {
    bytes += utf8Width(char.codePointAt(0)!)

    if (bytes > 262144) {
      return true
    }
  }

  return false
}

/** Page by UTF-8 bytes and lines without splitting a surrogate pair. */
export function outputPage(text: string, start: number): string {
  let end = start
  let bytes = 0
  let lines = 0

  for (const char of text.slice(start, start + 32768)) {
    const width = utf8Width(char.codePointAt(0)!)

    if (bytes + width > 32768) {
      break
    }

    bytes += width
    end += char.length

    if (char === '\n' && ++lines === 200) {
      break
    }
  }

  return text.slice(start, end)
}

export function BoundedOutput({ text, diff = false, language }: { text: string; diff?: boolean; language?: string }) {
  const [positions, setPositions] = useState([0])
  const start = Math.min(positions.at(-1) ?? 0, text.length)
  const page = useMemo(() => outputPage(text, start), [text, start])
  const more = start + page.length < text.length

  return (
    <div className="hermes-output">
      {language ? (
        <Suspense fallback={<pre>{page}</pre>}>
          <Highlight language={language} theme={{ dark: 'github-dark-dimmed', light: 'github-light-default' }}>
            {page}
          </Highlight>
        </Suspense>
      ) : (
        <pre style={{ overflow: 'auto', maxHeight: '40vh', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {diff
            ? page.split('\n').map((line, i) => (
                <span
                  key={i}
                  style={{
                    display: 'block',
                    color:
                      diffKind(line) === 'add'
                        ? 'var(--color-green-600, green)'
                        : diffKind(line) === 'remove'
                          ? 'var(--color-red-600, crimson)'
                          : undefined
                  }}
                >
                  {line}
                </span>
              ))
            : page}
        </pre>
      )}
      {more || start > 0 ? (
        <nav aria-label="Output pages">
          <span>
            Showing {start + 1}–{start + page.length} of {text.length} characters.{' '}
          </span>
          <button disabled={positions.length === 1} onClick={() => setPositions(p => p.slice(0, -1))} type="button">
            Previous
          </button>{' '}
          <button disabled={!more} onClick={() => setPositions(p => [...p, start + page.length])} type="button">
            Next
          </button>
        </nav>
      ) : null}
    </div>
  )
}

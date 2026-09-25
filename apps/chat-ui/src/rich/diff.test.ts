import { expect, it } from 'vitest'

import { diffKind, parseDiff, parseFullFileDiff } from './diff'

it('retains old/new line ownership through a replacement', () => {
  const lines = parseDiff('--- a/f\n+++ b/f\n@@ -2,2 +2,2 @@\n-before\n+after\n unchanged')
  expect(lines).toEqual([
    { kind: 'remove', text: 'before', oldNo: 2 },
    { kind: 'add', text: 'after', newNo: 2 },
    { kind: 'context', text: 'unchanged', newNo: 3, oldNo: 3 }
  ])
})
it('keeps full-file source line numbering and inserts deleted content without renumbering the source', () => {
  const lines = parseFullFileDiff('@@ -2,1 +2,0 @@\n-removed', 'first\nlast')
  expect(lines.filter(line => line.kind !== 'remove').map(line => [line.newNo, line.text])).toEqual([[1, 'first'], [2, 'last']])
  expect(lines.find(line => line.kind === 'remove')).toMatchObject({ text: 'removed', oldNo: 2 })
})
it('does not count file headers as changes', () => {
  expect(diffKind('+++ b/file')).toBe('context')
  expect(diffKind('--- a/file')).toBe('context')
})

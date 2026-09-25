import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as chat from './index'

afterEach(cleanup)

describe('portable rich content', () => {
  it('rejects executable and credential-bearing links', () => {
    expect(typeof chat.safeExternalUrl).toBe('function')

    for (const url of ['javascript:alert(1)', 'vbscript:x', 'data:text/html,x', 'https://user:pass@example.org', 'file:///etc/passwd']) {
      expect(chat.safeExternalUrl(url)).toBeNull()
    }

    expect(chat.safeExternalUrl('https://example.org/a')).toBe('https://example.org/a')
  })

  it('bounds output even when expanded, without losing the original download', () => {
    expect(typeof chat.BoundedOutput).toBe('function')
    const text = Array.from({ length: 10000 }, (_, i) => `line ${i}`).join('\n')
    const { container } = render(<chat.BoundedOutput text={text} />)
    expect(container.textContent).not.toContain('line 9999')
    fireEvent.click(screen.getByRole('button', { name: /next/i }))
    expect(container.textContent).not.toContain('line 9999')
    expect(container.textContent!.length).toBeLessThan(40000)
  })

  it('renders tables, fences and math, leaving malicious HTML inert', () => {
    expect(typeof chat.RichMarkdown).toBe('function')
    const { container } = render(<chat.RichMarkdown sourceId="row-1" text={'# Report\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n```js\nconst x = 1\n```\n\n$x^2$\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))'} />)
    expect(screen.getByRole('heading').textContent).toBe('Report')
    expect(container.querySelector('table')).not.toBeNull()
    expect(container.textContent).toContain('const x = 1')
    expect(container.querySelector('.katex')).not.toBeNull()
    expect(container.querySelector('script,iframe,object,embed')).toBeNull()
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull()
  })

  it('keeps huge blocks readable without parsing the entire payload', () => {
    expect(typeof chat.RichMarkdown).toBe('function')
    const { container } = render(<chat.RichMarkdown sourceId="large" text={'x'.repeat(2_000_000)} />)
    expect(container.textContent!.length).toBeLessThan(40000)
    expect(screen.getByText(/showing/i)).toBeTruthy()
  })
})

it('bounds multibyte output by UTF-8 bytes without splitting characters', () => {
  const chunk = chat.outputPage('😀'.repeat(20000), 0)
  expect(new TextEncoder().encode(chunk).length).toBeLessThanOrEqual(32768)
  expect(chunk.endsWith('😀')).toBe(true)
})

it('uses block positions for distinct stable artifacts and never executes their source', () => {
  const document = '<!doctype html><html><body>' + 'safe '.repeat(50) + '<script>window.executed = true</script></body></html>'
  const text = ['```html', document, '```', '', 'Between', '', '```html', document, '```'].join('\n')
  const { container, rerender } = render(<chat.RichMarkdown sourceId="durable-turn:part:2" text={text} />)
  const ids = () => [...container.querySelectorAll('[data-artifact-id]')].map(n => n.getAttribute('data-artifact-id'))
  const original = ids()
  expect(original).toHaveLength(2)
  expect(new Set(original).size).toBe(2)
  rerender(<chat.RichMarkdown sourceId="durable-turn:part:2" text={text} />)
  expect(ids()).toEqual(original)
  expect(container.querySelector('script,iframe,object,embed')).toBeNull()
})

it('releases both mounted and late media resources without leaking object URLs', async () => {
  const release = vi.fn()
  let complete!: (value: { url: string; release: () => void }) => void
  const resolveMedia = vi.fn(() => new Promise<{ url: string; release: () => void }>(resolve => { complete = resolve }))
  const host = { resolveMedia, openReference: vi.fn(), previewArtifact: vi.fn(), downloadArtifact: vi.fn() }
  const reference = { kind: 'image' as const, value: '/images/one.png' }
  const view = render(<chat.ChatHostContext.Provider value={host}><chat.MediaView reference={reference} /></chat.ChatHostContext.Provider>)
  await act(async () => complete({ url: 'blob:one', release }))
  await waitFor(() => expect(view.container.querySelector('img')).not.toBeNull())
  view.unmount()
  expect(release).toHaveBeenCalledTimes(1)
  const late = render(<chat.ChatHostContext.Provider value={host}><chat.MediaView reference={reference} /></chat.ChatHostContext.Provider>)
  late.unmount()
  await act(async () => complete({ url: 'blob:late', release }))
  expect(release).toHaveBeenCalledTimes(2)
})

it('whitelists presentation fields and rejects forged identities and media URLs', () => {
  const value = { version: 1, tool_call_id: 'real', secret: 'never copy', changes: [{ path: '/file', diff: '+one', operation: 'execute', added: -3, secret: 'hidden' }], media: [{ id: 'image', ref: 'https://user:password@example.org/a', mime: 'image/png' }] }
  expect(chat.readToolPresentation(value, 'wrong')).toBeUndefined()
  expect(chat.readToolPresentation(value, 'real')).toEqual({ version: 1, tool_call_id: 'real', changes: [{ path: '/file', diff: '+one' }], media: [] })
})

/** Renderer data contains neither transport authority nor host callbacks. */
export interface ContentReference {
  kind: 'url' | 'file' | 'folder' | 'image' | 'media'
  value: string
  title?: string
  mime?: string
}

export interface ContentArtifact {
  id: string
  title: string
  kind: 'code' | 'html' | 'svg' | 'document'
  text: string
  language?: string
}

export interface MediaResource {
  url: string
  release: () => void
}

export interface ChatHostAdapter {
  openReference: (reference: ContentReference) => Promise<void>
  resolveMedia: (reference: ContentReference, signal: AbortSignal) => Promise<MediaResource>
  previewArtifact: (artifact: ContentArtifact) => Promise<void>
  downloadArtifact: (artifact: ContentArtifact) => Promise<void>
}

/** Live and durable transcript projections carry the same portable data. */
export interface ChatTranscriptPart {
  type: 'text' | 'reasoning' | 'tool'
  text: string
  sourceId?: string
  id?: string
  name?: string
  status?: 'running' | 'complete' | 'error'
  sealed?: boolean
  final?: boolean
  args?: Record<string, unknown>
  result?: unknown
  presentation?: ToolPresentation
}

export interface ToolPresentation {
  version: 1
  tool_call_id: string
  changes?: { path: string; diff: string; operation?: string; added?: number; removed?: number }[]
  media?: { id: string; ref: string; mime: string; title?: string }[]
}

export function safeExternalUrl(value: string): string | null {
  try {
    const url = new URL(value)

    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch {
    return null
  }
}

const mediaTypes = new Set([
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
  'audio/mpeg',
  'audio/wav',
  'audio/ogg',
  'video/mp4',
  'video/webm'
])

function safeReference(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 4096 || [...value].some(c => c.charCodeAt(0) < 32)) {
    return false
  }

  // Local references are resolved against the host's authenticated allowlist.
  if (/^[a-z]:[\\/]/i.test(value)) {
    return true
  }

  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
    return !!safeExternalUrl(value)
  }

  return !value.startsWith('//')
}

export function readToolPresentation(value: unknown, toolId: string): ToolPresentation | undefined {
  if (!value || typeof value !== 'object') {
    return undefined
  }

  const p = value as ToolPresentation

  if (p.version !== 1 || !toolId || p.tool_call_id !== toolId) {
    return undefined
  }

  return {
    version: 1,
    tool_call_id: toolId,
    changes: Array.isArray(p.changes)
      ? p.changes
          .slice(0, 100)
          .filter(c => c && safeReference(c.path) && typeof c.diff === 'string')
          .map(c => ({
            path: c.path,
            diff: c.diff,
            ...(['create', 'modify', 'delete'].includes(c.operation || '') ? { operation: c.operation } : {}),
            ...(Number.isSafeInteger(c.added) && c.added! >= 0 ? { added: c.added } : {}),
            ...(Number.isSafeInteger(c.removed) && c.removed! >= 0 ? { removed: c.removed } : {})
          }))
      : [],
    media: Array.isArray(p.media)
      ? p.media
          .slice(0, 100)
          .filter(m => m && typeof m.id === 'string' && safeReference(m.ref) && mediaTypes.has(m.mime))
          .map(m => ({
            id: m.id.slice(0, 256),
            ref: m.ref,
            mime: m.mime,
            ...(typeof m.title === 'string' ? { title: m.title.slice(0, 240) } : {})
          }))
      : []
  }
}

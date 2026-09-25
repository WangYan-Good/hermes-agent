import { preprocessMarkdown as preprocess } from '@hermes/chat-ui/markdown-preprocess'

import { stripPreviewTargets } from '@/lib/preview-targets'
import { linkifySessionRefs } from '@/lib/session-refs'

export function preprocessMarkdown(text: string): string {
  return preprocess(text, { stripPreviewTargets, linkifySessionRefs })
}

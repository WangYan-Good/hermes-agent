import type { ChatHostAdapter } from '@hermes/chat-ui/contracts'
import { safeExternalUrl } from '@hermes/chat-ui/contracts'

import { openExternalLink } from '@/lib/external-link'
import { downloadGatewayMediaFile, isRemoteGateway, mediaExternalUrl, resolveMediaPlaybackSrc } from '@/lib/media'
import { openArtifact, upsertArtifact } from '@/store/artifacts'

/** The shared renderer never sees Electron, connection credentials or stores. */
export function createDesktopChatHost(sessionId: string): ChatHostAdapter {
  return {
    async openReference(reference) {
      if (reference.kind === 'url') {
        const url = safeExternalUrl(reference.value)

        if (!url) {
          throw new Error('Unsupported link')
        }

        openExternalLink(url)
      } else if (isRemoteGateway()) {
        await downloadGatewayMediaFile(reference.value)
      } else {
        openExternalLink(mediaExternalUrl(reference.value))
      }
    },
    async resolveMedia(reference, signal) {
      const url = await resolveMediaPlaybackSrc(reference.value)

      if (signal.aborted) {
        throw new Error('Media request cancelled')
      }

      return { url, release: () => {} }
    },
    async previewArtifact(artifact) {
      if (artifact.kind === 'document') {
        throw new Error('Unsupported artifact')
      }

      const result = upsertArtifact(
        sessionId,
        { kind: artifact.kind, title: artifact.title, language: artifact.language || '' },
        artifact.text
      )

      if (!result) {
        return
      }

      const index = result.record.versions.findIndex(v => v.content === artifact.text)
      openArtifact(result.artifactId, index < 0 ? undefined : index)
    },
    async downloadArtifact(artifact) {
      const url = URL.createObjectURL(new Blob([artifact.text], { type: 'text/plain' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `${artifact.title.replace(/[^\w.-]/g, '_').slice(0, 80) || 'artifact'}.txt`
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    }
  }
}

import { useEffect, useState } from 'react'

import { BoundedOutput } from './bounded-output'
import type { ContentArtifact, ContentReference, ToolPresentation } from './contracts'
import { useChatHost } from './host'

export function ArtifactView({ artifact }: { artifact: ContentArtifact }) {
  const host = useChatHost()
  const [error, setError] = useState(false)

  const action = (fn: () => Promise<void>) => {
    setError(false)
    void fn().catch(() => setError(true))
  }

  return (
    <section aria-label={`Artifact ${artifact.title}`} data-artifact-id={artifact.id}>
      <strong>{artifact.title}</strong> <small>{artifact.kind}</small>
      <details>
        <summary>Source preview</summary>
        <BoundedOutput text={artifact.text} />
      </details>
      {host ? (
        <>
          <button onClick={() => action(() => host.previewArtifact(artifact))} type="button">
            Open preview
          </button>{' '}
          <button onClick={() => action(() => host.downloadArtifact(artifact))} type="button">
            Download
          </button>
        </>
      ) : null}
      {error ? <p role="alert">Could not open this artifact.</p> : null}
    </section>
  )
}

export function ReferenceView({ reference }: { reference: ContentReference }) {
  const host = useChatHost()
  const [error, setError] = useState(false)

  return (
    <span>
      <button
        disabled={!host}
        onClick={() => {
          setError(false)
          void host?.openReference(reference).catch(() => setError(true))
        }}
        type="button"
      >
        {reference.title || reference.value}
      </button>
      {error ? <span role="status"> Unavailable</span> : null}
    </span>
  )
}

export function MediaView({ reference }: { reference: ContentReference }) {
  const host = useChatHost()
  const [url, setUrl] = useState('')
  const [error, setError] = useState(false)
  useEffect(() => {
    setUrl('')
    setError(false)
    const controller = new AbortController()
    let release: (() => void) | undefined

    if (host) {
      void host
        .resolveMedia(reference, controller.signal)
        .then(resource => {
          if (controller.signal.aborted) {
            resource.release()
          } else {
            release = resource.release
            setUrl(resource.url)
          }
        })
        .catch(() => {
          if (!controller.signal.aborted) {
            setError(true)
          }
        })
    }

    return () => {
      controller.abort()
      release?.()
    }
    // Resource identity is data, independent of the caller object identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [host, reference.kind, reference.value, reference.mime, reference.title])

  return (
    <figure>
      {url && !error ? (
        reference.mime?.startsWith('video/') ? (
          <video controls preload="metadata" src={url} />
        ) : reference.mime?.startsWith('audio/') ? (
          <audio controls preload="metadata" src={url} />
        ) : (
          <img
            alt={reference.title || 'Generated image'}
            loading="lazy"
            onError={() => setError(true)}
            src={url}
            style={{ maxWidth: '100%' }}
          />
        )
      ) : (
        <span role="status">{error ? 'Media unavailable' : 'Loading media…'}</span>
      )}
      <figcaption>
        <ReferenceView reference={reference} />
      </figcaption>
    </figure>
  )
}

export function ToolResultView({ result, presentation }: { result: unknown; presentation?: ToolPresentation }) {
  const [expanded, setExpanded] = useState(false)
  const text = !expanded ? '' : typeof result === 'string' ? result : (JSON.stringify(result, null, 2) ?? '')

  return (
    <div>
      {presentation?.changes?.map((change, i) => (
        <section aria-label={`Changed file ${change.path}`} key={i}>
          <strong>{change.path}</strong>
          {change.operation ? ` · ${change.operation}` : ''}
          {change.added !== undefined ? ` +${change.added}` : ''}
          {change.removed !== undefined ? ` −${change.removed}` : ''}
          <details>
            <summary>Diff</summary>
            <BoundedOutput diff text={change.diff} />
          </details>
        </section>
      ))}
      {presentation?.media?.map(media => (
        <MediaView
          key={media.id}
          reference={{ kind: 'media', value: media.ref, mime: media.mime, title: media.title }}
        />
      ))}
      <details onToggle={event => setExpanded(event.currentTarget.open)}>
        <summary>Tool output</summary>
        {expanded ? <BoundedOutput text={text} /> : null}
      </details>
    </div>
  )
}

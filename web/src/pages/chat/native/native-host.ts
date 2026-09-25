import type { ChatHostAdapter, ContentArtifact, ContentReference } from '@hermes/chat-ui';
import { outputPage, safeExternalUrl } from '@hermes/chat-ui';
import { authedFetch } from '@/lib/api';

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = name;
  a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function media(reference: ContentReference, profile: string, signal?: AbortSignal) {
  const external = safeExternalUrl(reference.value);
  // Never forward dashboard credentials to external media servers.
  const response = external ? await fetch(external, { signal, credentials: 'omit', referrerPolicy: 'no-referrer' }) : await authedFetch(`/api/chat/resources?profile=${encodeURIComponent(profile)}&path=${encodeURIComponent(reference.value)}`, { signal });
  if (!response.ok || !response.body) throw new Error('Media unavailable');
  const type = response.headers.get('Content-Type')?.split(';')[0] ?? '';
  if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp', 'audio/mpeg', 'audio/wav', 'audio/ogg', 'video/mp4', 'video/webm'].includes(type)) throw new Error('Unsafe media type');
  const reader = response.body.getReader(); const chunks: Uint8Array<ArrayBuffer>[] = []; let size = 0;
  try {
    for (;;) { const { value, done } = await reader.read(); if (done) break; size += value.length; if (size > 25 * 1024 * 1024) throw new Error('Media too large'); chunks.push(new Uint8Array(value)); }
  } finally { await reader.cancel(); }
  return new Blob(chunks, { type });
}
function artifactName(a: ContentArtifact) { return `${a.title.replace(/[^\w.-]/g, '_').slice(0, 80) || 'artifact'}.txt`; }

export function createNativeHost(profile: string): ChatHostAdapter { return {
  async openReference(reference) {
    if (reference.kind === 'url') {
      const url = safeExternalUrl(reference.value); if (!url) throw new Error('Unsafe link');
      window.open(url, '_blank', 'noopener,noreferrer'); return;
    }
    if (reference.kind === 'image' || reference.kind === 'media') { download(await media(reference, profile), reference.title || 'image'); return; }
    if (reference.kind === 'folder') throw new Error('Folder references are context, not browser directories');
    const response = await authedFetch(`/api/chat/resources?profile=${encodeURIComponent(profile)}&path=${encodeURIComponent(reference.value)}`);
    if (!response.ok) throw new Error('File unavailable');
    download(await response.blob(), reference.title || reference.value.split('/').at(-1) || 'attachment');
  },
  async resolveMedia(reference, signal) {
    const url = URL.createObjectURL(await media(reference, profile, signal));
    return { url, release: () => URL.revokeObjectURL(url) };
  },
  async previewArtifact(artifact) {
    // Source-only preview; HTML and SVG never become executable documents.
    const dialog = document.createElement('dialog'); const pre = document.createElement('pre');
    dialog.style.cssText = 'max-width:min(90vw,70rem);max-height:80vh;overflow:auto;padding:1.5rem;color:var(--midground);background:var(--background-base);border:1px solid currentColor;border-radius:12px';
    pre.style.whiteSpace = 'pre-wrap';
    const status = document.createElement('p');
    const next = document.createElement('button'); next.textContent = 'Next page';
    const previous = document.createElement('button'); previous.textContent = 'Previous page';
    const positions = [0]; let page = 0;
    const render = () => {
      const start = positions[page]; const text = outputPage(artifact.text, start);
      pre.textContent = text; status.textContent = `Showing page ${page + 1}${start + text.length < artifact.text.length ? ' · more content available' : ''}`;
      previous.disabled = page === 0; next.disabled = start + text.length >= artifact.text.length;
      positions[page + 1] = start + text.length;
    };
    next.onclick = () => { page += 1; render(); }; previous.onclick = () => { page -= 1; render(); }; render();
    const close = document.createElement('button'); close.textContent = 'Close preview'; close.onclick = () => dialog.close();
    dialog.append(close, status, pre, previous, next); dialog.setAttribute('aria-label', artifact.title);
    dialog.addEventListener('close', () => dialog.remove(), { once: true });
    document.body.append(dialog); dialog.showModal();
  },
  async downloadArtifact(artifact) { download(new Blob([artifact.text], { type: 'text/plain' }), artifactName(artifact)); },
};
}

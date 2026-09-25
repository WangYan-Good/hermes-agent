import { useMemo } from 'react';
import { MediaView, ReferenceView, safeExternalUrl, type ContentReference } from '@hermes/chat-ui';
import { referenceRe } from '@hermes/chat-ui/reference-kinds';

export function NativeReferences({ text }: { text: string }) {
  const references = useMemo(() => [...text.matchAll(referenceRe())].flatMap(match => {
    const kind = match[1]; let value = match[2];
    if (['`', '"', "'"].includes(value[0]) && value.at(-1) === value[0]) value = value.slice(1, -1);
    else value = value.replace(/[.,;!?]+$/, '');
    if (!['url', 'file', 'folder', 'image'].includes(kind) || (kind === 'url' && !safeExternalUrl(value))) return [];
    return [{ kind, value, title: kind === 'url' ? value : value.split('/').at(-1) || value } as ContentReference];
  }), [text]);
  return <div className="flex flex-wrap gap-2">{references.map((ref, i) => ref.kind === 'image' ? <MediaView key={`${i}:${ref.value}`} reference={ref} /> : <ReferenceView key={`${i}:${ref.value}`} reference={ref} />)}</div>;
}

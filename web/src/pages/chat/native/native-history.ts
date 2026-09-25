import { fetchJSON } from '@/lib/api';

export interface HistoryPage {
  session_id: string;
  messages: Record<string, unknown>[];
  pagination: { returned: number; limit: number; offset: number; order: string };
}
export function readHistory(profile: string, storedId: string, beforeId?: number, signal?: AbortSignal): Promise<HistoryPage> {
  const params = new URLSearchParams({ profile, limit: '100', order: 'latest', include_compacted: 'true' });
  if (beforeId !== undefined) params.set('before_id', String(beforeId));
  return fetchJSON(`/api/sessions/${encodeURIComponent(storedId)}/messages?${params}`, { signal });
}

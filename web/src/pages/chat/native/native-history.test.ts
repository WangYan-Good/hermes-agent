import { expect, it, vi } from 'vitest';
import { fetchJSON } from '@/lib/api';
import { readHistory } from './native-history';

vi.mock('@/lib/api', () => ({ fetchJSON: vi.fn().mockResolvedValue({}) }));

it('requests the canonical display view for latest and older pages through authenticated fetch', async () => {
  const controller = new AbortController();
  for (const beforeId of [undefined, 42]) {
    await readHistory('work', 'old/id', beforeId, controller.signal);
    const [path, options] = vi.mocked(fetchJSON).mock.calls.at(-1)!;
    const url = new URL(path, 'https://hermes.test');
    expect(url.pathname).toBe('/api/sessions/old%2Fid/messages');
    expect(Object.fromEntries(url.searchParams)).toEqual({ profile: 'work', view: 'display', limit: '100', order: 'latest', include_compacted: 'true', ...(beforeId ? { before_id: '42' } : {}) });
    expect(options?.signal).toBe(controller.signal);
  }
});

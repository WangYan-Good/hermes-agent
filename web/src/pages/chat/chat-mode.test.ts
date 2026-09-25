import { afterEach, expect, it, vi } from 'vitest';
import { CHAT_MODE_STORAGE_KEY, normalizeChatMode, readBrowserChatMode, resolveChatMode, writeBrowserChatMode } from './chat-mode';
afterEach(() => vi.unstubAllGlobals());
it.each([null, undefined, '', 'Native', ' terminal ', 'unknown', 1, {}, []])('ignores corrupt preferences %j', value => {
  expect(normalizeChatMode(value)).toBeNull();
  expect(resolveChatMode({ urlMode: value, browserMode: value, serverMode: value })).toEqual({ requested: 'native', effective: 'native', source: 'default' });
});
it('resolves URL, browser, profile and canonical default in order without an availability gate', () => {
  expect(resolveChatMode({ urlMode: 'terminal', browserMode: 'native', serverMode: 'native' }).source).toBe('url');
  expect(resolveChatMode({ urlMode: 'invalid', browserMode: 'terminal', serverMode: 'native' })).toMatchObject({ effective: 'terminal', source: 'browser' });
  expect(resolveChatMode({ browserMode: 'native', serverMode: 'terminal' }).effective).toBe('native');
  expect(resolveChatMode({ serverMode: 'terminal' })).toMatchObject({ effective: 'terminal', source: 'profile' });
  expect(resolveChatMode().effective).toBe('native');
});
it('reads without writes, persists explicit choices and clears to follow profile', () => {
  const data = new Map<string, string>();
  const setItem = vi.fn((k: string, v: string) => data.set(k, v));
  vi.stubGlobal('window', { localStorage: { getItem: (k: string) => data.get(k), setItem, removeItem: (k: string) => data.delete(k) } });
  expect(readBrowserChatMode()).toBeNull(); expect(setItem).not.toHaveBeenCalled();
  expect(writeBrowserChatMode('terminal')).toBe(true); expect(data.get(CHAT_MODE_STORAGE_KEY)).toBe('terminal');
  expect(readBrowserChatMode()).toBe('terminal'); expect(writeBrowserChatMode(null)).toBe(true); expect(readBrowserChatMode()).toBeNull();
});
it('handles blocked storage without persisting URL resolution', () => {
  vi.stubGlobal('window', { get localStorage() { throw new Error('blocked'); } });
  expect(readBrowserChatMode()).toBeNull(); expect(writeBrowserChatMode('native')).toBe(false);
  expect(resolveChatMode({ urlMode: 'terminal' }).effective).toBe('terminal');
});

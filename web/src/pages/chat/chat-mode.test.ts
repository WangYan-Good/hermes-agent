// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest';
import { normalizeChatMode, resolveChatMode } from './chat-mode';
import { $browserMode, $chatHost, $preferenceNotice, $profileModes, commitBrowserMode, requestBrowserMode } from './chat-preferences';
const getConfig = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api', () => ({ api: { getConfig } }));
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) || null, setItem: vi.fn((key: string, value: string) => { values.set(key, value); }), removeItem: (key: string) => { values.delete(key); } });
  getConfig.mockReset(); $browserMode.set(null); $chatHost.set(null); $profileModes.set({}); $preferenceNotice.set(''); vi.restoreAllMocks(); });
it.each([
  [undefined, undefined, undefined, 'native', 'default'],
  [null, null, 'native', 'native', 'profile'], [null, null, 'terminal', 'terminal', 'profile'],
  [null, 'native', 'terminal', 'native', 'browser'], [null, 'terminal', 'native', 'terminal', 'browser'],
  ['native', 'terminal', 'terminal', 'native', 'url'], ['terminal', 'native', 'native', 'terminal', 'url'],
  ['invalid', 'terminal', 'native', 'terminal', 'browser'], [null, 'invalid', 'terminal', 'terminal', 'profile'],
  ['invalid', 'invalid', 'invalid', 'native', 'default'],
])('resolves %s / %s / %s', (url, browser, profile, mode, source) => { expect(resolveChatMode(url, browser, profile)).toEqual({ mode, source }); });
it.each(['', 'NATIVE', false, 2, {}])('ignores unknown %s', value => { expect(normalizeChatMode(value)).toBeNull(); });
it('storage failure affects persistence only and is visible', () => {
  vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
  commitBrowserMode('terminal'); expect($browserMode.get()).toBe('terminal'); expect($preferenceNotice.get()).toContain('could not save');
});
it('settings without a chat host only persist preference', async () => {
  await requestBrowserMode('', 'terminal'); expect($chatHost.get()).toBeNull(); expect(getConfig).not.toHaveBeenCalled();
  expect(localStorage.getItem('hermes.dashboard.chat.mode')).toBe('terminal');
});
it('Follow profile clears override after known profile resolution', async () => {
  commitBrowserMode('terminal'); getConfig.mockResolvedValue({ dashboard: { chat: { default_mode: 'native' } } });
  await requestBrowserMode('', null); expect($browserMode.get()).toBeNull(); expect(localStorage.getItem('hermes.dashboard.chat.mode')).toBeNull();
});
it('unavailable profile does not silently overwrite an override', async () => {
  commitBrowserMode('terminal'); getConfig.mockRejectedValue(new Error('offline'));
  await requestBrowserMode('', null); expect($browserMode.get()).toBe('terminal'); expect($preferenceNotice.get()).toContain('unavailable');
});

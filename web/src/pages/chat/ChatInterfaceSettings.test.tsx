// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ProfileContext } from '@/contexts/profile-context';
import { ChatInterfaceSettings } from './ChatInterfaceSettings';
import { CHAT_MODE_STORAGE_KEY } from './chat-mode';
import { profileModes } from './chat-preferences';
import type { useChatMode } from './use-chat-mode';

const observed = vi.hoisted(() => ({ mode: null as ReturnType<typeof useChatMode> | null }));
const getConfig = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api', () => ({ api: { getConfig } }));
vi.mock('./use-chat-mode', async importActual => {
  const actual = await importActual<typeof import('./use-chat-mode')>();
  return { useChatMode: () => { observed.mode = actual.useChatMode(); return observed.mode; } };
});

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  profileModes.clear(); getConfig.mockReset();
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); window.history.replaceState(null, '', '/'); vi.unstubAllGlobals(); });

it.each([
  { browser: 'terminal', search: '', effective: 'terminal' },
  { browser: '', search: '?chat_mode=native', effective: 'native' },
])('reports unknown profile data despite a usable override: $effective $search', async ({ browser, search, effective }) => {
  window.history.replaceState(null, '', `/config${search}`);
  if (browser) localStorage.setItem(CHAT_MODE_STORAGE_KEY, browser);
  getConfig.mockRejectedValueOnce(new Error('offline'));
  await act(async () => root.render(<MemoryRouter><ProfileContext.Provider value={{ profile: 'review', currentProfile: 'review', profiles: [], setProfile: () => {} }}><ChatInterfaceSettings /></ProfileContext.Provider></MemoryRouter>));
  expect(observed.mode).toMatchObject({ effective, loaded: true, profileKnown: false });
  expect(container.textContent).toContain('Profile default: unavailable');
  expect(container.textContent).not.toContain('Profile default: native');
  getConfig.mockResolvedValueOnce({ dashboard: { chat: { default_mode: 'terminal' } } });
  await act(async () => observed.mode!.retry());
  expect(getConfig).toHaveBeenCalledTimes(2);
  expect(observed.mode).toMatchObject({ effective, profileKnown: true });
  expect(container.textContent).toContain('Profile default: terminal');
});

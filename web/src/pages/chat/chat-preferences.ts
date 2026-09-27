import { atom } from 'nanostores';
import { api } from '@/lib/api';
import { CHAT_MODE_STORAGE_KEY, normalizeChatMode, type ChatMode } from './chat-mode';
import type { ChatSwitch, SwitchState } from './chat-switch';

function readPreference() { try { return normalizeChatMode(localStorage.getItem(CHAT_MODE_STORAGE_KEY)); } catch { return null; } }
export const $browserMode = atom<ChatMode | null>(readPreference());
export const $preferenceNotice = atom('');
export const $chatHost = atom<{ profile: string; machine: ChatSwitch; state: SwitchState } | null>(null);
export const $profileModes = atom<Record<string, { loaded: boolean; mode: ChatMode | null; error: boolean }>>({});
const loading = new Map<string, Promise<void>>();
export function loadProfileMode(profile: string): Promise<void> {
  const pending = loading.get(profile); if (pending) return pending;
  const request = api.getConfig(profile).then(config => {
    const dashboard = config.dashboard as { chat?: { default_mode?: unknown } } | undefined;
    $profileModes.set({ ...$profileModes.get(), [profile]: { loaded: true, mode: normalizeChatMode(dashboard?.chat?.default_mode), error: false } });
  }).catch(() => {
    $profileModes.set({ ...$profileModes.get(), [profile]: { loaded: false, mode: null, error: true } });
  }).finally(() => loading.delete(profile));
  loading.set(profile, request); return request;
}
export function commitBrowserMode(mode: ChatMode | null) {
  $browserMode.set(mode);
  try {
    if (mode) localStorage.setItem(CHAT_MODE_STORAGE_KEY, mode); else localStorage.removeItem(CHAT_MODE_STORAGE_KEY);
    $preferenceNotice.set('');
  } catch { $preferenceNotice.set('Your choice works on this page, but the browser could not save this preference.'); }
}
let preferenceRequest = 0;
export async function requestBrowserMode(profile: string, mode: ChatMode | null) {
  const request = ++preferenceRequest;
  const originalHost = $chatHost.get()?.machine;
  if (mode === null && !$profileModes.get()[profile]?.loaded) await loadProfileMode(profile);
  if (request !== preferenceRequest || (originalHost && $chatHost.get()?.machine !== originalHost)) return;
  const setting = $profileModes.get()[profile];
  if (mode === null && !setting?.loaded) { $preferenceNotice.set('Profile default unavailable. Retry loading configuration.'); return; }
  const host = $chatHost.get();
  if (host?.profile === profile) {
    if (!host.machine.request(mode || setting?.mode || 'native', () => commitBrowserMode(mode))) {
      $preferenceNotice.set('Finish or cancel the current switch before changing preference.');
    }
  } else commitBrowserMode(mode);
}

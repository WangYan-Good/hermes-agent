import { readBrowserChatMode, writeBrowserChatMode, type ChatMode } from './chat-mode';

const listeners = new Set<() => void>();
let revision = 0;
export let preferenceNotice = '';
let browser: ChatMode | null | undefined;
export const preferenceRevision = () => revision;
export const subscribePreferences = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
export function notifyPreferences() { revision++; listeners.forEach(fn => fn()); }
export const browserPreference = () => browser === undefined ? readBrowserChatMode() : browser;
export function commitBrowserPreference(mode: ChatMode | null) {
  browser = mode;
  const saved = writeBrowserChatMode(mode);
  preferenceNotice = saved ? '' : 'Interface changed for this page. Browser preference could not be saved.';
  notifyPreferences();
  return saved;
}
export const profileModes = new Map<string, { mode: ChatMode | null; version: number }>();
export function publishProfileMode(profile: string, mode: ChatMode | null) {
  profileModes.set(profile, { mode, version: revision + 1 }); notifyPreferences();
}
interface Host { request(mode: ChatMode | null): void; status: string; source: 'url' | 'browser' | 'profile' | 'default' }
export const chatHosts = new Map<string, Host>();
export function requestBrowserPreference(profile: string, mode: ChatMode | null) {
  const host = chatHosts.get(profile);
  if (host) host.request(mode);
  else commitBrowserPreference(mode);
}

export type ChatMode = 'native' | 'terminal';
export const DEFAULT_CHAT_MODE: ChatMode = 'native';
export const CHAT_MODE_STORAGE_KEY = 'hermes.dashboard.chat.mode';
export function normalizeChatMode(value: unknown): ChatMode | null {
  return value === 'native' || value === 'terminal' ? value : null;
}
export interface ModeResolution { mode: ChatMode; source: 'url' | 'browser' | 'profile' | 'default' }
export function resolveChatMode(url: unknown, browser: unknown, profile: unknown): ModeResolution {
  for (const [source, value] of [['url', url], ['browser', browser], ['profile', profile]] as const) {
    const mode = normalizeChatMode(value);
    if (mode) return { mode, source };
  }
  return { mode: DEFAULT_CHAT_MODE, source: 'default' };
}

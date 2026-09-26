export type ChatMode = "native" | "terminal";
export const DEFAULT_CHAT_MODE: ChatMode = "native";
export const CHAT_MODE_STORAGE_KEY = "hermes.dashboard.chat.mode";
export function normalizeChatMode(value: unknown): ChatMode | null {
  return value === "native" || value === "terminal" ? value : null;
}
export interface ChatModeRequest { urlMode?: unknown; browserMode?: unknown; serverMode?: unknown }
export interface ChatModeResolution { requested: ChatMode; effective: ChatMode; source: "url" | "browser" | "profile" | "default" }
export function resolveChatMode({ urlMode, browserMode, serverMode }: ChatModeRequest = {}): ChatModeResolution {
  const sources = [["url", urlMode], ["browser", browserMode], ["profile", serverMode]] as const;
  for (const [source, value] of sources) {
    const mode = normalizeChatMode(value);
    if (mode) return { requested: mode, effective: mode, source };
  }
  return { requested: DEFAULT_CHAT_MODE, effective: DEFAULT_CHAT_MODE, source: "default" };
}
export function readBrowserChatMode(): ChatMode | null {
  try { return normalizeChatMode(window.localStorage.getItem(CHAT_MODE_STORAGE_KEY)); } catch { return null; }
}
export function writeBrowserChatMode(mode: ChatMode | null): boolean {
  try {
    if (mode) window.localStorage.setItem(CHAT_MODE_STORAGE_KEY, mode);
    else window.localStorage.removeItem(CHAT_MODE_STORAGE_KEY);
    return true;
  } catch { return false; }
}

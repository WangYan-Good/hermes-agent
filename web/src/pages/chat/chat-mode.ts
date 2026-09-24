export type ChatMode = "native" | "terminal";

export const DEFAULT_CHAT_MODE: ChatMode = "terminal";
export const CHAT_MODE_STORAGE_KEY = "hermes.dashboard.chat.mode";

export function normalizeChatMode(value: unknown): ChatMode | null {
  return value === "native" || value === "terminal" ? value : null;
}

export interface ChatModeRequest {
  urlMode?: unknown;
  browserMode?: unknown;
  serverMode?: unknown;
  nativeAvailable?: boolean;
}

export interface ChatModeResolution {
  requested: ChatMode;
  effective: ChatMode;
}

/** Mode selects presentation only; it must never select agent/session state. */
export function resolveChatMode({
  urlMode,
  browserMode,
  serverMode,
  nativeAvailable = false,
}: ChatModeRequest = {}): ChatModeResolution {
  const requested =
    normalizeChatMode(urlMode) ??
    normalizeChatMode(browserMode) ??
    normalizeChatMode(serverMode) ??
    DEFAULT_CHAT_MODE;
  return {
    requested,
    effective: requested === "native" && !nativeAvailable ? "terminal" : requested,
  };
}

/** Read-only seam: no producer or switching UI until UI-P6. */
export function readBrowserChatMode(): ChatMode | null {
  try {
    return normalizeChatMode(window.localStorage.getItem(CHAT_MODE_STORAGE_KEY));
  } catch {
    // SSR, private browsing, or storage policy: use the next resolver source.
    return null;
  }
}

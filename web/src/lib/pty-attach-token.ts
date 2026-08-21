// Stable per-browser token identifying this chat tab's keep-alive PTY.
// sessionStorage deliberately scopes identity to one browser tab: refresh
// reuses it, while a new tab gets an independent PTY and cannot evict the
// first tab's live attachment. `rotate` is the explicit force-fresh path.
const PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";
const PTY_EVENT_CHANNEL_KEY_PREFIX = "hermes.pty.channel.chat.";

function randomHexToken(): string {
  const bytes = new Uint8Array(16);

  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

export function ptyAttachToken(
  rotate = false,
  storage: Storage = window.sessionStorage,
): string {
  let token = "";

  if (!rotate) {
    try {
      token = storage.getItem(PTY_ATTACH_TOKEN_KEY) ?? "";
    } catch {
      // Private mode or storage policy may block access.
    }
  }

  if (!token) {
    token = randomHexToken();
    try {
      storage.setItem(PTY_ATTACH_TOKEN_KEY, token);
    } catch {
      // An in-memory token still gives this mount a stable connection id.
    }
  }

  return token;
}

/**
 * Stable event-publisher channel for one tab and one chat scope.
 *
 * The PTY attach token deliberately survives a browser refresh, so the
 * existing Ink process and its `/api/pub` publisher survive too.  The browser
 * `/api/events` subscriber must reconnect to that same channel; generating a
 * new channel on every React mount silently strands sidebar events after a
 * refresh even though the terminal itself reattaches successfully.
 */
export function ptyEventChannel(
  scope: string,
  storage: Storage = window.sessionStorage,
): string {
  const key = `${PTY_EVENT_CHANNEL_KEY_PREFIX}${encodeURIComponent(scope)}`;
  let channel = "";

  try {
    channel = storage.getItem(key) ?? "";
  } catch {
    // Private mode or storage policy may block access.
  }

  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(channel)) {
    channel = `${scope ? "chat" : "chat-fresh"}-${randomHexToken()}`;
    try {
      storage.setItem(key, channel);
    } catch {
      // Keep the in-memory channel for this mount.
    }
  }

  return channel;
}

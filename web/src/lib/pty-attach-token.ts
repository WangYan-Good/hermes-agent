// Stable per-browser token identifying this chat tab's keep-alive PTY.
// sessionStorage deliberately scopes identity to one browser tab: refresh
// reuses it, while a new tab gets an independent PTY and cannot evict the
// first tab's live attachment. `rotate` is the explicit force-fresh path.
const PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";

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
    const bytes = new Uint8Array(16);

    crypto.getRandomValues(bytes);
    token = Array.from(bytes, (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
    try {
      storage.setItem(PTY_ATTACH_TOKEN_KEY, token);
    } catch {
      // An in-memory token still gives this mount a stable connection id.
    }
  }

  return token;
}

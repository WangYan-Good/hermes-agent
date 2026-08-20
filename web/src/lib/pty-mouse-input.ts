import { shouldBlockPtyInput, type PtyConnectionState } from "./pty-reconnect";

export type PtyInputClassification = "keyboard" | "sgr-other" | "sgr-wheel";

// eslint-disable-next-line no-control-regex -- SGR reports begin with a literal ESC byte
const SGR_MOUSE_RE = /^\x1b\[<(\d+);(\d+);(\d+)([Mm])$/;
const SGR_MOTION_BIT = 32;
const SGR_WHEEL_BIT = 64;
const WS_OPEN = 1;

export function classifyPtyInput(data: string): PtyInputClassification {
  const match = SGR_MOUSE_RE.exec(data);

  if (!match) {
    return "keyboard";
  }

  const button = Number(match[1]);
  const action = match[4];
  const isWheel =
    action === "M" &&
    (button & SGR_WHEEL_BIT) !== 0 &&
    (button & SGR_MOTION_BIT) === 0;

  return isWheel ? "sgr-wheel" : "sgr-other";
}

export function shouldForwardClassifiedPtyInput(
  input: PtyInputClassification,
  socketReadyState: number,
  ptyState: PtyConnectionState,
): boolean {
  return (
    input !== "sgr-other" &&
    socketReadyState === WS_OPEN &&
    !shouldBlockPtyInput(ptyState)
  );
}

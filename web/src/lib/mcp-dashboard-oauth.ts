import type { McpOAuthFlow } from "./api";

interface CompleteOptions {
  serverName: string;
  start: (name: string) => Promise<McpOAuthFlow>;
  status: (flowId: string) => Promise<McpOAuthFlow>;
  open: (url?: string | URL, target?: string, features?: string) => unknown;
  sleep?: (milliseconds: number) => Promise<void>;
  maxPollFailures?: number;
  signal?: AbortSignal;
  cancel?: (flowId: string) => Promise<unknown>;
  cancelOnAbort?: boolean;
}
export class McpOAuthCancelled extends Error {
  constructor() { super("OAuth cancelled"); }
}
const defaultSleep = (milliseconds: number) => new Promise<void>(resolve => window.setTimeout(resolve, milliseconds));

export async function completeMcpDashboardOAuth({ serverName, start, status, open, sleep = defaultSleep, maxPollFailures = 3, signal, cancel, cancelOnAbort = true }: CompleteOptions): Promise<McpOAuthFlow> {
  // Synchronous user gesture, before any await.
  const authWindow = open("about:blank", "_blank") as Window | null;
  if (!authWindow) throw new Error("OAuth popup was blocked — allow popups for this dashboard and retry");
  authWindow.opener = null;
  let flowId: string | undefined;
  let cancelSent = false;
  let windowClosed = false;
  const closeWindow = () => { if (!windowClosed) { windowClosed = true; authWindow.close?.(); } };
  const cancelFlow = () => {
    closeWindow();
    if (flowId && !cancelSent && cancel) { cancelSent = true; void cancel(flowId).catch(() => undefined); }
  };
  const onAbort = () => { if (cancelOnAbort) cancelFlow(); };
  const check = () => { if (signal?.aborted) { onAbort(); throw new McpOAuthCancelled(); } };
  signal?.addEventListener("abort", onAbort, { once: true });
  try {
    check();
    const started = await start(serverName);
    flowId = started.flow_id;
    check();
    if (started.status === "approved") return started;
    if (started.status === "error") throw new Error(started.error || "OAuth failed to start");
    if (!started.authorization_url) throw new Error("OAuth server did not provide an authorization URL");
    authWindow.location.href = started.authorization_url;
    let failures = 0;
    for (;;) {
      check();
      let current: McpOAuthFlow;
      try { current = await status(started.flow_id); failures = 0; }
      catch (error) { check(); if (++failures >= maxPollFailures) throw error; await sleep(1000); continue; }
      check();
      if (current.status === "approved") return current;
      if (current.status === "error") throw new Error(current.error || "OAuth authorization failed");
      if (authWindow.closed) throw new Error("OAuth authorization window was closed before completion");
      await sleep(1000);
    }
  } catch (error) {
    if (!signal?.aborted || cancelOnAbort) cancelFlow();
    throw error;
  } finally {
    signal?.removeEventListener("abort", onAbort);
    if (!signal?.aborted || cancelOnAbort) closeWindow();
  }
}

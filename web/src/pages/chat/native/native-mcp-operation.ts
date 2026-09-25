import { api, type McpSetupOperation } from "@/lib/api";
import { McpOAuthCancelled } from "@/lib/mcp-dashboard-oauth";
import type { NativeInteraction } from "./native-interactions";
import type { NativeSession } from "./native-session";

export type McpRequest = Extract<NativeInteraction, { kind: "mcp.setup" }>;
export interface McpOutcome { status: "installed" | "enabled" | "authorized" | "declined" | "error"; detail?: string; tools?: string[] }
export const setupBinding = (r: McpRequest) => ({ session_id: r.runtimeId, request_id: r.requestId });

export async function settleMcp(session: NativeSession, r: McpRequest, decided: { current: boolean }, signal: AbortSignal | undefined, outcome: McpOutcome) {
  const current = () => !decided.current && session.isCurrent(r) && (outcome.status === "declined" || !signal?.aborted);
  if (!current()) return;
  if (["installed", "enabled", "authorized"].includes(outcome.status)) await session.reloadMcp(r);
  if (!current()) return;
  decided.current = true;
  await session.respondMcpSetup(r, { ...outcome, server: r.server });
}

/** Only read an accepted operation. No install/auth POST exists on this path. */
export async function waitForMcpOperation(session: NativeSession, r: McpRequest, initial: McpSetupOperation, signal: AbortSignal): Promise<McpOutcome> {
  let operation = initial;
  const check = () => { if (signal.aborted || !session.isCurrent(r)) throw new McpOAuthCancelled(); };
  const deadline = Date.now() + 600_000;
  for (;;) {
    check();
    if (operation.state === "starting") {
      const result = await api.getMcpSetupOperation(setupBinding(r), operation.profile); check();
      if (!result.operation || result.operation.id !== operation.id || result.operation.kind !== operation.kind || result.operation.profile !== operation.profile) throw new Error("Operation identity unavailable");
      operation = result.operation;
      session.rememberMcpOperation(r, operation);
    }
    if (operation.state === "failed") return { status: "error", detail: "MCP setup could not start. Check MCP settings before retrying." };
    if (operation.state === "running" && operation.kind === "install") {
      const status = await api.getActionStatus(operation.id, 0); check();
      if (!status.running) return status.exit_code === 0 ? { status: "installed" } : { status: "error", detail: "MCP installation failed." };
    }
    if (operation.state === "running" && operation.kind === "authorize") {
      const flow = await api.getMcpOAuthFlow(operation.id); check();
      if (flow.status === "approved") return { status: "authorized", ...(flow.tools ? { tools: flow.tools.map(t => t.name) } : {}) };
      if (flow.status === "error") return { status: "error", detail: "Authorization ended without success. Check MCP settings." };
    }
    if (Date.now() >= deadline) throw new Error("Operation is still pending");
    await new Promise<void>(resolve => {
      const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
      const timer = setTimeout(done, 1000);
      signal.addEventListener("abort", done, { once: true });
      if (signal.aborted) done();
    });
  }
}

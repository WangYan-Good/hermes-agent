import type { GatewayEvent } from "@hermes/shared";
import { record, string } from "./native-events";
import type { NativeSessionResponse } from "./native-types";
import type { McpSetupOperation } from "@/lib/api";

export type ApprovalChoice = "once" | "session" | "always" | "deny";
export type InteractionKind = "approval" | "clarify" | "secret" | "sudo" | "mcp.setup";
export type InteractionPhase = "pending" | "submitting" | "uncertain" | "resolved" | "expired";
interface Binding { key: string; requestId: string; runtimeId: string; generation: number; phase: InteractionPhase; error?: string }
export type NativeInteraction = Binding & (
  | { kind: "approval"; command: string; description: string; choices: ApprovalChoice[] }
  | { kind: "clarify"; question: string; choices: string[]; multiSelect: boolean }
  | { kind: "secret"; envVar: string; prompt: string }
  | { kind: "sudo" }
  | { kind: "mcp.setup"; server: string; action: string; reason: string; operation?: McpSetupOperation }
);
export type Interactions = Record<string, NativeInteraction>;
export const activeInteraction = (r: NativeInteraction) => !["resolved", "expired"].includes(r.phase);
export const hasInteraction = (rs: Interactions) => Object.values(rs).some(activeInteraction);
export function readMcpOperation(value: unknown): McpSetupOperation | undefined {
  const p = record(value);
  if ((p.kind !== "install" && p.kind !== "authorize") || typeof p.id !== "string" || !/^[A-Za-z0-9_-]{1,160}$/.test(p.id) || (p.state !== "starting" && p.state !== "running" && p.state !== "failed") || typeof p.profile !== "string") return undefined;
  return { kind: p.kind, id: p.id, state: p.state, profile: p.profile };
}
export function normalizeChoices(value: unknown): string[] {
  return Array.isArray(value) ? [...new Set(value.filter((v): v is string => typeof v === "string" && Boolean(v.trim())))] : [];
}
export function approvalChoices(p: Record<string, unknown>): ApprovalChoice[] {
  const canonical: ApprovalChoice[] = ["once", "session", "always", "deny"];
  // An explicit choices payload is authoritative even when empty/malformed.
  const offered = p.choices === undefined ? canonical : normalizeChoices(p.choices);
  return canonical.filter(c => offered.includes(c) && !(p.smart_denied === true && (c === "session" || c === "always")) && !(p.allow_permanent === false && c === "always"));
}
export function readInteraction(event: GatewayEvent, generation: number): NativeInteraction | null {
  if (!event.session_id || !event.type.endsWith(".request")) return null;
  const p = record(event.payload);
  const kind = event.type.slice(0, -8) as InteractionKind;
  const requestId = string(p.request_id);
  if (!requestId && kind !== "approval") return null;
  const binding: Binding = { requestId, runtimeId: event.session_id, generation, key: `${kind}:${requestId || "legacy"}`, phase: "pending" };
  switch (kind) {
    case "approval": return { ...binding, kind, command: string(p.command), description: string(p.description), choices: approvalChoices(p) };
    case "clarify": return { ...binding, kind, question: string(p.question), choices: normalizeChoices(p.choices), multiSelect: p.multi_select === true };
    case "secret": return { ...binding, kind, envVar: string(p.env_var), prompt: string(p.prompt) };
    case "sudo": return { ...binding, kind };
    case "mcp.setup": return { ...binding, kind, server: string(p.server), action: string(p.action), reason: string(p.reason), operation: readMcpOperation(p.operation) };
    default: return null;
  }
}
export function reduceInteractions(state: Interactions, event: GatewayEvent, generation: number): Interactions {
  const request = readInteraction(event, generation);
  if (request) {
    const old = state[request.key];
    // Replay is not another request and must not revive a settled answer.
    if (old?.generation === generation && old.runtimeId === request.runtimeId) {
      if (!activeInteraction(old)) return state;
      // The original request event may have been buffered before a resume
      // snapshot recorded the operation. It cannot erase accepted identity.
      if (old.kind === "mcp.setup" && request.kind === "mcp.setup" && old.operation) {
        request.operation = old.operation;
      }
      const next = { ...request, phase: old.phase, error: old.error };
      return JSON.stringify(next) === JSON.stringify(old) ? state : { ...state, [request.key]: next };
    }
    return { ...state, [request.key]: request };
  }
  if (!event.type.endsWith(".expire")) return state;
  const id = string(record(event.payload).request_id);
  const key = `${event.type.slice(0, -7)}:${id}`;
  const old = state[key];
  if (!id || !old || old.generation !== generation || old.runtimeId !== event.session_id || !activeInteraction(old)) return state;
  return { ...state, [key]: { ...old, phase: "expired", error: undefined } };
}
export function recoverInteractions(response: NativeSessionResponse, generation: number): Interactions {
  let state: Interactions = {};
  const events = Array.isArray(response.pending_interactions) ? [...response.pending_interactions] : response.pending_clarify ? [{ type: "clarify.request", payload: response.pending_clarify }] : [];
  if (response.pending_approval) events.unshift({ type: "approval.request", payload: response.pending_approval });
  for (const event of events) state = reduceInteractions(state, { ...event, session_id: response.session_id }, generation);
  return state;
}

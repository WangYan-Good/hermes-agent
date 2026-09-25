import type { GatewayEvent } from "@hermes/shared";
import { record, string } from "./native-events";
import type { NativeSessionResponse } from "./native-types";

export interface NativeTodo { id: string; content: string; status: string }
export interface NativeSubagent { id: string; goal: string; status: string; activity: string; complete: boolean }
export interface NativeControl {
  submitting: boolean;
  queued: string | null;
  notice: string;
  usage: Record<string, number>;
  todos: NativeTodo[];
  subagents: Record<string, NativeSubagent>;
}
export const emptyControl = (): NativeControl => ({ submitting: false, queued: null, notice: "", usage: {}, todos: [], subagents: {} });
const usageKeys = ["calls", "input", "output", "total", "context_percent", "context_used", "context_max", "cost_usd"];
function usage(value: unknown): Record<string, number> {
  const source = record(value);
  return Object.fromEntries(usageKeys.flatMap(key => typeof source[key] === "number" && Number.isFinite(source[key]) ? [[key, source[key]]] : []));
}
export function recoverControl(response: NativeSessionResponse): NativeControl {
  return { ...emptyControl(), queued: string(response.queued?.user) || null, usage: usage(response.info?.usage) };
}
export function reduceControl(state: NativeControl, event: GatewayEvent): NativeControl {
  const p = record(event.payload);
  if (event.type === "session.usage" || event.type === "session.info") return { ...state, usage: { ...state.usage, ...usage(p.usage) } };
  if (event.type === "tool.complete" && p.name === "todo" && Array.isArray(p.todos) && !p.error && !record(p.result).error && record(p.result).success !== false) {
    const todos = p.todos.flatMap((value, i) => {
      const t = record(value);
      return ["pending", "in_progress", "completed", "cancelled"].includes(string(t.status)) && typeof t.content === "string" ? [{ id: string(t.id) || String(i), content: t.content, status: string(t.status) }] : [];
    });
    return { ...state, todos };
  }
  if (!["subagent.spawn_requested", "subagent.start", "subagent.thinking", "subagent.tool", "subagent.progress", "subagent.complete"].includes(event.type)) return state;
  const id = string(p.subagent_id) || string(p.child_session_id) || `task-${p.task_index ?? 0}`;
  const old = state.subagents[id];
  if (old?.complete) return state;
  const complete = event.type === "subagent.complete";
  const status = string(p.status);
  const child: NativeSubagent = { id, goal: string(p.goal) || old?.goal || "", complete, status: complete ? (["error", "failed", "timeout", "cancelled", "canceled"].includes(status) ? status : "completed") : status || "running", activity: string(p.summary) || string(p.text) || string(p.tool_preview) || string(p.tool_name) || old?.activity || "" };
  return { ...state, subagents: { ...state.subagents, [id]: child } };
}

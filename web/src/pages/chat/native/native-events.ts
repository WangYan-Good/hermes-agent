import type { GatewayEvent } from "@hermes/shared";
import type { NativeConversationState, NativeMessage, NativePart } from "./native-types";

export function emptyConversation(): NativeConversationState {
  return { messages: [], running: false, activeId: null, status: "", error: null, nextId: 1 };
}

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}
export function string(value: unknown): string { return typeof value === "string" ? value : ""; }

export function beginPrompt(state: NativeConversationState, text: string): NativeConversationState {
  const next: NativeConversationState = {
    ...state, running: true, activeId: null, error: null, status: "Sending…",
    nextId: state.nextId + 1,
    messages: [...state.messages, { id: `user-${state.nextId}`, role: "user", parts: [{ type: "text", text }] }],
  };
  return assistant(next)[0];
}

function assistant(state: NativeConversationState): [NativeConversationState, NativeMessage] {
  const current = state.messages.find(m => m.id === state.activeId);
  if (current) return [state, current];
  const message: NativeMessage = { id: `assistant-${state.nextId}`, role: "assistant", parts: [], pending: true };
  return [{ ...state, nextId: state.nextId + 1, activeId: message.id, messages: [...state.messages, message] }, message];
}

function update(state: NativeConversationState, message: NativeMessage): NativeConversationState {
  return { ...state, messages: state.messages.map(m => m.id === message.id ? message : m) };
}

export function failConversation(state: NativeConversationState, error: string): NativeConversationState {
  return {
    ...state, running: false, status: "", error,
    messages: state.messages.map(m => m.id === state.activeId ? { ...m, pending: false, error, parts: m.parts.map(p => p.type === "tool" && p.status === "running" ? { ...p, status: "error" } : p) } : m),
  };
}


/** Pure, forward-compatible reducer. Routing and transport live outside it. */
export function reduceNativeEvent(state: NativeConversationState, event: GatewayEvent): NativeConversationState {
  const p = record(event.payload);
  const text = string(p.text);
  if (event.type === "error") return failConversation(state, string(p.message) || "Gateway error");
  // The current gateway thinking callback carries spinner/status snapshots,
  // including an empty string to clear it; it is not reasoning token data.
  if (event.type === "thinking.delta") return { ...state, status: text };
  if (event.type === "status.update") return { ...state, status: text || string(p.status) };
  if (event.type === "session.info") {
    if (p.running === false) return { ...state, running: false, status: "", messages: state.messages.map(m => m.pending ? { ...m, pending: false } : m) };
    return state;
  }
  if (event.type === "tool.generating" || (event.type === "tool.progress" && !string(p.tool_id))) return { ...state, status: string(p.preview) || `Preparing ${string(p.name) || "tool"}…` };
  const supported = ["message.start", "message.delta", "message.interim", "message.complete", "reasoning.delta", "thinking.delta", "tool.start", "tool.progress", "tool.complete"];
  if (!supported.includes(event.type)) return state;
  // Late duplicate completion is idempotent; do not open another bubble.
  if (event.type === "message.complete" && !state.running && state.activeId) {
    const current = state.messages.find(m => m.id === state.activeId);
    if (current?.parts.some(part => part.final && part.text === text) && !p.error) return state;
  }
  if (event.type.startsWith("tool.") && !string(p.tool_id) && !string(p.tool_call_id) && !string(p.id)) return state;
  let message: NativeMessage;
  [state, message] = assistant(state);
  let parts = [...message.parts];
  let running = true;
  let error: string | undefined;
  if (event.type === "message.start") {
    return update({ ...state, running: true, status: "Thinking…", error: null }, { ...message, pending: true });
  }
  if (event.type === "message.delta" || event.type === "reasoning.delta") {
    const type = event.type === "message.delta" ? "text" : "reasoning";
    const last = parts.at(-1);
    if (last?.type === type && !last.sealed) parts[parts.length - 1] = { ...last, text: last.text + text };
    else if (text) parts.push({ type, text });
  } else if (event.type === "message.interim") {
    const last = parts.at(-1);
    if (p.already_streamed && last?.type === "text" && !last.sealed) parts[parts.length - 1] = { ...last, text: text || last.text, sealed: true };
    else if (text) parts.push({ type: "text", text, sealed: true });
  } else if (event.type === "message.complete") {
    running = false;
    const lastText = parts.findLastIndex(part => part.type === "text");
    if (p.response_previewed && lastText >= 0 && parts[lastText].text === text) {
      parts[lastText] = { ...parts[lastText], sealed: true, final: true };
    } else {
      // Only the unsealed final segment is replaced; keep commentary/tools.
      parts = parts.filter(part => part.type !== "text" || (part.sealed && !part.final));
      if (text) parts.push({ type: "text", text, sealed: true, final: true });
    }
    const reasoning = string(p.reasoning);
    if (reasoning && !parts.some(part => part.type === "reasoning" && part.text === reasoning)) {
      const i = parts.findLastIndex(part => part.type === "reasoning");
      if (i >= 0) parts[i] = { ...parts[i], text: reasoning };
      else parts.unshift({ type: "reasoning", text: reasoning });
    }
    if (p.status === "error" || p.error) error = string(p.error) || "The turn failed. You can retry after reconnecting.";
    parts = parts.map(part => part.type === "tool" && part.status === "running" ? { ...part, status: error || p.status === "interrupted" ? "error" : "complete" } : part);
  } else {
    const id = string(p.tool_id) || string(p.tool_call_id) || string(p.id);
    const name = string(p.name) || "tool";
    if (!id) return state; // Never correlate a completion by tool name.
    const i = parts.findIndex(part => part.type === "tool" && part.id === id);
    if (i >= 0 && parts[i].status !== "running") return state;
    const result = record(p.result);
    const tool: NativePart = {
      type: "tool", id: id || parts[i]?.id || `tool-${state.nextId}-${parts.length}`, name: string(p.name) || parts[i]?.name || name,
      status: event.type === "tool.complete" ? (p.error || result.error || result.success === false ? "error" : "complete") : "running",
      text: (string(p.summary) || string(p.preview) || string(p.context) || parts[i]?.text || "").slice(0, 180),
    };
    if (i >= 0) parts[i] = tool;
    else parts.push(tool);
  }
  return update({ ...state, running, error: error ?? null, status: running ? state.status : "" }, { ...message, parts, pending: running, error });
}

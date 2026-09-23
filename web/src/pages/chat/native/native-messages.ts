import type { GatewayEvent } from "@hermes/shared";
import { emptyConversation, record, reduceNativeEvent, string } from "./native-events";
import type { NativeConversationState, NativeHistoryMessage, NativeMessage, NativeSessionResponse } from "./native-types";

const syntheticDisplayKinds = new Set(["model_switch", "personality_switch", "auto_continue", "async_delegation_complete"]);

function isSyntheticDisplayRow(row: NativeHistoryMessage): boolean {
  return syntheticDisplayKinds.has(row.display_kind ?? "");
}

function reasoningText(row: Record<string, unknown>): string {
  for (const key of ["reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items"]) {
    const value = row[key];
    if (typeof value === "string" && value) return value;
    if (Array.isArray(value)) {
      const text = value.map(item => string(record(item).text) || (Array.isArray(record(item).summary) ? (record(item).summary as unknown[]).map(s => string(record(s).text)).join("\n") : "")).filter(Boolean).join("\n");
      if (text) return text;
    }
  }
  return "";
}

export function hydrateNativeHistory(response: NativeSessionResponse): NativeConversationState {
  const messages: NativeMessage[] = [];
  for (const [index, row] of (response.messages ?? []).entries()) {
    // Provider role is not display attribution. MVP omits system timeline
    // entries using the gateway's structured contract, never a text heuristic.
    if (row.role === "system" || row.display_kind === "hidden" || isSyntheticDisplayRow(row)) continue;
    let message = messages.at(-1);
    if (row.role === "user" || !message || message.role !== "assistant") {
      message = { id: `history-${row.row_id ?? index}-${row.role}`, role: row.role === "user" ? "user" : "assistant", parts: [] };
      messages.push(message);
    }
    if (row.role === "tool") {
      message.parts.push({ type: "tool", id: `history-tool-${index}`, name: row.name || "tool", status: "complete", text: (row.context ?? "").slice(0, 180) });
    } else {
      const reasoning = reasoningText(row as unknown as Record<string, unknown>);
      if (reasoning) message.parts.push({ type: "reasoning", text: reasoning, sealed: true });
      if (row.text) message.parts.push({ type: "text", text: row.text, sealed: true });
    }
  }
  const inflight = response.inflight;
  if (inflight) {
    const lastUser = messages.findLastIndex(m => m.role === "user");
    if (lastUser < 0 || messages[lastUser].parts[0]?.text !== inflight.user) {
      messages.push({ id: "inflight-user", role: "user", parts: [{ type: "text", text: inflight.user }] });
    }
    const last = messages.at(-1);
    // The live snapshot may overlap an already-persisted assistant row.
    if (last?.role === "assistant" && last.parts.filter(p => p.type === "text").map(p => p.text).join("") === inflight.assistant) {
      last.pending = inflight.streaming;
      last.error = inflight.error;
    } else {
      messages.push({ id: "inflight-assistant", role: "assistant", parts: inflight.assistant ? [{ type: "text", text: inflight.assistant }] : [], pending: inflight.streaming, error: inflight.error });
    }
  }
  return {
    ...emptyConversation(), messages, nextId: messages.length + 1,
    running: Boolean(response.running || inflight?.streaming || response.pending_approval || response.pending_clarify),
    activeId: messages.at(-1)?.role === "assistant" ? messages.at(-1)!.id : null,
    error: inflight?.error ?? null,
    blocked: response.pending_approval ? "approval.request" : response.pending_clarify ? "clarify.request" : null,
  };
}

/** A resume snapshot and events can cross on the wire. Reconcile their shared
 * text boundary instead of appending the snapshot to the existing transcript. */
export function reconcileNativeResume(response: NativeSessionResponse, buffered: GatewayEvent[], previous: NativeConversationState): NativeConversationState {
  let state = hydrateNativeHistory(response);
  const events = buffered.filter(e => e.session_id === response.session_id);
  const final = events.findLast(e => e.type === "message.complete" || e.type === "error");
  // If history already contains this terminal frame, its tool/commentary
  // projection is authoritative too; replaying those buffered events would
  // duplicate tools because history's compact tool rows have no live tool_id.
  if (final?.type === "message.complete" && !response.running && !response.inflight &&
      state.messages.at(-1)?.parts.some(p => p.type === "text" && p.text === string(record(final.payload).text))) {
    if (record(final.payload).status === "error") state = reduceNativeEvent({ ...state, running: true }, final);
    return state;
  }
  const oldTail = previous.messages.at(-1);
  const tail = state.messages.at(-1);
  // In-process reconnect keeps observed reasoning/tools, which inflight cannot
  // represent. Never carry them from another session (controller resets it).
  if (response.inflight && tail?.role === "assistant" && oldTail?.role === "assistant") {
    const oldText = oldTail.parts.filter(p => p.type === "text").map(p => p.text).join("");
    const snapshotText = response.inflight.assistant;
    let parts = [...oldTail.parts];
    if (snapshotText.startsWith(oldText)) {
      const extra = snapshotText.slice(oldText.length);
      const last = parts.at(-1);
      if (extra && last?.type === "text" && !last.sealed) parts[parts.length - 1] = { ...last, text: last.text + extra };
      else if (extra) parts.push({ type: "text", text: extra });
    } else if (!oldText.startsWith(snapshotText)) {
      parts = [...parts.filter(p => p.type !== "text"), ...tail.parts.filter(p => p.type === "text")];
    }
    state = { ...state, messages: [...state.messages.slice(0, -1), { ...tail, parts }] };
  }
  const deltas = events.filter(e => e.type === "message.delta").map(e => string(record(e.payload).text)).join("");
  const snapshot = response.inflight?.assistant ?? "";
  let overlap = Math.min(snapshot.length, deltas.length);
  while (overlap > 0 && !snapshot.endsWith(deltas.slice(0, overlap))) overlap--;
  for (const event of events) {
    if (event.type === "message.delta") {
      if (final) continue; // terminal authoritative text includes these deltas
      const text = string(record(event.payload).text);
      const skip = Math.min(overlap, text.length);
      overlap -= skip;
      if (skip < text.length) state = reduceNativeEvent(state, { ...event, payload: { text: text.slice(skip) } });
    } else if (event.type === "message.complete") {
      const finalText = string(record(event.payload).text);
      const current = state.messages.at(-1);
      // A completed history projection already contains the final answer.
      if (!response.inflight && !response.running && current?.parts.some(p => p.type === "text" && p.text === finalText)) {
        state = { ...state, running: false };
      } else state = reduceNativeEvent({ ...state, running: true }, event);
    } else if (event.type !== "message.start" || !state.activeId) state = reduceNativeEvent(state, event);
  }
  return state;
}

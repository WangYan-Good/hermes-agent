import { describe, expect, it } from "vitest";
import { beginPrompt, emptyConversation, reduceNativeEvent } from "./native-events";
import { hydrateNativeHistory, reconcileNativeResume } from "./native-messages";
import type { NativeSessionResponse } from "./native-types";

const event = (type: string, payload: Record<string, unknown> = {}) => ({ type, session_id: "runtime", payload });
const started = () => reduceNativeEvent(beginPrompt(emptyConversation(), "hello"), event("message.start"));

describe("native event contract", () => {
  it("streams into one assistant, retaining settled references and separate reasoning", () => {
    let state = started();
    const user = state.messages[0];
    for (const e of [event("reasoning.delta", { text: "think" }), event("reasoning.delta", { text: " more" }), event("message.delta", { text: "Hel" }), event("message.delta", { text: "lo" })]) state = reduceNativeEvent(state, e);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[0]).toBe(user);
    expect(state.messages[1].parts).toEqual([{ type: "reasoning", text: "think more" }, { type: "text", text: "Hello" }]);
    state = reduceNativeEvent(state, event("message.complete", { text: "Hello!", reasoning: "think more" }));
    expect(state.running).toBe(false);
    expect(state.messages[1].parts.filter(p => p.type === "text")).toEqual([{ type: "text", text: "Hello!", sealed: true, final: true }]);
    expect(reduceNativeEvent(state, event("message.complete", { text: "Hello!" }))).toBe(state);
  });
  it("seals already-streamed commentary and updates the same tool by identity", () => {
    let state = started();
    for (const e of [event("message.delta", { text: "Checking" }), event("message.interim", { text: "Checking", already_streamed: true }), event("tool.start", { tool_id: "t", name: "terminal" }), event("tool.progress", { tool_id: "t", name: "terminal", preview: "pwd" }), event("tool.complete", { tool_id: "t", name: "terminal", result: { success: true } }), event("message.complete", { text: "/workspace" })]) state = reduceNativeEvent(state, e);
    expect(state.messages[1].parts.map(p => p.text)).toEqual(["Checking", "pwd", "/workspace"]);
    expect(state.messages[1].parts[1].status).toBe("complete");
  });
  it("does not duplicate a previewed final answer", () => {
    let state = reduceNativeEvent(started(), event("message.interim", { text: "Done" }));
    state = reduceNativeEvent(state, event("message.complete", { text: "Done", response_previewed: true }));
    expect(state.messages[1].parts.filter(p => p.type === "text")).toHaveLength(1);
  });
  it.each(["error", "message.complete"])("settles %s visibly without discarding partial text", type => {
    let state = reduceNativeEvent(started(), event("message.delta", { text: "Partial" }));
    state = reduceNativeEvent(state, event(type, { status: "error", text: "Partial", error: "failed", message: "failed" }));
    expect(state.running).toBe(false);
    expect(state.error).toBe("failed");
    expect(state.messages[1].parts[0].text).toBe("Partial");
  });
  it.each(["approval.request", "clarify.request", "secret.request", "sudo.request", "mcp.setup.request"])("leaves %s outside transcript state", type => {
    const state = reduceNativeEvent(started(), event(type, { secret: "private-value" }));
    expect(state).not.toHaveProperty("blocked");
    expect(state.running).toBe(true);
    expect(JSON.stringify(state)).not.toContain("private-value");
  });
  it("ignores unknown events without affecting reference identity", () => {
    const state = started();
    expect(reduceNativeEvent(state, event("future.event"))).toBe(state);
  });
  it("handles tool errors and status text without dumping raw results", () => {
    let state = reduceNativeEvent(started(), event("tool.complete", { tool_id: "t", name: "terminal", result: { error: "private output" } }));
    state = reduceNativeEvent(state, event("status.update", { text: "Working" }));
    expect(state.status).toBe("Working");
    expect(state.messages[1].parts[0].status).toBe("error");
    expect(JSON.stringify(state)).not.toContain("private output");
  });
});

describe("history and live reconciliation", () => {
  it("hides synthetic display rows while preserving user/skill rows and the next live turn", () => {
    const kinds = ["model_switch", "personality_switch", "auto_continue", "async_delegation_complete", "hidden"];
    const response: NativeSessionResponse = { session_id: "runtime", messages: [
      { role: "user", text: "normal message", row_id: 1 },
      ...kinds.map((display_kind, index) => ({ role: "user" as const, text: `[System: internal ${display_kind}]`, display_kind, row_id: index + 2 })),
      { role: "assistant", text: "normal reply", row_id: 7 },
      { role: "user", text: "[System: user supplied text]", row_id: 8 },
      { role: "user", text: "/my-skill", display_kind: "skill_invocation", row_id: 9 },
      { role: "assistant", text: "skill reply", row_id: 10 },
    ] };
    let state = hydrateNativeHistory(response);
    expect(state.messages.map(m => m.parts[0].text)).toEqual(["normal message", "normal reply", "[System: user supplied text]", "/my-skill", "skill reply"]);
    expect(JSON.stringify(state)).not.toContain("internal");
    const history = state.messages;
    state = beginPrompt(state, "next");
    state = reduceNativeEvent(state, event("message.delta", { text: "live" }));
    state = reduceNativeEvent(state, event("message.complete", { text: "live reply" }));
    expect(state.messages.slice(0, history.length)).toEqual(history);
    expect(state.messages.at(-1)?.parts[0].text).toBe("live reply");
    expect(new Set(state.messages.map(m => m.id)).size).toBe(state.messages.length);
  });
  const response: NativeSessionResponse = { session_id: "runtime", session_key: "stored", messages: [{ role: "user", text: "hello", row_id: 1 }, { role: "assistant", text: "Checking", reasoning: "think", row_id: 2 }, { role: "tool", name: "terminal", context: "pwd" }, { role: "assistant", text: "Done", row_id: 4 }], running: false };
  it("coalesces tool/commentary/final into one assistant and then streams a new turn", () => {
    let state = hydrateNativeHistory(response);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1].parts.map(p => p.type)).toEqual(["reasoning", "text", "tool", "text"]);
    const settled = state.messages[1];
    state = beginPrompt(state, "next");
    state = reduceNativeEvent(state, event("message.delta", { text: "New" }));
    state = reduceNativeEvent(state, event("message.complete", { text: "New answer" }));
    expect(state.messages).toHaveLength(4);
    expect(state.messages[1]).toBe(settled);
  });
  it("does not duplicate a completed reply when completion beats resume response", () => {
    const state = reconcileNativeResume(response, [event("message.delta", { text: "Done" }), event("message.complete", { text: "Done" })], emptyConversation());
    expect(state.running).toBe(false);
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1].parts.filter(p => p.text === "Done")).toHaveLength(1);
  });
  it("removes overlap between inflight snapshot and buffered deltas", () => {
    const state = reconcileNativeResume({ session_id: "runtime", running: true, messages: [{ role: "user", text: "hello" }], inflight: { user: "hello", assistant: "Hello", streaming: true } }, [event("message.delta", { text: "lo" }), event("message.delta", { text: " world" })], emptyConversation());
    expect(state.messages).toHaveLength(2);
    expect(state.messages[1].parts[0].text).toBe("Hello world");
  });
  it("completion newer than a running snapshot wins and unrelated events are ignored", () => {
    const state = reconcileNativeResume({ session_id: "runtime", running: true, inflight: { user: "hello", assistant: "hel", streaming: true } }, [event("message.complete", { text: "hello" }), { ...event("message.delta", { text: "WRONG" }), session_id: "other" }], emptyConversation());
    expect(state.running).toBe(false);
    expect(state.messages[1].parts.at(-1)?.text).toBe("hello");
  });
  it("restores a failed inflight turn and pending interaction", () => {
    const state = hydrateNativeHistory({ session_id: "runtime", inflight: { user: "hello", assistant: "partial", streaming: false, error: "failure" }, pending_approval: { command: "private" } });
    expect(state.error).toBe("failure");
    expect(state).not.toHaveProperty("blocked");
    expect(JSON.stringify(state)).not.toContain("private");
  });
});

it("treats thinking snapshots as replaceable status without polluting reasoning", () => {
  let state = reduceNativeEvent(started(), event("thinking.delta", { text: "Thinking…" }));
  expect(state.status).toBe("Thinking…");
  state = reduceNativeEvent(state, event("thinking.delta", { text: "" }));
  expect(state.status).toBe("");
  expect(state.messages[1].parts).toEqual([]);
});

it("accepts authoritative completion after a session.info idle notification", () => {
  let state = reduceNativeEvent(started(), event("message.delta", { text: "part" }));
  state = reduceNativeEvent(state, event("session.info", { running: false }));
  state = reduceNativeEvent(state, event("message.complete", { text: "complete answer" }));
  expect(state.messages[1].parts.at(-1)?.text).toBe("complete answer");
  state = reduceNativeEvent(state, event("message.complete", { text: "corrected final" }));
  expect(state.messages[1].parts.filter(p => p.type === "text")).toHaveLength(1);
});

it("does not replay tool completion already projected in authoritative history", () => {
  const state = reconcileNativeResume({ session_id: "runtime", running: false, messages: [{ role: "user", text: "hello" }, { role: "tool", name: "terminal", context: "pwd" }, { role: "assistant", text: "Done" }] }, [event("tool.complete", { tool_id: "t", name: "terminal" }), event("message.complete", { text: "Done" })], emptyConversation());
  expect(state.messages[1].parts.filter(p => p.type === "tool")).toHaveLength(1);
});

it("retains observed commentary and tools across an inflight reconnect and final completion", () => {
  let old = reduceNativeEvent(started(), event("message.interim", { text: "Checking" }));
  old = reduceNativeEvent(old, event("tool.start", { name: "terminal", tool_id: "t" }));
  let state = reconcileNativeResume({ session_id: "runtime", running: true, inflight: { user: "hello", assistant: "CheckingPartial", streaming: true } }, [], old);
  state = reduceNativeEvent(state, event("message.complete", { text: "Final" }));
  expect(state.messages[1].parts.filter(p => p.type === "text").map(p => p.text)).toEqual(["Checking", "Final"]);
  expect(state.messages[1].parts.some(p => p.type === "tool")).toBe(true);
});
it("tool completion is idempotent and idless progress never joins a tool by name", () => {
  let state = started();
  state = reduceNativeEvent(state, event("tool.generating", { name: "terminal" }));
  expect(state.status).toContain("terminal"); expect(state.messages[1].parts).toHaveLength(0);
  state = reduceNativeEvent(state, event("tool.start", { tool_id: "a", name: "terminal" }));
  state = reduceNativeEvent(state, event("tool.start", { tool_id: "b", name: "terminal" }));
  state = reduceNativeEvent(state, event("tool.progress", { name: "terminal", preview: "working" }));
  expect(state.messages[1].parts.every(p => p.text === "")).toBe(true);
  state = reduceNativeEvent(state, event("tool.complete", { tool_id: "a", name: "terminal" }));
  const again = reduceNativeEvent(state, event("tool.complete", { tool_id: "a", name: "terminal" }));
  expect(again).toBe(state); expect(state.messages[1].parts[1].status).toBe("running");
});

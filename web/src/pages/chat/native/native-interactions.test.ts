import { expect, it } from "vitest";
import { approvalChoices, readInteraction, reduceInteractions, recoverInteractions } from "./native-interactions";
import { emptyControl, reduceControl } from "./native-control";
const event = (type: string, payload: unknown, session_id = "s") => ({ type, payload, session_id });
it.each([
  [{}, ["once", "session", "always", "deny"]],
  [{ allow_permanent: false }, ["once", "session", "deny"]],
  [{ smart_denied: true }, ["once", "deny"]],
  [{ choices: ["always", "session"], smart_denied: true }, []],
  [{ choices: ["always", "once"], allow_permanent: false }, ["once"]],
  [{ choices: ["deny", "bogus"] }, ["deny"]],
  [{ choices: "once" }, []],
  [{ choices: [] }, []],
])("only exposes allowed choices: %j", (payload, expected) => expect(approvalChoices(payload)).toEqual(expected));
it.each(["clarify", "secret", "sudo", "mcp.setup"])("recovers only metadata for %s, ignoring values and old expiry", kind => {
  const payload = { request_id: "one", question: "Q", choices: ["a", "", 42, {}, "a", "b"], multi_select: true, env_var: "KEY", prompt: "Enter", server: "test", action: "install", reason: "Needed", value: "PRIVATE", password: "PRIVATE" };
  const r = readInteraction(event(`${kind}.request`, payload), 2)!;
  expect(JSON.stringify(r)).not.toContain("PRIVATE");
  let state = recoverInteractions({ session_id: "s", pending_interactions: [{ type: `${kind}.request`, payload }] }, 2);
  expect(state[r.key]).toEqual(r);
  expect(reduceInteractions(state, event(`${kind}.request`, payload), 2)).toBe(state);
  expect(reduceInteractions(state, event(`${kind}.expire`, { request_id: "wrong" }), 2)).toBe(state);
  expect(reduceInteractions(state, event(`${kind}.expire`, { request_id: "one" }), 1)).toBe(state);
  expect(reduceInteractions(state, event(`${kind}.expire`, { request_id: "one" }, "other"), 2)).toBe(state);
  state = reduceInteractions(state, event(`${kind}.expire`, { request_id: "one" }), 2);
  expect(state[r.key].phase).toBe("expired");
  expect(reduceInteractions(state, event(`${kind}.request`, payload), 2)).toBe(state);
});
it("normalizes clarify choices and handles multiple requests independently", () => {
  const r = readInteraction(event("clarify.request", { request_id: "a", choices: ["a", " ", null, {}, "a", "b"], multi_select: true }), 1);
  expect(r).toMatchObject({ choices: ["a", "b"], multiSelect: true });
  let state = recoverInteractions({ session_id: "s", pending_interactions: ["a", "b"].map(request_id => ({ type: "secret.request", payload: { request_id } })) }, 1);
  state = reduceInteractions(state, event("secret.expire", { request_id: "a" }), 1);
  expect(state["secret:b"].phase).toBe("pending");
});
it("uses complete todo lists and preserves missing usage fields", () => {
  let state = reduceControl(emptyControl(), event("session.usage", { usage: { input: 12, output: undefined } }));
  expect(state.usage).toEqual({ input: 12 });
  state = reduceControl(state, event("session.usage", { usage: { output: 0, cost_usd: 0.4 } }));
  expect(state.usage).toEqual({ input: 12, output: 0, cost_usd: 0.4 });
  const todos = ["pending", "in_progress", "completed", "cancelled"].map((status, id) => ({ id: String(id), content: status, status }));
  state = reduceControl(state, event("tool.complete", { name: "todo", todos }));
  expect(state.todos).toEqual(todos);
  expect(reduceControl(state, event("tool.start", { name: "todo", args: { todos: [] } })).todos).toEqual(todos);
  expect(reduceControl(state, event("tool.complete", { name: "todo", todos: [] })).todos).toEqual([]);
});
it("subagent completion is terminal even when stale progress arrives", () => {
  let state = emptyControl();
  for (const type of ["spawn_requested", "start", "thinking", "tool", "progress"]) state = reduceControl(state, event(`subagent.${type}`, { subagent_id: "child", goal: "Task", text: type }));
  expect(state.subagents.child.activity).toBe("progress");
  state = reduceControl(state, event("subagent.complete", { subagent_id: "child", status: "failed", summary: "Failed" }));
  expect(state.subagents.child).toMatchObject({ status: "failed", complete: true, activity: "Failed" });
  expect(reduceControl(state, event("subagent.progress", { subagent_id: "child", text: "old" }))).toBe(state);
});

it("buffered original MCP requests cannot erase accepted operation metadata", () => {
  const operation = { kind: "install", id: "action-a", state: "running", profile: "work", env: "MCP-PRIVATE-SENTINEL" };
  const payload = { request_id: "a", server: "test", action: "install", operation };
  let state = recoverInteractions({ session_id: "s", pending_interactions: [{ type: "mcp.setup.request", payload }] }, 1);
  state = reduceInteractions(state, event("mcp.setup.request", { request_id: "a", server: "test", action: "install" }), 1);
  expect(state["mcp.setup:a"]).toMatchObject({ operation: { id: "action-a" } });
  expect(JSON.stringify(state)).not.toContain("MCP-PRIVATE-SENTINEL");
});

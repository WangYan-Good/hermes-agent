// @vitest-environment jsdom
import { act, useSyncExternalStore } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NativeInteractions } from "./NativeInteractions";
import { NativeSession } from "./native-session";
import { FakeNativeSocket, flushNative } from "./fake-websocket.test-support";
import { NativeChatRuntime } from "./NativeChatRuntime";
import { NativeThread } from "./NativeThread";
import { ThreadPrimitive } from "@assistant-ui/react";

const api = vi.hoisted(() => ({ getMcpCatalog: vi.fn(), getMcpServers: vi.fn(), installMcpCatalogEntry: vi.fn(), setMcpServerEnabled: vi.fn(), authMcpServer: vi.fn(), getMcpOAuthFlow: vi.fn(), cancelMcpOAuthFlow: vi.fn(), getActionStatus: vi.fn() }));
vi.mock("@/lib/api", () => ({ api, buildWsUrl: async () => "ws://localhost/api/ws" }));
vi.mock("@/lib/dashboard-auth-reload", () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let root: Root, container: HTMLDivElement, session: NativeSession;
function Harness() {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  return <NativeChatRuntime session={session} state={state}><ThreadPrimitive.Root><NativeThread /><NativeInteractions session={session} state={state} /></ThreadPrimitive.Root></NativeChatRuntime>;
}
beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("WebSocket", FakeNativeSocket);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollTo = vi.fn();
  FakeNativeSocket.reset(); vi.clearAllMocks();
  FakeNativeSocket.responder = (r, s) => r.method.endsWith(".respond") ? s.reply(r, { status: "ok", resolved: 1 }) : r.method === "approval.pending" ? s.reply(r, { approvals: [] }) : FakeNativeSocket.defaultResponse(r, s);
  api.getMcpCatalog.mockResolvedValue({ entries: [{ name: "test", source: "catalog", required_env: [{ name: "KEY", prompt: "API key", required: true }] }] });
  api.getMcpServers.mockResolvedValue({ servers: [] });
  api.installMcpCatalogEntry.mockResolvedValue({ ok: true, background: false });
  api.setMcpServerEnabled.mockResolvedValue({ ok: true });
  api.cancelMcpOAuthFlow.mockResolvedValue({ ok: true });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  session = new NativeSession("work", null);
  await act(async () => { session.start(); await flushNative(); root.render(<Harness />); });
});
afterEach(async () => { await act(async () => { root.unmount(); session.stop(); }); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const requests = (method: string) => FakeNativeSocket.requests.filter(r => r.method === method);
async function emit(type: string, payload: unknown) { await act(async () => { FakeNativeSocket.instances.at(-1)!.event(type, payload); await flushNative(); }); }
async function click(label: string) { await act(async () => { [...container.querySelectorAll("button")].find(b => b.textContent === label)!.click(); await flushNative(); }); }
async function input(selector: string, text: string) {
  await act(async () => {
    const node = container.querySelector(selector) as HTMLInputElement;
    Object.getOwnPropertyDescriptor(node.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value")!.set!.call(node, text);
    node.dispatchEvent(new Event("input", { bubbles: true })); node.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
it("approval displays the redacted payload, acknowledges and confirms permanent changes", async () => {
  await emit("approval.request", { request_id: "a", description: "Risk", command: "echo [REDACTED]", choices: ["always", "deny"] });
  expect(container.textContent).toContain("echo [REDACTED]");
  expect(container.textContent).not.toContain("Allow once");
  expect(requests("approval.received")[0].params).toEqual({ session_id: "runtime", request_id: "a" });
  await click("Always allow"); expect(requests("approval.respond")).toHaveLength(0);
  expect(container.querySelector('[role="alertdialog"]')).not.toBeNull();
  await click("Confirm always allow");
  expect(requests("approval.respond")[0].params).toEqual({ session_id: "runtime", request_id: "a", choice: "always" });
  expect(requests("approval.pending")).toHaveLength(1);
});
it.each(["once", "session", "deny"])("sends the explicit %s approval", async choice => {
  await emit("approval.request", { request_id: "a", choices: [choice] });
  await click({ once: "Allow once", session: "Allow this session", deny: "Deny" }[choice]!);
  expect(requests("approval.respond")[0].params.choice).toBe(choice);
});
it("clarify multi-select encodes a JSON string and allows free text", async () => {
  await emit("clarify.request", { request_id: "q", question: "Pick", choices: ["a", "b", null, " "], multi_select: true });
  const inputs = container.querySelectorAll('input[type="checkbox"]'); expect(inputs).toHaveLength(2);
  await act(async () => { (inputs[0] as HTMLElement).click(); (inputs[1] as HTMLElement).click(); });
  await click("Answer"); expect(requests("clarify.respond")[0].params).toEqual({ request_id: "q", answer: '["a","b"]' });
  await emit("clarify.request", { request_id: "q2", question: "Free", choices: [null, {}] });
  await input("textarea", "typed answer"); await click("Answer");
  expect(requests("clarify.respond")[1].params.answer).toBe("typed answer");
});
it("clarify single choice and empty Skip use the existing answer contract", async () => {
  await emit("clarify.request", { request_id: "q", question: "Pick", choices: ["a"] });
  await act(async () => { (container.querySelector('input[type="radio"]') as HTMLElement).click(); });
  await click("Answer"); expect(requests("clarify.respond")[0].params.answer).toBe("a");
  await emit("clarify.request", { request_id: "q2", question: "Skip?" });
  await click("Skip"); expect(requests("clarify.respond")[1].params.answer).toBe("");
});
it.each(["secret", "sudo"])("%s is masked, never stored/projected/logged, and cleared on submit", async kind => {
  const sensitive = "test-SENSITIVE-7k2";
  const log = vi.spyOn(console, "log"); const warn = vi.spyOn(console, "warn"); const error = vi.spyOn(console, "error");
  await emit(`${kind}.request`, { request_id: "private", env_var: "API_KEY", prompt: "Enter key" });
  const field = container.querySelector('input[type="password"]') as HTMLInputElement;
  expect(field).not.toBeNull(); await input('input[type="password"]', sensitive);
  const assertAbsent = () => {
    expect(JSON.stringify(session.getSnapshot())).not.toContain(sensitive);
    expect(container.textContent).not.toContain(sensitive);
    expect(JSON.stringify(localStorage)).not.toContain(sensitive); expect(JSON.stringify(sessionStorage)).not.toContain(sensitive);
    expect(location.href).not.toContain(sensitive);
    expect(JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls])).not.toContain(sensitive);
  };
  assertAbsent(); await click("Submit securely"); assertAbsent(); expect(field.value).toBe("");
  expect(requests(`${kind}.respond`)[0].params).toEqual({ request_id: "private", [kind === "sudo" ? "password" : "value"]: sensitive });
});
it.each(["secret", "sudo"])("%s cancellation and expiry clear only that request", async kind => {
  await emit(`${kind}.request`, { request_id: "a" }); await input('input[type="password"]', "private"); await click("Cancel");
  expect(requests(`${kind}.respond`)[0].params[kind === "sudo" ? "password" : "value"]).toBe("");
  await emit(`${kind}.request`, { request_id: "b" }); await input('input[type="password"]', "private");
  const field = container.querySelector('input[type="password"]') as HTMLInputElement;
  await emit(`${kind}.expire`, { request_id: "a" }); expect(field.value).toBe("private");
  await emit(`${kind}.expire`, { request_id: "b" }); expect(field.value).toBe(""); expect(container.textContent).toContain("Request expired");
});
it("unmount clears masked fields without synthesizing a response", async () => {
  await emit("secret.request", { request_id: "a" }); await input('input[type="password"]', "private");
  const field = container.querySelector('input[type="password"]') as HTMLInputElement;
  await act(async () => root.render(null)); expect(field.value).toBe(""); expect(requests("secret.respond")).toHaveLength(0);
});
it("MCP credentials are transient and a background install waits for completion", async () => {
  let resolve!: (value: unknown) => void;
  api.installMcpCatalogEntry.mockResolvedValue({ ok: true, background: true, action: "install-test" });
  api.getActionStatus.mockImplementation(() => new Promise(r => { resolve = r; }));
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "install", reason: "Needed" });
  await input('input[type="password"]', "MCP-PRIVATE"); await click("Confirm setup");
  expect(api.installMcpCatalogEntry).toHaveBeenCalledWith("test", { KEY: "MCP-PRIVATE" }, true, "work");
  expect(JSON.stringify(session.getSnapshot())).not.toContain("MCP-PRIVATE");
  expect((container.querySelector('input[type="password"]') as HTMLInputElement).value).toBe("");
  expect(requests("mcp.setup.respond")).toHaveLength(0);
  await act(async () => { resolve({ running: false, exit_code: 0 }); await flushNative(); });
  expect(requests("reload.mcp")[0].params).toEqual({ confirm: true, session_id: "runtime" });
  expect(requests("mcp.setup.respond")[0].params).toEqual({ request_id: "m", result: JSON.stringify({ status: "installed", server: "test" }) });
});
it("MCP cancel wins against late installation success", async () => {
  let resolve!: (value: unknown) => void;
  api.installMcpCatalogEntry.mockImplementation(() => new Promise(r => { resolve = r; }));
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "install" });
  await input('input[type="password"]', "PRIVATE"); await click("Confirm setup"); await click("Cancel");
  await act(async () => { resolve({ ok: true, background: false }); await flushNative(); });
  expect(requests("mcp.setup.respond")).toHaveLength(1);
  expect(JSON.parse(requests("mcp.setup.respond")[0].params.result as string).status).toBe("declined");
  expect(requests("reload.mcp")).toHaveLength(0);
});
it("MCP enable remains successful when live reload fails", async () => {
  const responder = FakeNativeSocket.responder;
  FakeNativeSocket.responder = (r, s) => r.method === "reload.mcp" ? s.fail(r) : responder(r, s);
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "enable" }); await click("Confirm setup");
  expect(api.setMcpServerEnabled).toHaveBeenCalledWith("test", true, "work");
  expect(JSON.parse(requests("mcp.setup.respond")[0].params.result as string).status).toBe("enabled");
  expect(session.getSnapshot().control.notice).toContain("reload failed");
});
it("MCP OAuth popup opens before start and reports authorization", async () => {
  const popup = { location: { href: "" }, close: vi.fn(), closed: false, opener: null };
  const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
  api.authMcpServer.mockImplementation(async () => { expect(open).toHaveBeenCalledOnce(); return { flow_id: "flow", status: "authorization_required", authorization_url: "https://example.test/oauth" }; });
  api.getMcpOAuthFlow.mockResolvedValue({ status: "approved", tools: [{ name: "tool" }] });
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "authorize" }); await click("Confirm setup");
  expect(JSON.parse(requests("mcp.setup.respond")[0].params.result as string)).toEqual({ status: "authorized", server: "test", tools: ["tool"] });
});
it("blocked popup produces one safe error outcome", async () => {
  vi.spyOn(window, "open").mockReturnValue(null);
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "authorize" }); await click("Confirm setup");
  expect(api.authMcpServer).not.toHaveBeenCalled();
  expect(JSON.parse(requests("mcp.setup.respond")[0].params.result as string).status).toBe("error");
});
it.each(["secret", "sudo"])("%s transport error never renders the credential or raw error", async kind => {
  const sensitive = "P4-REJECTED-CREDENTIAL";
  const logs = vi.spyOn(console, "error");
  await emit(`${kind}.request`, { request_id: "reject" });
  FakeNativeSocket.responder = (r, s) => s.frame({ id: r.id, error: { code: 4009, message: sensitive } });
  await input('input[type="password"]', sensitive); await click("Submit securely");
  expect(container.textContent).toContain("Response not confirmed");
  expect(container.textContent).not.toContain(sensitive); expect(JSON.stringify(session.getSnapshot())).not.toContain(sensitive);
  expect(JSON.stringify(logs.mock.calls)).not.toContain(sensitive);
});
it.each(["missing-action", "failed-exit"])("MCP %s cannot report installation success", async failure => {
  api.installMcpCatalogEntry.mockResolvedValue({ ok: true, background: true, ...(failure === "failed-exit" ? { action: "install" } : {}) });
  api.getActionStatus.mockResolvedValue({ running: false, exit_code: 1, lines: ["PRIVATE-INSTALL-LOG"] });
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "install" });
  await input('input[type="password"]', "PRIVATE"); await click("Confirm setup");
  const outcome = JSON.parse(requests("mcp.setup.respond")[0].params.result as string);
  expect(outcome.status).toBe("error"); expect(JSON.stringify(outcome)).not.toContain("PRIVATE");
  expect(requests("reload.mcp")).toHaveLength(0);
});
it("MCP expiry prevents a late enable completion from sending success", async () => {
  let resolve!: (value: unknown) => void;
  api.setMcpServerEnabled.mockImplementation(() => new Promise(r => { resolve = r; }));
  await emit("mcp.setup.request", { request_id: "m", server: "test", action: "enable" }); await click("Confirm setup");
  await emit("mcp.setup.expire", { request_id: "m" });
  await act(async () => { resolve({ ok: true }); await flushNative(); });
  expect(requests("mcp.setup.respond")).toHaveLength(0); expect(requests("reload.mcp")).toHaveLength(0);
});

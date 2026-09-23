// @vitest-environment jsdom
import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigate } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NativeChatRuntime } from "./NativeChatRuntime";
import { NativeThread } from "./NativeThread";
import NativeChatPage from "./NativeChatPage";
import { NativeSession } from "./native-session";
import { FakeNativeSocket, flushNative } from "./fake-websocket.test-support";
import { ThreadPrimitive } from "@assistant-ui/react";

const profile = vi.hoisted(() => ({ profile: "" }));
vi.mock("@/contexts/useProfileScope", () => ({ useProfileScope: () => profile }));
vi.mock("@/lib/api", () => ({ HERMES_BASE_PATH: "", buildWsUrl: vi.fn(async () => "ws://localhost/api/ws?ticket=fresh") }));
vi.mock("@/lib/dashboard-auth-reload", () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let container: HTMLDivElement;
let root: Root;
function Harness() {
  const location = useLocation(); const navigate = useNavigate();
  return <><NativeChatPage isActive={location.pathname === "/chat"} /><button data-nav="away" onClick={() => navigate("/sessions")} /><button data-nav="back" onClick={() => navigate("/chat")} /><output>{location.search}</output></>;
}
beforeEach(() => {
  profile.profile = ""; FakeNativeSocket.reset();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("WebSocket", FakeNativeSocket);
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollTo = vi.fn();
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
async function render(path = "/chat?chat_mode=native") {
  await act(async () => { root.render(<StrictMode><MemoryRouter initialEntries={[path]}><Harness /></MemoryRouter></StrictMode>); await flushNative(); });
}
async function click(selector: string) { await act(async () => { (container.querySelector(selector) as HTMLElement).click(); await flushNative(); }); }
async function send(text: string) {
  await act(async () => {
    const textarea = container.querySelector("textarea")!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, text);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const button = [...container.querySelectorAll("button")].find(b => b.textContent === "Send")!;
  await act(async () => { button.click(); await flushNative(); });
}
it("renders a real shared runtime with text, separate reasoning, tools, and Stop", async () => {
  await render();
  expect(FakeNativeSocket.instances).toHaveLength(1);
  expect(FakeNativeSocket.instances[0].url).toContain("/api/ws");
  expect(container.querySelector(".xterm")).toBeNull();
  await send("hello");
  expect(container.textContent).toContain("hello");
  expect(container.textContent).toContain("Stop");
  await act(async () => {
    const socket = FakeNativeSocket.instances[0];
    socket.event("reasoning.delta", { text: "Considering the request" });
    socket.event("tool.start", { tool_id: "t", name: "terminal", context: "pwd" });
    socket.event("message.delta", { text: "Working directory" });
  });
  expect(container.querySelector("details")?.textContent).toContain("Considering the request");
  expect(container.querySelector("details")?.open).toBe(false);
  expect(container.textContent).toContain("terminal");
  expect(container.textContent).toContain("Running…");
  expect(container.textContent).toContain("Working directory");
  await act(async () => {
    FakeNativeSocket.instances[0].event("tool.complete", { tool_id: "t", name: "terminal", result: { success: true } });
    FakeNativeSocket.instances[0].event("message.complete", { text: "/workspace" });
  });
  expect(container.textContent).toContain("Completed");
  expect(container.textContent).toContain("/workspace");
  expect(container.querySelector("output")?.textContent).toContain("resume=stored");
});
it("preserves the socket/session while hidden and restores the explicit native URL", async () => {
  await render(); await send("hello");
  await click('[data-nav="away"]');
  await act(async () => { FakeNativeSocket.instances[0].event("message.complete", { text: "finished while hidden" }); });
  await click('[data-nav="back"]');
  expect(FakeNativeSocket.instances).toHaveLength(1);
  expect(FakeNativeSocket.requests.filter(r => r.method === "session.create")).toHaveLength(1);
  expect(FakeNativeSocket.requests.filter(r => r.method === "prompt.submit")).toHaveLength(1);
  expect(container.textContent).toContain("finished while hidden");
  expect(container.querySelector("output")?.textContent).toContain("chat_mode=native");
});
it("resumes on fresh mount using durable URL identity", async () => {
  await render("/chat?chat_mode=native&resume=saved");
  expect(FakeNativeSocket.requests[0]).toMatchObject({ method: "session.resume", params: { session_id: "saved" } });
  expect(container.textContent).toContain("restored");
});
it("keeps the shared runtime parent chain intact after filtering synthetic history", async () => {
  FakeNativeSocket.responder = (request, socket) => {
    if (request.method !== "session.resume") return FakeNativeSocket.defaultResponse(request, socket);
    socket.reply(request, { session_id: "runtime", session_key: "stored", running: false, messages: [
      { role: "user", text: "original user", row_id: 1 },
      ...["model_switch", "personality_switch", "auto_continue", "async_delegation_complete"].map((display_kind, index) => ({ role: "user", text: `[System: internal ${display_kind}]`, display_kind, row_id: index + 2 })),
      { role: "assistant", text: "original answer", row_id: 6 },
    ] });
  };
  await render("/chat?chat_mode=native&resume=saved");
  expect(container.textContent).not.toContain("internal");
  expect(container.querySelectorAll('[aria-label="Your message"]')).toHaveLength(1);
  await send("next user");
  await act(async () => {
    FakeNativeSocket.instances[0].event("message.delta", { text: "live" });
    FakeNativeSocket.instances[0].event("message.complete", { text: "next answer" });
  });
  expect([...container.querySelectorAll('[aria-label="Your message"], [aria-label="Hermes response"]')].map(el => el.textContent)).toEqual(["original user", "original answer", "next user", "next answer"]);
});
it("removes a definitively rejected optimistic turn and displays the rejection", async () => {
  await render();
  FakeNativeSocket.responder = (request, socket) => socket.fail(request);
  await send("rejected input");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("rejected");
  expect(container.querySelectorAll('[aria-label="Your message"], [aria-label="Hermes response"]')).toHaveLength(0);
  expect(container.querySelector("textarea")?.disabled).toBe(false);
  expect(FakeNativeSocket.requests.filter(r => r.method === "prompt.submit")).toHaveLength(1);
  expect(container.querySelector("output")?.textContent).not.toContain("resume=");
});
it("shows unsupported requests without responding and Stop uses the real interrupt RPC", async () => {
  await render(); await send("hello");
  await act(async () => FakeNativeSocket.instances[0].event("approval.request", { command: "secret details" }));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("not supported");
  expect(container.textContent).not.toContain("secret details");
  const stop = [...container.querySelectorAll("button")].find(b => b.textContent === "Stop")!;
  await act(async () => { stop.click(); await flushNative(); });
  expect(FakeNativeSocket.requests.some(r => r.method === "session.interrupt")).toBe(true);
  expect(FakeNativeSocket.requests.some(r => r.method.endsWith(".respond"))).toBe(false);
});
it("shows terminal error and reconnect affordance", async () => {
  await render(); await send("hello");
  await act(async () => FakeNativeSocket.instances[0].event("message.complete", { text: "partial", error: "Turn failed", status: "error" }));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Turn failed");
  expect(container.textContent).toContain("Reconnect");
  expect(container.textContent).not.toContain("Stop");
});
it("renders hydrated messages with the same runtime adapter", async () => {
  const session = new NativeSession("", "stored"); session.start(); await flushNative();
  await act(async () => root.render(<NativeChatRuntime session={session} state={session.getSnapshot()}><ThreadPrimitive.Root><NativeThread /></ThreadPrimitive.Root></NativeChatRuntime>));
  expect(container.textContent).toContain("restored"); session.stop();
});

it("isolates profile changes without resuming the previous profile's durable ID", async () => {
  await render("/chat?chat_mode=native&resume=profile-a-id");
  profile.profile = "work";
  await act(async () => { root.render(<StrictMode><MemoryRouter><Harness /></MemoryRouter></StrictMode>); await flushNative(); });
  expect(FakeNativeSocket.instances.filter(s => s.readyState === 1)).toHaveLength(1);
  expect(FakeNativeSocket.requests.at(-1)).toMatchObject({ method: "session.create", params: { profile: "work" } });
  expect(FakeNativeSocket.requests.filter(r => r.method === "session.resume")).toHaveLength(1);
  expect(container.querySelector("output")?.textContent).not.toContain("profile-a-id");
});

it("does not send the IME confirmation Enter (keyCode 229)", async () => {
  await render();
  await act(async () => {
    const textarea = container.querySelector("textarea")!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, "你好");
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 229, bubbles: true, cancelable: true }));
  });
  expect(FakeNativeSocket.requests.some(r => r.method === "prompt.submit")).toBe(false);
});

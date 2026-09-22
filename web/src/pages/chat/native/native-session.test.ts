import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NativeSession } from "./native-session";
import { FakeNativeSocket, flushNative } from "./fake-websocket.test-support";

const buildWsUrl = vi.hoisted(() => vi.fn(async () => "ws://localhost/api/ws?ticket=fresh"));
vi.mock("@/lib/api", () => ({ buildWsUrl }));
vi.mock("@/lib/dashboard-auth-reload", () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let session: NativeSession;
beforeEach(() => { vi.useFakeTimers(); FakeNativeSocket.reset(); vi.stubGlobal("WebSocket", FakeNativeSocket); buildWsUrl.mockClear(); session = new NativeSession("work", null); });
afterEach(() => { session.stop(); vi.useRealTimers(); vi.unstubAllGlobals(); });
async function start() { session.start(); await flushNative(); }
const requests = (method: string) => FakeNativeSocket.requests.filter(r => r.method === method);

describe("native session over shared JSON-RPC client", () => {
  it("creates a profile-scoped draft without claiming its ID is durable", async () => {
    await start();
    expect(requests("session.create")[0].params).toMatchObject({ profile: "work", source: "web" });
    expect(session.getSnapshot()).toMatchObject({ runtimeId: "runtime", storedId: "stored", durable: false, ready: true });
  });
  it("submits once, uses runtime identity, records durable acceptance and stops", async () => {
    await start();
    await Promise.all([session.submit("hello"), session.submit("second")]);
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(requests("prompt.submit")[0].params).toEqual({ session_id: "runtime", text: "hello" });
    expect(session.getSnapshot().durable).toBe(true);
    await session.interrupt();
    expect(requests("session.interrupt")[0].params).toEqual({ session_id: "runtime" });
    expect(session.getSnapshot().conversation.running).toBe(false);
  });
  it("resumes from URL durable identity and then continues streaming", async () => {
    session = new NativeSession("work", "saved-id");
    await start();
    expect(requests("session.resume")[0].params).toEqual({ session_id: "saved-id", profile: "work" });
    expect(requests("session.create")).toHaveLength(0);
    await session.submit("next");
    FakeNativeSocket.instances[0].event("message.delta", { text: "next reply" });
    FakeNativeSocket.instances[0].event("message.complete", { text: "next reply" });
    expect(session.getSnapshot().conversation.messages).toHaveLength(4);
  });
  it("reconnects with fresh auth and resumes, never replaying an accepted prompt", async () => {
    await start(); await session.submit("hello");
    FakeNativeSocket.instances[0].close(1006);
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(requests("session.resume")[0].params.session_id).toBe("stored");
    expect(buildWsUrl).toHaveBeenCalledTimes(2);
    expect(FakeNativeSocket.instances.filter(s => s.readyState === 1)).toHaveLength(1);
    expect(session.getSnapshot().conversation.messages.at(-1)?.parts[0].text).toBe("restored");
  });
  it("reattaches an empty draft by runtime ID rather than resuming a nonexistent DB row", async () => {
    await start(); FakeNativeSocket.instances[0].close(1006);
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(requests("session.activate")[0].params.session_id).toBe("runtime");
    expect(requests("session.create")).toHaveLength(1);
    expect(requests("session.resume")).toHaveLength(0);
  });
  it("recovers an unacknowledged prompt without resending", async () => {
    await start();
    FakeNativeSocket.responder = (request, socket) => request.method === "prompt.submit" ? socket.close(1006) : FakeNativeSocket.defaultResponse(request, socket);
    await session.submit("hello");
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(requests("session.resume")).toHaveLength(1);
  });
  it.each(["session.create", "session.resume"])("surfaces %s failure without silently creating another session", async method => {
    if (method === "session.resume") session = new NativeSession("work", "missing");
    FakeNativeSocket.responder = (request, socket) => socket.fail(request);
    await start();
    expect(session.getSnapshot()).toMatchObject({ ready: false, connection: "error" });
    expect(session.getSnapshot().conversation.error).toBeTruthy();
    expect(FakeNativeSocket.requests.map(r => r.method)).toEqual([method]);
  });
  it("shows definitive submission rejection and lets the user recover the draft", async () => {
    await start();
    FakeNativeSocket.responder = (request, socket) => socket.fail(request);
    await session.submit("hello");
    expect(session.getSnapshot().ready).toBe(true);
    expect(session.getSnapshot().conversation.running).toBe(false);
    expect(requests("prompt.submit")).toHaveLength(1);
  });
  it("detects unsupported requests and ignores another runtime", async () => {
    await start(); await session.submit("hello");
    const socket = FakeNativeSocket.instances[0];
    socket.event("message.delta", { text: "wrong" }, "other");
    socket.event("secret.request", { prompt: "private" });
    expect(session.getSnapshot().conversation.blocked).toBe("secret.request");
    expect(JSON.stringify(session.getSnapshot())).not.toContain("private");
    await session.submit("second");
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(FakeNativeSocket.requests.some(r => r.method.endsWith(".respond"))).toBe(false);
  });
  it("does not reconnect authentication rejection automatically", async () => {
    await start(); FakeNativeSocket.instances[0].close(4403);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeNativeSocket.instances).toHaveLength(1);
    expect(session.getSnapshot().conversation.error).toContain("denied");
  });
  it("times out pending ticket acquisition and does not open a late socket after cleanup", async () => {
    let resolve!: (url: string) => void;
    buildWsUrl.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
    await start(); await vi.advanceTimersByTimeAsync(20_001);
    expect(session.getSnapshot().connection).toBe("error");
    session.stop(); resolve("ws://localhost/api/ws?ticket=late"); await flushNative();
    expect(FakeNativeSocket.instances).toHaveLength(0);
  });
  it("cleans up delayed reconnects on disposal", async () => {
    await start(); FakeNativeSocket.instances[0].close(1006); session.stop();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeNativeSocket.instances).toHaveLength(1);
  });
});

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
    expect(requests("session.create")[0].params).toMatchObject({ profile: "work", source: "webui" });
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
    expect(requests("session.activate")).toHaveLength(0);
    expect(session.getSnapshot()).toMatchObject({ ready: true, durable: true, connection: "open" });
  });
  it.each([false, true])("inspects an uncertain first draft after stored resume 4007 (running=%s)", async running => {
    await start();
    FakeNativeSocket.responder = (request, socket) => {
      if (request.method === "prompt.submit") return socket.close(1006);
      if (request.method === "session.resume") return socket.fail(request); // no persisted row
      if (request.method === "session.activate") return socket.reply(request, {
        session_id: "runtime", session_key: "stored", running,
        messages: running ? [{ role: "user", text: "uncertain first prompt" }] : [],
        ...(running ? { inflight: { user: "uncertain first prompt", assistant: "In progress", streaming: true } } : {}),
      });
      FakeNativeSocket.defaultResponse(request, socket);
    };
    await session.submit("uncertain first prompt");
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(requests("session.resume")).toHaveLength(1);
    expect(requests("session.resume")[0].params).toEqual({ session_id: "stored", profile: "work" });
    expect(requests("session.activate")).toHaveLength(1);
    expect(requests("session.activate")[0].params).toEqual({ session_id: "runtime" });
    expect(requests("session.create")).toHaveLength(1);
    const recovered = session.getSnapshot();
    expect(recovered).toMatchObject({ ready: true, connection: "open", durable: false, runtimeId: "runtime", storedId: "stored" });
    expect(recovered.conversation).toMatchObject({ running, error: null });
    if (running) {
      expect(recovered.conversation.messages.map(m => [m.role, m.parts[0]?.text])).toEqual([["user", "uncertain first prompt"], ["assistant", "In progress"]]);
      FakeNativeSocket.instances.at(-1)!.event("message.complete", { text: "Completed after recovery" });
      expect(session.getSnapshot().conversation.running).toBe(false);
    } else expect(recovered.conversation.messages).toEqual([]);
    // A later disconnect reattaches by runtime ID: uncertainty was cleared,
    // so the failed stored resume is not retried forever.
    FakeNativeSocket.instances.at(-1)!.close(1006);
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(requests("session.resume")).toHaveLength(1);
    expect(requests("prompt.submit")).toHaveLength(1);
    if (!running) {
      FakeNativeSocket.responder = FakeNativeSocket.defaultResponse;
      await session.submit("user explicitly sends a new prompt");
      expect(requests("prompt.submit").map(r => r.params.text)).toEqual(["uncertain first prompt", "user explicitly sends a new prompt"]);
      expect(session.getSnapshot().durable).toBe(true);
    }
  });
  it("does not activate a known runtime when its durable stored session is missing", async () => {
    await start(); await session.submit("accepted");
    FakeNativeSocket.responder = (request, socket) => socket.fail(request);
    FakeNativeSocket.instances[0].close(1006);
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(session.getSnapshot()).toMatchObject({ durable: true, ready: false, connection: "error", runtimeId: "runtime" });
    expect(session.getSnapshot().conversation.error).toContain("restore");
    expect(requests("session.resume")).toHaveLength(1);
    expect(requests("session.activate")).toHaveLength(0);
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(requests("session.create")).toHaveLength(1);
  });
  it("surfaces a failed draft activation without creating a replacement or replaying", async () => {
    await start();
    FakeNativeSocket.responder = (request, socket) => request.method === "prompt.submit" ? socket.close(1006) : socket.fail(request);
    await session.submit("unknown");
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(session.getSnapshot()).toMatchObject({ ready: false, durable: false, connection: "error" });
    expect(session.getSnapshot().conversation.error).toContain("restore");
    expect(requests("session.activate")).toHaveLength(1);
    expect(requests("session.create")).toHaveLength(1);
    expect(requests("prompt.submit")).toHaveLength(1);
  });
  it("does not activate a draft after a different resume RPC error", async () => {
    await start();
    FakeNativeSocket.responder = (request, socket) => request.method === "prompt.submit" ? socket.close(1006) : socket.frame({ id: request.id, error: { code: 4030, message: "denied" } });
    await session.submit("unknown");
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(session.getSnapshot()).toMatchObject({ ready: false, connection: "error" });
    expect(requests("session.activate")).toHaveLength(0);
    expect(requests("prompt.submit")).toHaveLength(1);
  });
  it.each(["resume", "activate"])("ignores a replaced connection while uncertain recovery waits for %s", async phase => {
    await start();
    FakeNativeSocket.responder = (request, socket) => {
      if (request.method === "prompt.submit") return socket.close(1006);
      if (request.method === "session.resume" && phase === "activate") return socket.fail(request);
      // Leave the selected recovery RPC pending until its controller is replaced.
    };
    await session.submit("old unknown prompt");
    await vi.advanceTimersByTimeAsync(1000); await flushNative();
    const oldSocket = FakeNativeSocket.instances.at(-1)!;
    const oldRequest = FakeNativeSocket.requests.at(-1)!;
    expect(oldRequest.method).toBe(`session.${phase}`);
    FakeNativeSocket.responder = FakeNativeSocket.defaultResponse;
    session.select(null); await flushNative();
    const replacement = session.getSnapshot();
    oldSocket.frame({ id: oldRequest.id, ...(phase === "resume" ? { error: { code: 4007, message: "session not found" } } : { result: { session_id: "old", running: true, messages: [{ role: "user", text: "stale" }] } }) });
    await flushNative();
    expect(session.getSnapshot()).toBe(replacement);
    expect(requests("session.activate")).toHaveLength(phase === "activate" ? 1 : 0);
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(replacement.conversation.messages).toEqual([]);
    expect(FakeNativeSocket.instances.filter(s => s.readyState === 1)).toHaveLength(1);
  });
  it.each(["session.create", "session.resume"])("surfaces %s failure without silently creating another session", async method => {
    if (method === "session.resume") session = new NativeSession("work", "missing");
    FakeNativeSocket.responder = (request, socket) => socket.fail(request);
    await start();
    expect(session.getSnapshot()).toMatchObject({ ready: false, connection: "error" });
    expect(session.getSnapshot().conversation.error).toBeTruthy();
    expect(FakeNativeSocket.requests.map(r => r.method)).toEqual([method]);
  });
  it.each([null, "saved-id"])("rolls back definitive submission rejection without changing prior messages (resume=%s)", async resume => {
    session = new NativeSession("work", resume);
    await start();
    const before = session.getSnapshot();
    FakeNativeSocket.responder = (request, socket) => socket.fail(request);
    await session.submit("rejected prompt");
    expect(session.getSnapshot().ready).toBe(true);
    expect(session.getSnapshot().conversation.running).toBe(false);
    expect(session.getSnapshot().conversation.messages).toBe(before.conversation.messages);
    expect(JSON.stringify(session.getSnapshot().conversation.messages)).not.toContain("rejected prompt");
    expect(session.getSnapshot().conversation.error).toContain("rejected");
    expect(session.getSnapshot().durable).toBe(before.durable);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(requests("prompt.submit")).toHaveLength(1);
  });
  it("isolates structured requests from another runtime", async () => {
    await start(); await session.submit("hello");
    const socket = FakeNativeSocket.instances[0];
    socket.event("message.delta", { text: "wrong" }, "other");
    socket.event("secret.request", { request_id: "secret-1", prompt: "Enter key" });
    expect(session.getSnapshot().interactions["secret:secret-1"]).toMatchObject({ kind: "secret", requestId: "secret-1" });
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

describe("native interaction and control recovery", () => {
  it.each(["approval", "clarify", "secret", "sudo", "mcp.setup"])("recovers %s metadata without duplicating responses", async kind => {
    FakeNativeSocket.responder = (r, s) => {
      if (r.method === "session.activate") return s.reply(r, { session_id: "runtime", session_key: "stored", running: true, pending_interactions: kind === "approval" ? [] : [{ type: `${kind}.request`, payload: { request_id: "r", question: "Q", prompt: "Enter", server: "test", action: "install" } }], ...(kind === "approval" ? { pending_approval: { request_id: "r" } } : {}) });
      FakeNativeSocket.defaultResponse(r, s);
    };
    await start(); FakeNativeSocket.instances[0].event(`${kind}.request`, { request_id: "r" });
    FakeNativeSocket.instances[0].close(1006); await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(Object.values(session.getSnapshot().interactions)).toHaveLength(1);
    expect(session.getSnapshot().interactions[`${kind}:r`]).toMatchObject({ phase: "pending", runtimeId: "runtime" });
    expect(FakeNativeSocket.requests.filter(r => r.method.endsWith(".respond"))).toHaveLength(0);
  });
  it.each(["secret", "sudo", "clarify", "mcp.setup"])("settles late %s responses as expired", async kind => {
    await start(); FakeNativeSocket.instances[0].event(`${kind}.request`, { request_id: "a" });
    const r = session.getSnapshot().interactions[`${kind}:a`];
    FakeNativeSocket.responder = (req, s) => s.reply(req, { status: "expired" });
    if (kind === "secret") await session.respondSecret(r, "private");
    if (kind === "sudo") await session.respondSudo(r, "private");
    if (kind === "clarify") await session.respondClarify(r, "answer");
    if (kind === "mcp.setup") await session.respondMcpSetup(r, { status: "declined", server: "test" });
    expect(session.getSnapshot().interactions[r.key].phase).toBe("expired");
    expect(JSON.stringify(session.getSnapshot())).not.toContain("private");
  });
  it("deduplicates response clicks and prevents an old response/expiry/error clearing a new request", async () => {
    await start(); const socket = FakeNativeSocket.instances[0]; socket.event("secret.request", { request_id: "a" });
    const old = session.getSnapshot().interactions["secret:a"];
    FakeNativeSocket.responder = () => undefined;
    const sending = session.respondSecret(old, "private"); await session.respondSecret(old, "private"); await flushNative();
    expect(requests("secret.respond")).toHaveLength(1);
    socket.event("secret.request", { request_id: "b" }); socket.event("secret.expire", { request_id: "a" });
    socket.fail(requests("secret.respond")[0]); await sending;
    expect(session.getSnapshot().interactions["secret:b"].phase).toBe("pending");
    expect(session.getSnapshot().interactions["secret:a"].phase).toBe("expired");
  });
  it("an old-generation response cannot resolve recovered input", async () => {
    await start(); const socket = FakeNativeSocket.instances[0]; socket.event("sudo.request", { request_id: "a" });
    const r = session.getSnapshot().interactions["sudo:a"];
    FakeNativeSocket.responder = (req, s) => req.method === "sudo.respond" ? undefined : s.reply(req, { session_id: "runtime", pending_interactions: [{ type: "sudo.request", payload: { request_id: "a" } }] });
    const promise = session.respondSudo(r, "private"); await flushNative();
    session.retry(); await flushNative(); await promise;
    const current = session.getSnapshot();
    socket.reply(requests("sudo.respond")[0], { status: "ok" }); socket.event("sudo.expire", { request_id: "a" });
    expect(session.getSnapshot()).toBe(current);
    expect(current.interactions["sudo:a"].phase).toBe("pending");
    expect(requests("sudo.respond")).toHaveLength(1);
  });
  it("snapshot in flight cannot discard a newly arrived request", async () => {
    await start(); await session.submit("start");
    FakeNativeSocket.responder = () => undefined;
    FakeNativeSocket.instances[0].event("message.complete", { text: "done" }); await flushNative();
    const refresh = requests("session.activate")[0];
    FakeNativeSocket.instances[0].event("secret.request", { request_id: "new" });
    FakeNativeSocket.instances[0].reply(refresh, { session_id: "runtime", running: false, pending_interactions: [] }); await flushNative();
    expect(session.getSnapshot().interactions["secret:new"].phase).toBe("pending");
  });
  it.each(["queued", "steered", "redirected"])("busy submit respects authoritative %s without a fresh local turn", async status => {
    await start(); await session.submit("first"); const before = session.getSnapshot().conversation.messages;
    FakeNativeSocket.responder = (r, s) => r.method === "prompt.submit" ? s.reply(r, { status }) : s.reply(r, { session_id: "runtime", running: true, ...(status === "queued" ? { queued: { user: "second" } } : {}) });
    await session.submit("second"); await flushNative();
    expect(session.getSnapshot().conversation.messages).toBe(before);
    expect(session.getSnapshot().control.queued).toBe(status === "queued" ? "second" : null);
    expect(requests("prompt.submit")).toHaveLength(2);
  });
  it("explicit queue uses backend queued:true and Stop never drains it", async () => {
    await start(); await session.submit("first");
    FakeNativeSocket.responder = (r, s) => r.method === "prompt.submit" ? s.reply(r, { status: "queued" }) : r.method === "session.activate" ? s.reply(r, { session_id: "runtime", running: false, pending_interactions: [] }) : FakeNativeSocket.defaultResponse(r, s);
    await session.submit("next", true); await session.interrupt();
    expect(requests("prompt.submit")[1].params).toEqual({ session_id: "runtime", text: "next", queued: true });
    await vi.advanceTimersByTimeAsync(5000); expect(requests("prompt.submit")).toHaveLength(2);
    expect(session.getSnapshot().control.queued).toBeNull();
  });
  it.each(["queued", "rejected"])("explicit steer %s never falls back to submit", async status => {
    await start(); await session.submit("first"); FakeNativeSocket.responder = (r, s) => s.reply(r, { status });
    await session.steer("correction");
    expect(requests("session.steer")[0].params).toEqual({ session_id: "runtime", text: "correction" });
    expect(requests("prompt.submit")).toHaveLength(1);
    expect(session.getSnapshot().control.notice).toContain(status === "queued" ? "queued" : "rejected");
  });
  it("ambiguous busy queue submission reconnects without replay and restores backend queue", async () => {
    await start(); await session.submit("first");
    FakeNativeSocket.responder = (r, s) => r.method === "prompt.submit" ? s.close(1006) : s.reply(r, { session_id: "runtime", running: true, queued: { user: "later" }, pending_interactions: [] });
    await session.submit("later", true); await vi.advanceTimersByTimeAsync(1000); await flushNative();
    expect(requests("prompt.submit")).toHaveLength(2); expect(session.getSnapshot().control.queued).toBe("later");
  });
});

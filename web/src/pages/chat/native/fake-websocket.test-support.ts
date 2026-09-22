export interface RpcRequest { id: string; method: string; params: Record<string, unknown> }

/** Exercise the real shared JSON-RPC parser and request bookkeeping. */
export class FakeNativeSocket extends EventTarget {
  static OPEN = 1;
  static instances: FakeNativeSocket[] = [];
  static requests: RpcRequest[] = [];
  static responder: (request: RpcRequest, socket: FakeNativeSocket) => void = FakeNativeSocket.defaultResponse;
  static defaultResponse(request: RpcRequest, socket: FakeNativeSocket) {
    const result = request.method === "session.create" ? { session_id: "runtime", stored_session_id: "stored", messages: [] } : request.method === "session.resume" || request.method === "session.activate" ? { session_id: "runtime", session_key: "stored", messages: [{ role: "user", text: "hello" }, { role: "assistant", text: "restored" }], running: false } : { status: request.method === "session.interrupt" ? "interrupted" : "streaming" };
    socket.reply(request, result);
  }
  static reset() { this.instances = []; this.requests = []; this.responder = this.defaultResponse; }
  readyState = 0;
  readonly url: string;
  constructor(url: string) {
    super(); this.url = url; FakeNativeSocket.instances.push(this);
    queueMicrotask(() => {
      if (this.readyState === 3) return;
      this.readyState = 1;
      this.dispatchEvent(new Event("open"));
      this.event("gateway.ready", {}, "");
    });
  }
  send(data: string) {
    const request = JSON.parse(data) as RpcRequest;
    FakeNativeSocket.requests.push(request);
    queueMicrotask(() => FakeNativeSocket.responder(request, this));
  }
  reply(request: RpcRequest, result: unknown) { this.frame({ id: request.id, result }); }
  fail(request: RpcRequest) { this.frame({ id: request.id, error: { code: 4007, message: "not found" } }); }
  frame(frame: unknown) { if (this.readyState === 1) this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(frame) })); }
  event(type: string, payload: unknown = {}, session_id = "runtime") { this.frame({ method: "event", params: { type, payload, session_id } }); }
  close(code = 1000) {
    if (this.readyState === 3) return;
    this.readyState = 3;
    const event = new Event("close");
    Object.defineProperty(event, "code", { value: code });
    this.dispatchEvent(event);
  }
}

export async function flushNative() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

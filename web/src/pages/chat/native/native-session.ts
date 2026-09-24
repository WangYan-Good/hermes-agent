import { JsonRpcGatewayError, type GatewayEvent } from "@hermes/shared";
import { beginPrompt, emptyConversation, failConversation, record, reduceNativeEvent, string } from "./native-events";
import { bounded, NativeGateway } from "./native-gateway";
import { reconcileNativeResume } from "./native-messages";
import type { NativeSessionResponse, NativeSessionState } from "./native-types";

const initial = (): NativeSessionState => ({ runtimeId: null, storedId: null, durable: false, connection: "closed", ready: false, conversation: emptyConversation() });

export class NativeSession {
  private state = initial();
  private listeners = new Set<() => void>();
  private gateway: NativeGateway | null = null;
  private generation = 0;
  private stopped = true;
  private retries = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private target: string | null;
  private hydrating = false;
  private buffered: GatewayEvent[] = [];
  private uncertainSubmit = false;
  readonly profile: string;
  private readonly makeGateway: () => NativeGateway;

  constructor(profile: string, resume: string | null, makeGateway = () => new NativeGateway()) {
    this.profile = profile;
    this.target = resume;
    this.makeGateway = makeGateway;
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(next: NativeSessionState) { this.state = next; this.listeners.forEach(fn => fn()); }
  start = () => { if (!this.stopped) return; this.stopped = false; void this.connect(); };
  stop = () => {
    this.stopped = true; this.generation++; clearTimeout(this.timer);
    this.gateway?.close(); this.gateway = null;
  };
  retry = () => { if (this.stopped) return; this.retries = 0; void this.connect(); };

  select = (storedId: string | null) => {
    if (storedId && (storedId === this.state.storedId || storedId === this.target)) return;
    this.target = storedId; this.uncertainSubmit = false;
    this.set(initial()); this.retries = 0;
    if (!this.stopped) void this.connect();
  };

  private async connect() {
    const generation = ++this.generation;
    clearTimeout(this.timer);
    this.gateway?.close();
    const gateway = this.makeGateway();
    this.gateway = gateway;
    const current = () => !this.stopped && this.generation === generation;
    this.hydrating = true; this.buffered = [];
    this.set({ ...this.state, ready: false, connection: "connecting" });
    let resolveReady!: () => void;
    const ready = new Promise<void>(resolve => { resolveReady = resolve; });
    gateway.onAny(event => {
      if (!current()) return;
      if (event.type === "gateway.ready") { resolveReady(); return; }
      if (this.hydrating) { this.buffered.push(event); return; }
      this.event(event);
    });
    gateway.onState(connection => {
      if (current() && connection === "closed") this.disconnected(generation, gateway.authFailed);
    });
    try {
      // Observe ready before opening: servers may send it during the handshake.
      await bounded(Promise.all([gateway.open(), ready]), 20_000, "Gateway connection timed out. Retry to reconnect.");
      if (!current()) return;
      const storedId = this.state.storedId || this.target;
      let response: NativeSessionResponse;
      let resumed = false;
      if (storedId && (this.state.durable || this.target || this.uncertainSubmit)) {
        try {
          response = await gateway.request("session.resume", { session_id: storedId, profile: this.profile });
          resumed = true;
        } catch (error) {
          if (!current()) return;
          // The first prompt may never have reached acceptance: its allocated
          // stored ID then has no DB row, while the live draft can still exist.
          // Only inspect that uncertain draft; never mask a lost durable row.
          if (!this.uncertainSubmit || this.state.durable || !this.state.runtimeId ||
              !(error instanceof JsonRpcGatewayError) || error.code !== 4007) throw error;
          response = await gateway.request("session.activate", { session_id: this.state.runtimeId });
        }
      } else if (this.state.runtimeId) {
        // Empty drafts have no DB row. Reattach their live runtime; never hide
        // an expired draft behind an automatic replacement session.
        response = await gateway.request("session.activate", { session_id: this.state.runtimeId });
      } else {
        response = await gateway.request("session.create", { profile: this.profile, source: "webui", close_on_disconnect: false });
      }
      if (!current()) return;
      const conversation = reconcileNativeResume(response, this.buffered, this.state.conversation);
      this.hydrating = false; this.buffered = []; this.retries = 0; this.uncertainSubmit = false;
      this.set({ runtimeId: response.session_id, storedId: response.stored_session_id || response.session_key || response.info?.stored_session_id || storedId, durable: resumed || this.state.durable, ready: true, connection: "open", conversation });
    } catch {
      if (!current()) return;
      // Never surface raw transport/auth URLs or credential-bearing errors.
      const message = gateway.authFailed ? "Connection denied. Check dashboard authentication and retry." : "Could not connect or restore this session. Retry; your prompt will not be sent again.";
      this.generation++; gateway.close(); this.hydrating = false;
      this.set({ ...this.state, ready: false, connection: "error", conversation: failConversation(this.state.conversation, message) });
    }
  }

  private disconnected(generation: number, authFailed: boolean) {
    if (this.generation !== generation || this.stopped) return;
    this.generation++;
    this.gateway?.close();
    this.set({ ...this.state, ready: false, connection: "closed", conversation: failConversation(this.state.conversation, authFailed ? "Connection denied. Check authentication and retry." : "Connection lost. Recovering the session without resending your prompt…") });
    if (!authFailed && this.retries < 3) this.timer = setTimeout(() => void this.connect(), 1000 * 2 ** this.retries++);
  }

  private event(event: GatewayEvent) {
    if (event.session_id && event.session_id !== this.state.runtimeId) return;
    // Only connection-wide errors may be unscoped on this cross-surface path.
    if (!event.session_id && event.type !== "error") return;
    const payload = record(event.payload);
    const storedId = event.type === "session.info" ? string(payload.stored_session_id) : "";
    this.set({ ...this.state, storedId: storedId || this.state.storedId, conversation: reduceNativeEvent(this.state.conversation, event) });
  }

  submit = async (text: string) => {
    if (!text.trim() || !this.state.ready || this.state.conversation.running || this.state.conversation.blocked || !this.gateway || !this.state.runtimeId) return;
    const generation = this.generation;
    const beforeSubmit = this.state.conversation;
    this.uncertainSubmit = true;
    this.set({ ...this.state, conversation: beginPrompt(beforeSubmit, text) });
    try {
      await this.gateway.request("prompt.submit", { session_id: this.state.runtimeId, text });
      if (generation !== this.generation || this.stopped) return;
      this.uncertainSubmit = false;
      this.set({ ...this.state, durable: true });
    } catch (error) {
      if (generation !== this.generation || this.stopped) return;
      if (error instanceof JsonRpcGatewayError) {
        this.uncertainSubmit = false;
        // A definitive RPC rejection never became a conversation turn. Keep
        // prior messages intact; only ambiguous transport failures need resume.
        this.set({ ...this.state, ready: true, conversation: { ...beforeSubmit, running: false, status: "", blocked: null, error: "The gateway rejected this prompt. Correct the problem and send again, or start a new session." } });
        return;
      }
      // Acceptance is ambiguous on timeout. Disable sends until authoritative
      // resume; the controller never retries a non-idempotent prompt.
      this.set({ ...this.state, ready: false, conversation: failConversation(this.state.conversation, "Prompt submission failed or was not acknowledged. Reconnect to check its status before sending again.") });
    }
  };

  interrupt = async () => {
    if (!this.gateway || !this.state.runtimeId || !this.state.ready) return;
    const generation = this.generation;
    try {
      await this.gateway.request("session.interrupt", { session_id: this.state.runtimeId });
      if (generation !== this.generation) return;
      // Re-query the authoritative running flag after cooperative interrupt.
      const response = await this.gateway.request<NativeSessionResponse>("session.activate", { session_id: this.state.runtimeId, omit_messages: true });
      if (generation !== this.generation) return;
      if (!response.running) this.event({ type: "session.info", session_id: this.state.runtimeId!, payload: { running: false } });
      else this.set({ ...this.state, conversation: { ...this.state.conversation, blocked: null, status: "Stopping…" } });
    } catch {
      if (generation !== this.generation) return;
      this.set({ ...this.state, conversation: { ...this.state.conversation, error: "Could not stop this turn. Retry Stop or reconnect." } });
    }
  };
}

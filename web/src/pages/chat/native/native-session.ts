import { activeInteraction, hasInteraction, recoverInteractions, reduceInteractions, readInteraction, readMcpOperation, type NativeInteraction, type ApprovalChoice } from "./native-interactions";
import { emptyControl, recoverControl, reduceControl } from "./native-control";
import { JsonRpcGatewayError, type GatewayEvent } from "@hermes/shared";
import { beginPrompt, emptyConversation, failConversation, record, reduceNativeEvent, string } from "./native-events";
import { bounded, NativeGateway } from "./native-gateway";
import { reconcileNativeResume } from "./native-messages";
import type { NativeSessionResponse, NativeSessionState } from "./native-types";

const initial = (): NativeSessionState => ({ runtimeId: null, storedId: null, durable: false, connection: "closed", ready: false, conversation: emptyConversation(), interactions: {}, control: emptyControl() });

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
  private refreshVersion = 0;
  private eventRevision = 0;
  private acknowledged = new Set<string>();
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
    this.set({ ...this.state, ready: false, connection: "closed" });
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
    this.hydrating = true; this.buffered = []; this.acknowledged.clear();
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
      let interactions = recoverInteractions(response, generation);
      let control = recoverControl(response);
      for (const e of this.buffered.filter(e => e.session_id === response.session_id)) {
        interactions = reduceInteractions(interactions, e, generation);
        control = reduceControl(control, e);
      }
      this.hydrating = false; this.buffered = []; this.retries = 0; this.uncertainSubmit = false;
      this.set({ runtimeId: response.session_id, storedId: response.stored_session_id || response.session_key || response.info?.stored_session_id || storedId, durable: resumed || this.state.durable, ready: true, connection: "open", conversation, interactions, control });
      this.ackApprovals();
    } catch {
      if (!current()) return;
      // Never surface raw transport/auth URLs or credential-bearing errors.
      const message = gateway.authFailed ? "Connection denied. Check dashboard authentication and retry." : "Could not connect or restore this session. Retry; your prompt will not be sent again.";
      this.generation++; gateway.close(); this.hydrating = false;
      this.set({ ...this.state, ready: false, connection: "error", conversation: { ...this.state.conversation, error: message } });
    }
  }

  private disconnected(generation: number, authFailed: boolean) {
    if (this.generation !== generation || this.stopped) return;
    this.generation++;
    this.gateway?.close();
    this.set({ ...this.state, ready: false, connection: "closed", conversation: { ...this.state.conversation, error: authFailed ? "Connection denied. Check authentication and retry." : "Connection lost. Recovering the session without resending your prompt…" } });
    if (!authFailed && this.retries < 3) this.timer = setTimeout(() => void this.connect(), 1000 * 2 ** this.retries++);
  }

  private event(event: GatewayEvent) {
    if (event.session_id && event.session_id !== this.state.runtimeId) return;
    // Only connection-wide errors may be unscoped on this cross-surface path.
    if (!event.session_id && event.type !== "error") return;
    this.eventRevision++;
    const payload = record(event.payload);
    const storedId = event.type === "session.info" ? string(payload.stored_session_id) : "";
    this.set({ ...this.state, storedId: storedId || this.state.storedId, conversation: reduceNativeEvent(this.state.conversation, event), interactions: reduceInteractions(this.state.interactions, event, this.generation), control: reduceControl(this.state.control, event) });
    if (hasInteraction(this.state.interactions) && !this.state.conversation.running) this.set({ ...this.state, conversation: { ...this.state.conversation, running: true } });
    this.ackApprovals();
    if (event.type === "message.complete") void this.refresh().catch(() => { /* next reconnect restores metadata */ });
  }

  private ackApprovals() {
    for (const r of Object.values(this.state.interactions)) {
      if (r.kind !== "approval" || !r.requestId || !activeInteraction(r) || this.acknowledged.has(r.key)) continue;
      this.acknowledged.add(r.key);
      void this.gateway?.request("approval.received", { session_id: r.runtimeId, request_id: r.requestId }).catch(() => { /* replay/query owns recovery, never approval */ });
    }
  }

  /** Refresh control metadata without replacing a streaming transcript. */
  private async refresh() {
    if (!this.gateway || !this.state.runtimeId || !this.state.ready) return;
    const generation = this.generation;
    const version = ++this.refreshVersion;
    const runtimeId = this.state.runtimeId;
    const before = this.state.interactions;
    const revision = this.eventRevision;
    const response = await this.gateway.request<NativeSessionResponse>("session.activate", { session_id: runtimeId, omit_messages: true });
    if (generation !== this.generation || version !== this.refreshVersion || this.stopped || runtimeId !== this.state.runtimeId) return;
    // Events may have arrived while the snapshot was in flight. Preserve
    // settled tombstones, and do not overwrite newer local submissions.
    const recovered = recoverInteractions(response, generation);
    for (const [key, r] of Object.entries(this.state.interactions)) {
      if (!activeInteraction(r) || r.phase === "submitting" || before[key] !== r) recovered[key] = r;
    }
    this.set({ ...this.state, interactions: recovered, control: { ...this.state.control, queued: string(response.queued?.user) || null }, conversation: revision === this.eventRevision && response.running === false ? reduceNativeEvent(this.state.conversation, { type: "session.info", payload: { running: false } }) : this.state.conversation });
    this.ackApprovals();
  }

  isCurrent = (r: NativeInteraction) => !this.stopped && this.state.ready && r.generation === this.generation && r.runtimeId === this.state.runtimeId && this.state.interactions[r.key]?.generation === r.generation && activeInteraction(this.state.interactions[r.key]);
  rememberMcpOperation = (r: NativeInteraction, value: unknown) => {
    const operation = readMcpOperation(value);
    const current = this.state.interactions[r.key];
    if (!operation || !this.isCurrent(r) || current.kind !== "mcp.setup" || operation.kind !== current.action || (current.operation && current.operation.id !== operation.id)) return;
    if (current.operation && current.operation.state !== "starting" && operation.state === "starting") return;
    this.set({ ...this.state, interactions: { ...this.state.interactions, [r.key]: { ...current, operation } } });
  };
  private phase(r: NativeInteraction, phase: NativeInteraction["phase"], error?: string) {
    if (!this.isCurrent(r)) return;
    const current = this.state.interactions[r.key];
    this.set({ ...this.state, interactions: { ...this.state.interactions, [r.key]: { ...current, phase, error } } });
  }
  private async respond(r: NativeInteraction, params: Record<string, unknown>) {
    if (!this.gateway || !this.isCurrent(r) || this.state.interactions[r.key].phase !== "pending") return;
    this.phase(r, "submitting");
    try {
      // Sensitive params are passed directly to serialization; never store,
      // log, or include them (or transport error bodies) in UI state.
      const pending = this.gateway.request<{ status?: string; resolved?: number }>(`${r.kind}.respond`, params);
      params = {};
      const result = await pending;
      if (!this.isCurrent(r)) return;
      this.phase(r, result.status === "expired" ? "expired" : "resolved");
      if (r.kind === "approval") {
        const pending = await this.gateway.request<{ approvals?: unknown[] }>("approval.pending", { session_id: r.runtimeId });
        if (r.generation !== this.generation || this.stopped) return;
        for (const payload of pending.approvals ?? []) {
          const event = { type: "approval.request", session_id: r.runtimeId, payload };
          const item = readInteraction(event, this.generation);
          if (item) this.set({ ...this.state, interactions: { ...this.state.interactions, [item.key]: item } });
        }
        this.ackApprovals();
      }
      void this.refresh().catch(() => { /* live metadata is recovered on reconnect */ });
    } catch (error) {
      if (!this.isCurrent(r)) return;
      this.phase(r, error instanceof JsonRpcGatewayError ? "pending" : "uncertain", "Response not confirmed. Recover the request before trying again.");
      if (!(error instanceof JsonRpcGatewayError)) void this.connect();
    }
  }
  respondApproval = (r: NativeInteraction, choice: ApprovalChoice) => {
    const current = this.state.interactions[r.key];
    return current?.kind === "approval" && current.choices.includes(choice) ? this.respond(r, { session_id: r.runtimeId, ...(r.requestId ? { request_id: r.requestId } : {}), choice }) : Promise.resolve();
  };
  respondClarify = (r: NativeInteraction, answer: string) => r.kind === "clarify" ? this.respond(r, { request_id: r.requestId, answer }) : Promise.resolve();
  respondSecret = (r: NativeInteraction, value: string) => r.kind === "secret" ? this.respond(r, { request_id: r.requestId, value }) : Promise.resolve();
  respondSudo = (r: NativeInteraction, password: string) => r.kind === "sudo" ? this.respond(r, { request_id: r.requestId, password }) : Promise.resolve();
  respondMcpSetup = (r: NativeInteraction, outcome: { status: "installed" | "enabled" | "authorized" | "declined" | "error"; server: string; detail?: string; tools?: string[] }) => r.kind === "mcp.setup" ? this.respond(r, { request_id: r.requestId, result: JSON.stringify(outcome) }) : Promise.resolve();
  reloadMcp = async (r: NativeInteraction) => {
    if (!this.gateway || !this.isCurrent(r)) return;
    try { await this.gateway.request("reload.mcp", { confirm: true, session_id: r.runtimeId }); }
    catch { if (this.isCurrent(r)) this.set({ ...this.state, control: { ...this.state.control, notice: "Setup succeeded, but live tool reload failed. Reconnect or reload MCP before using the new tools." } }); }
  };

  submit = async (text: string, queued = false) => {
    if (!text.trim() || !this.state.ready || this.state.control.submitting || hasInteraction(this.state.interactions) || !this.gateway || !this.state.runtimeId) return;
    const generation = this.generation;
    this.refreshVersion++;
    const beforeSubmit = this.state.conversation;
    const busy = beforeSubmit.running;
    this.uncertainSubmit = true;
    this.set({ ...this.state, control: { ...this.state.control, submitting: true, notice: "", ...(!busy ? { todos: [], subagents: {} } : {}) }, conversation: busy ? beforeSubmit : beginPrompt(beforeSubmit, text) });
    try {
      const response = await this.gateway.request<{ status: string }>("prompt.submit", { session_id: this.state.runtimeId, text, ...(queued ? { queued: true } : {}) });
      if (generation !== this.generation || this.stopped) return;
      this.uncertainSubmit = false;
      const fresh = response.status === "streaming";
      const conversation = !fresh && !busy ? beforeSubmit : fresh && busy ? beginPrompt(this.state.conversation, text) : this.state.conversation;
      this.set({ ...this.state, durable: this.state.durable || fresh, conversation, control: { ...this.state.control, submitting: false, notice: fresh ? "" : `Message ${response.status}.`, queued: response.status === "queued" ? text : this.state.control.queued } });
      if (!fresh) void this.refresh().catch(() => { /* accepted submit must not be rolled back by a metadata failure */ });
    } catch (error) {
      if (generation !== this.generation || this.stopped) return;
      if (error instanceof JsonRpcGatewayError) {
        this.uncertainSubmit = false;
        this.set({ ...this.state, ready: true, control: { ...this.state.control, submitting: false }, conversation: { ...(busy ? this.state.conversation : beforeSubmit), error: "The gateway rejected this prompt. Correct the problem and send again, or start a new session." } });
        return;
      }
      // Unknown acceptance: recover, never repeat a non-idempotent submission.
      this.set({ ...this.state, ready: false, control: { ...this.state.control, submitting: false }, conversation: failConversation(this.state.conversation, "Prompt submission failed or was not acknowledged. Reconnecting without resending.") });
      void this.connect();
    }
  };
  steer = async (text: string) => {
    if (!text.trim() || !this.gateway || !this.state.runtimeId || !this.state.ready || this.state.control.submitting || hasInteraction(this.state.interactions)) return;
    const generation = this.generation;
    this.refreshVersion++;
    this.set({ ...this.state, control: { ...this.state.control, submitting: true } });
    try {
      const result = await this.gateway.request<{ status: string }>("session.steer", { session_id: this.state.runtimeId, text });
      if (generation !== this.generation || this.stopped) return;
      this.set({ ...this.state, control: { ...this.state.control, submitting: false, notice: result.status === "queued" ? "Steer queued for the next tool boundary." : "Steer rejected. Your message was not queued as a new turn." } });
    } catch {
      if (generation !== this.generation || this.stopped) return;
      this.set({ ...this.state, control: { ...this.state.control, submitting: false, notice: "Steer was not confirmed. It will not be sent again automatically." } });
    }
  };
  interrupt = async () => {
    if (!this.gateway || !this.state.runtimeId || !this.state.ready) return;
    const generation = this.generation;
    try {
      await this.gateway.request("session.interrupt", { session_id: this.state.runtimeId });
      if (generation !== this.generation || this.stopped) return;
      await this.refresh();
    } catch {
      if (generation !== this.generation || this.stopped) return;
      this.set({ ...this.state, conversation: { ...this.state.conversation, error: "Could not stop this turn. Retry Stop or reconnect." } });
    }
  };
}

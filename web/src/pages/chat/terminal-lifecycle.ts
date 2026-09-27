import type { ChatSurfaceLifecycle, SurfaceStatus } from './chat-surface-lifecycle';
import { buildWsUrl } from '@/lib/api';

export const PTY_PROTOCOL = 'hermes.pty-control.v1';
export interface TerminalStatus {
  confirmed?: boolean;
  ready?: boolean;
  blocked?: string[];
  ticket?: string;
  released?: boolean;
  cancelled?: boolean;
  stored_id?: string | null;
}
export interface TerminalOptions {
  profile: string;
  managed?: boolean;
  resume?: string;
  output: (bytes: Uint8Array) => void;
  state: (state: string) => void;
}

/** Instance-scoped transport. Reconnect reattaches output, never replays input. */
export class TerminalLifecycle {
  private socket: WebSocket | null = null;
  private attach = crypto.randomUUID();
  private generation = crypto.randomUUID();
  private instance: string | null = null;
  private pending = new Map<string, { resolve: (value: TerminalStatus) => void; reject: (reason: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  private stopped = false;
  private frozen = true;
  private controlReady = false;
  private accepted = false;
  private retry: ReturnType<typeof setTimeout> | undefined;
  private attempts = 0;
  private options: TerminalOptions;
  private inputEnabled = true;
  private released = false;
  private releaseStarted = false;
  private releaseGeneration?: string;
  private releaseReceipt?: TerminalStatus;
  private ticket?: string;
  private listeners = new Set<() => void>();
  private receiptSockets = new Set<WebSocket>();
  private notify = () => this.listeners.forEach(fn => fn());
  readonly surface: ChatSurfaceLifecycle = {
    subscribe: listener => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; },
    setInput: enabled => { this.inputEnabled = enabled && !this.stopped; },
    status: async () => {
      if (!this.controlReady || !this.instance || !this.socket || this.socket.readyState !== WebSocket.OPEN) return { ready: false, blocked: ['connection recovery'] };
      try { return this.surfaceStatus(await this.command('status')); }
      catch { return { ready: false, blocked: ['Terminal owner initializing or disconnected'] }; }
    },
    prepare: async () => {
      if (this.releaseStarted) {
        this.releaseReceipt = await this.readReceipt(); this.released = true;
        return { ready: true, blocked: [], released: true, storedId: this.releaseReceipt.stored_id };
      }
      const result = await this.command('prepare');
      this.ticket = result.ticket;
      if (!result.ready) {
        // Let the actual TUI resolve its composer/interactions. The next
        // prepare fences all bytes again; the browser never guesses input.
        await this.surface.cancel();
        this.inputEnabled = true;
      }
      return this.surfaceStatus(result);
    },
    cancel: async () => {
      const result = await this.command('cancel');
      if (!result.cancelled || result.released || this.released) throw new Error('Terminal cancellation not acknowledged');
      this.releaseStarted = false;
      this.releaseGeneration = undefined;
      this.releaseReceipt = undefined;
      this.ticket = undefined;
    },
    release: async () => {
      if (this.released) return;
      this.releaseStarted = true;
      this.releaseGeneration = this.generation;
      const result = await this.command('release', this.ticket).catch(() => this.readReceipt());
      if (!result.released) throw new Error('Terminal release not acknowledged');
      this.released = true;
    },
    discard: async () => { throw new Error('Finish or clear input in Terminal, or cancel switching.'); },
    detach: () => this.detach(),
    dispose: async () => {
      this.inputEnabled = false;
      if (!this.instance && !this.socket) { this.stopped = true; return; }
      if (!this.released) {
        if (!this.accepted) {
          this.releaseGeneration = this.generation;
          const result = await this.command('abort').catch(() => this.readReceipt());
          if (!result.released) throw new Error('Terminal abort not acknowledged');
          this.released = true;
        } else {
          const prepared = await this.surface.prepare();
          if (!prepared.ready) throw new Error('Terminal cleanup blocked');
          await this.surface.release();
        }
      }
      this.stopped = true; clearTimeout(this.retry); this.socket?.close(); this.socket = null;
    },
  };
  private surfaceStatus(result: TerminalStatus): SurfaceStatus {
    return { ready: result.confirmed === true && result.ready === true, blocked: result.blocked || [], storedId: result.stored_id, released: result.released };
  }
  constructor(options: TerminalOptions) { this.inputEnabled = !options.managed; this.options = { ...options, state: state => { options.state(state); this.notify(); } }; }

  private async readReceipt(): Promise<TerminalStatus> {
    if (!this.instance) throw new Error('Terminal instance was not acknowledged');
    const url = await buildWsUrl('/api/pty', { profile: this.options.profile, attach: this.attach, instance: this.instance, generation: this.releaseGeneration || this.generation, receipt: '1' });
    if (this.stopped) throw new Error('Terminal host detached');
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url, PTY_PROTOCOL);
      this.receiptSockets.add(socket);
      const timer = setTimeout(() => { socket.close(); reject(new Error('Terminal cleanup receipt unavailable')); }, 16000);
      socket.onmessage = event => {
        const frame = JSON.parse(String(event.data));
        if (frame.type === 'receipt' && frame.result?.released === true) {
          clearTimeout(timer); this.receiptSockets.delete(socket); socket.onclose = null; socket.close(); resolve(frame.result as TerminalStatus);
        }
      };
      socket.onclose = () => { this.receiptSockets.delete(socket); clearTimeout(timer); reject(new Error('Terminal cleanup unconfirmed')); };
      socket.onerror = () => { this.receiptSockets.delete(socket); clearTimeout(timer); socket.close(); reject(new Error('Terminal receipt connection failed')); };
    });
  }

  async connect(): Promise<void> {
    if (this.stopped) return;
    this.generation = crypto.randomUUID();
    this.options.state('connecting');
    const params: Record<string, string> = { profile: this.options.profile, attach: this.attach, generation: this.generation };
    if (this.instance) params.instance = this.instance;
    if (this.options.resume) params.resume = this.options.resume;
    try {
      const url = await buildWsUrl('/api/pty', params);
      if (this.stopped) return;
      const ws = new WebSocket(url, PTY_PROTOCOL);
      this.socket = ws;
      ws.binaryType = 'arraybuffer';
      ws.onmessage = event => {
        if (this.socket !== ws) return;
        if (typeof event.data !== 'string') {
          this.options.output(new Uint8Array(event.data as ArrayBuffer));
          return;
        }
        let frame: Record<string, unknown>;
        try { frame = JSON.parse(event.data) as Record<string, unknown>; } catch { ws.close(1002); return; }
        if (frame.type === 'changed') { this.notify(); return; }
        if (frame.type === 'attached') {
          this.instance = String(frame.instance);
          this.controlReady = frame.control_confirmed === true;
          this.frozen = frame.frozen === true || frame.control_confirmed !== true;
          this.attempts = 0;
          this.options.state(this.frozen ? 'waiting for owner' : 'ready');
        } else if (frame.type === 'owner-ready') {
          this.controlReady = true;
          // A reconnecting control owner may still hold preparation. Only the
          // initial connection can open input without an explicit cancel ACK.
          this.frozen = frame.frozen !== false;
          this.options.state(this.frozen ? 'blocked' : 'ready');
        } else if (frame.type === 'control') {
          const waiter = this.pending.get(String(frame.id));
          if (!waiter) return;
          clearTimeout(waiter.timer);
          this.pending.delete(String(frame.id));
          if (frame.error) waiter.reject(new Error(String(frame.error)));
          else waiter.resolve(frame.result as TerminalStatus);
        }
      };
      ws.onclose = event => {
        if (this.socket !== ws) return;
        this.socket = null;
        this.controlReady = false;
        this.frozen = true;
        this.rejectPending('Terminal disconnected; operation outcome unknown');
        if (this.stopped) return;
        this.options.state('disconnected');
        // No retry before an instance was acknowledged: a lost startup ACK is
        // ambiguous. A new process would risk duplicate ownership.
        if (this.instance && [1006, 1012].includes(event.code) && this.attempts < 5) {
          this.retry = setTimeout(() => void this.connect(), Math.min(1000 * 2 ** this.attempts++, 8000));
        }
      };
      ws.onerror = () => this.options.state('connection error');
    } catch (error) {
      if (!this.stopped) this.options.state(String(error));
    }
  }

  input(text: string): boolean {
    if (!this.inputEnabled || this.frozen || this.socket?.readyState !== WebSocket.OPEN || this.stopped) return false;
    this.accepted = true;
    this.socket.send(new TextEncoder().encode(text));
    return true;
  }

  resize(cols: number, rows: number) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ type: 'resize', cols, rows }));
  }

  async command(action: 'status' | 'prepare' | 'cancel' | 'release' | 'abort', ticket?: string): Promise<TerminalStatus> {
    if ((this.stopped && this.options.managed) || !this.instance || this.socket?.readyState !== WebSocket.OPEN) throw new Error('Terminal owner unavailable');
    if (['prepare', 'release', 'abort'].includes(action)) this.frozen = true;
    const id = crypto.randomUUID();
    const result = await new Promise<TerminalStatus>((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('Terminal lifecycle ACK missing')); }, 16000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket!.send(JSON.stringify({ type: 'control', id, action, ticket, instance: this.instance, generation: this.generation }));
    });
    if (this.stopped && this.options.managed) throw new Error('Terminal host detached');
    if (action === 'cancel' && result.cancelled) this.frozen = false;
    if (action === 'release' && !result.released) throw new Error('Terminal release unconfirmed');
    return result;
  }

  private rejectPending(reason: string) {
    for (const waiter of this.pending.values()) { clearTimeout(waiter.timer); waiter.reject(new Error(reason)); }
    this.pending.clear();
  }

  /** Host teardown is local abandonment, never an authoritative release ACK. */
  detach() {
    this.inputEnabled = false; this.frozen = true; this.stopped = true;
    clearTimeout(this.retry);
    const socket = this.socket; this.socket = null; this.controlReady = false;
    socket?.close();
    for (const receipt of this.receiptSockets) receipt.close();
    this.receiptSockets.clear();
    this.rejectPending('Terminal host detached; ownership remains unconfirmed');
  }

  async dispose() {
    if (this.options.managed) { this.detach(); return; }
    if (this.stopped) return;
    this.stopped = true;
    clearTimeout(this.retry);
    try {
      if (this.instance && this.socket?.readyState === WebSocket.OPEN) {
        if (!this.accepted) await this.command('abort');
        else {
          const prepared = await this.command('prepare');
          if (prepared.ready && prepared.ticket) await this.command('release', prepared.ticket);
          else await this.command('cancel');
        }
      }
    } catch { /* An uncertain owner is detached and reaped, never assumed released. */ }
    this.socket?.close();
    this.socket = null;
    this.rejectPending('Terminal disposed');
  }
}

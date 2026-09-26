import { api } from '@/lib/api';
import type { ChatSurfaceLifecycle, SurfaceStatus } from './chat-switch';

interface Reply { confirmed?: boolean; ready?: boolean; draft?: boolean; ticket?: string; stored_id?: string | null; released?: boolean }
export class TerminalLifecycle implements ChatSurfaceLifecycle {
  private state: SurfaceStatus = { ready: false, blocked: 'Waiting for TUI…', draft: false };
  private listeners = new Set<() => void>();
  private socket: WebSocket | null = null;
  private sendControl?: (data: string) => void;
  private pending = new Map<string, { resolve(value: Reply): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }>();
  private connectionUrl = "";
  private profile = "";
  private generation = "";
  private ticket: string | null = null;
  private checking = false;
  private dirty = false;
  private released = false;
  private readyTimer?: ReturnType<typeof setTimeout>;
  inputEnabled = false;
  setInputEnabled(value: boolean) { this.inputEnabled = value; }
  get intentionalClose() { return this.released; }
  status = () => this.state;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  private set(patch: Partial<SurfaceStatus>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(fn => fn()); }
  attach(ws: WebSocket) {
    this.socket = ws; this.released = false; this.connectionUrl = ws.url;
    this.profile = new URL(ws.url, window.location.href).searchParams.get("profile") ?? "";
    this.generation = crypto.randomUUID();
    clearTimeout(this.readyTimer);
    this.readyTimer = setTimeout(() => { if (!this.state.ready && this.socket === ws) this.set({ error: true }); }, 30_000);
    const send = ws.send.bind(ws);
    this.sendControl = send;
    // Negotiated text frames are exclusively lifecycle messages. Every existing
    // keyboard/IME/paste/shortcut caller continues sending terminal bytes.
    ws.send = data => {
      const resize = typeof data === 'string' && data.startsWith(String.fromCharCode(27) + '[RESIZE:');
      if (!this.inputEnabled && !resize) return;
      send(typeof data === 'string' ? new TextEncoder().encode(data) : data);
    };
    ws.addEventListener('open', () => { void this.refresh(); });
    ws.addEventListener('close', () => {
      if (this.socket !== ws) return;
      this.socket = null;
      for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error('TUI disconnected')); }
      this.pending.clear();
      if (!this.released) this.set({ ready: false, error: true, blocked: 'Waiting for TUI recovery…' });
    });
  }
  frame(data: unknown): boolean {
    if (typeof data !== 'string') return false;
    try {
      const frame = JSON.parse(data) as { handoff?: boolean; changed?: boolean; id?: string; result?: Reply; error?: string };
      if (!frame.handoff) return false;
      const p = frame.id ? this.pending.get(frame.id) : undefined;
      if (p && frame.id) {
        clearTimeout(p.timer); this.pending.delete(frame.id);
        if (frame.error) p.reject(new Error('TUI control unavailable')); else p.resolve(frame.result ?? {});
      }
      if (frame.changed) void this.refresh();
      return true;
    } catch { return false; }
  }
  private request(action: string): Promise<Reply> {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN || !this.sendControl) return Promise.reject(new Error('TUI disconnected'));
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('TUI control timed out')); }, 20_000);
      this.pending.set(id, { resolve, reject, timer });
      this.sendControl!(JSON.stringify({ handoff: true, id, action, ticket: this.ticket, profile: this.profile, generation: this.generation }));
    });
  }
  private async refresh() {
    if (this.released) return;
    if (this.checking) { this.dirty = true; return; }
    this.checking = true;
    const generation = this.generation;
    try {
      const result = await this.request('status');
      if (generation !== this.generation) return;
      if (result.confirmed) clearTimeout(this.readyTimer);
      this.set({ ready: this.state.ready || !!result.confirmed, blocked: result.ready ? null : 'Waiting for the TUI turn, prompt, or input to settle…', draft: false, error: false });
    } catch { this.set({ ready: false, blocked: 'TUI lifecycle channel unavailable. Reconnect or cancel the switch.' }); }
    finally { this.checking = false; if (this.dirty) { this.dirty = false; void this.refresh(); } }
  }
  prepare = async () => {
    this.inputEnabled = false;
    const result = await this.request('prepare');
    if (!result.ready) { this.inputEnabled = true; return null; }
    this.ticket = result.ticket ?? null;
    return { storedId: result.stored_id ?? null };
  };
  cancel = async () => { await this.request('cancel'); this.ticket = null; this.inputEnabled = true; };
  release = async () => {
    const result = await this.request('release');
    if (!result.released) throw new Error('TUI did not release');
    this.released = true; this.socket?.close();
  };
  discard = async () => { throw new Error('Clear or submit the terminal input in Terminal before switching'); };
  dispose = async () => {
    clearTimeout(this.readyTimer); this.inputEnabled = false; this.released = true;
    if (!this.connectionUrl) return;
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      const old = new URL(this.connectionUrl, window.location.href);
      old.searchParams.delete('token'); old.searchParams.delete('ticket');
      const url = await api.buildWsUrl('/api/pty', Object.fromEntries(old.searchParams));
      const ws = new WebSocket(url, 'hermes.pty-control.v1');
      this.attach(ws); this.released = true;
      ws.addEventListener('message', event => { this.frame(event.data); });
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => { ws.close(); reject(new Error('Cleanup unavailable')); }, 10_000);
        ws.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
        ws.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('Cleanup unavailable')); }, { once: true });
      });
    }
    if (this.socket?.readyState === WebSocket.OPEN) {
      const result = await this.request('abort');
      if (!result.released) throw new Error('Target cleanup unconfirmed');
    }
    this.released = true; this.socket?.close();
  };
}

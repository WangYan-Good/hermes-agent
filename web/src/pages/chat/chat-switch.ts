import type { ChatMode } from './chat-mode';

export interface SurfaceStatus { ready: boolean; blocked: string | null; draft: boolean; error?: boolean }
export interface Handoff { storedId: string | null }
export interface ChatSurfaceLifecycle {
  status(): SurfaceStatus;
  subscribe(listener: () => void): () => void;
  prepare(): Promise<Handoff | null>;
  cancel(): Promise<void>;
  release(): Promise<void>;
  discard(): Promise<void>;
  dispose(): Promise<void>;
}
export interface SwitchState {
  phase: 'initializing' | 'stable-native' | 'stable-terminal' | 'switch-requested' | 'waiting-for-idle' | 'switching' | 'failed';
  mounted: ChatMode | null;
  active: ChatMode | null;
  target: ChatMode | null;
  resume: string | null;
  mount: number;
  reason: string;
  draft: boolean;
}

/** One owner, including across asynchronous teardown. Rendering is only an effect of this lifecycle. */
export class ChatSwitch {
  private state: SwitchState;
  private listeners = new Set<() => void>();
  private adapter: ChatSurfaceLifecycle | null = null;
  private unsubscribe?: () => void;
  private commit?: () => void;
  private generation = 0;
  private disposed = false;
  private preparing = false;
  private queued?: { mode: ChatMode; commit?: () => void };
  private previous: ChatMode | null = null;
  constructor(mode: ChatMode, resume: string | null) {
    this.state = { phase: 'initializing', mounted: mode, active: null, target: mode, resume, mount: 0, reason: '', draft: false };
  }
  getSnapshot = () => this.state;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  private set(patch: Partial<SwitchState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(fn => fn()); }
  register = (adapter: ChatSurfaceLifecycle, mount: number) => {
    if (mount !== this.state.mount) return () => {};
    this.adapter = adapter;
    this.unsubscribe?.();
    this.unsubscribe = adapter.subscribe(this.drive);
    this.drive();
    return () => { if (this.adapter === adapter) { this.unsubscribe?.(); this.adapter = null; } };
  };
  request = (mode: ChatMode, commit?: () => void) => {
    if (this.disposed) return;
    if (this.state.phase === 'switching' || this.state.phase === 'initializing') { this.commit = undefined; this.queued = { mode, commit }; return; }
    if (this.state.phase === 'failed' && !this.state.mounted && !this.adapter) {
      this.commit = commit; this.generation++;
      this.set({ phase: 'initializing', mounted: mode, target: mode, mount: this.state.mount + 1 });
      return;
    }
    if (this.state.phase === 'failed' && mode === this.state.active) {
      void this.revert().then(() => { if (!this.disposed && this.state.phase === `stable-${mode}`) commit?.(); });
      return;
    }
    this.commit = commit;
    if (mode === this.state.active && this.state.mounted === mode) {
      this.generation++;
      this.set({ phase: `stable-${mode}`, target: null, reason: '', draft: false });
      this.commit?.(); this.commit = undefined;
      return;
    }
    this.generation++;
    this.set({ target: mode, phase: 'switch-requested', reason: '', draft: false });
    this.drive();
  };
  cancel = () => {
    if (this.preparing || this.state.phase === 'switching' || !this.state.active) return;
    this.commit = undefined; this.generation++;
    this.set({ phase: `stable-${this.state.active}`, target: null, reason: '', draft: false });
  };
  discard = async () => {
    try { await this.adapter?.discard(); this.drive(); }
    catch { this.set({ reason: 'Draft removal was not confirmed. Retry or cancel the switch.' }); }
  };
  private drive = () => {
    if (this.disposed || this.preparing || !this.adapter) return;
    const status = this.adapter.status();
    if ((this.state.phase === 'initializing' || this.state.phase === 'switching') && this.state.mounted === this.state.target) {
      if (status.error) { void this.targetFailed(); return; }
      if (!status.ready) return;
      const mode = this.state.mounted!;
      this.set({ active: mode, target: null, phase: `stable-${mode}`, reason: '', draft: false });
      this.commit?.(); this.commit = undefined;
      const queued = this.queued; this.queued = undefined;
      if (queued) this.request(queued.mode, queued.commit);
      return;
    }
    if (!['switch-requested', 'waiting-for-idle'].includes(this.state.phase)) return;
    if (!status.ready || status.blocked || status.draft) {
      this.set({ phase: 'waiting-for-idle', reason: status.blocked || (status.draft ? 'Discard the unsent draft to switch, or cancel.' : 'Waiting for authoritative recovery…'), draft: status.draft });
      return;
    }
    void this.handoff();
  };
  private async handoff() {
    const adapter = this.adapter!; const generation = this.generation; const target = this.state.target!;
    this.preparing = true;
    this.set({ phase: 'switching', reason: 'Preparing a safe handoff…' });
    try {
      const handoff = await adapter.prepare();
      if (this.disposed || generation !== this.generation) { await adapter.cancel(); return; }
      if (!handoff) {
        this.set({ phase: 'waiting-for-idle', reason: 'Waiting for the current turn and interactions to settle…' });
        return;
      }
      this.previous = this.state.active;
      this.set({ resume: handoff.storedId });
      await adapter.release();
      if (this.disposed || generation !== this.generation) return;
      this.unsubscribe?.(); this.adapter = null;
      this.set({ active: null, mounted: target, mount: this.state.mount + 1, reason: handoff.storedId ? 'Restoring conversation…' : 'Opening a new draft in the selected interface…' });
    } catch {
      try { await adapter.cancel(); } catch { /* uncertain ownership remains blocked */ }
      if (!this.disposed && generation === this.generation) this.set({ phase: 'failed', reason: 'Could not complete the handoff. Retry or return to the previous interface.' });
    } finally { this.preparing = false; }
  }
  fail = () => { void this.targetFailed(); };
  private async targetFailed() {
    if (this.disposed || this.preparing) return;
    this.preparing = true;
    const generation = this.generation;
    try {
      await this.adapter?.dispose();
      this.unsubscribe?.(); this.adapter = null;
      if (!this.disposed && generation === this.generation) this.set({ phase: 'failed', mounted: null, reason: 'Could not initialize this interface. Your conversation is preserved; no message was resent.' });
    } catch {
      if (!this.disposed) this.set({ phase: 'failed', reason: 'Target cleanup could not be confirmed. Retry before restoring another interface.' });
    } finally { this.preparing = false; }
  }
  retry = () => {
    if (this.state.phase !== 'failed' || this.preparing) return;
    if (this.state.mounted && this.adapter && this.state.mounted !== this.state.active) {
      void this.targetFailed().then(() => { if (!this.disposed && !this.state.mounted) this.retry(); });
      return;
    }
    if (this.state.mounted && this.adapter) {
      this.set({ phase: 'switch-requested' }); this.drive();
    } else {
      const target = this.state.target ?? this.previous ?? this.state.active ?? 'native';
      this.set({ phase: 'initializing', mounted: target, target, mount: this.state.mount + 1 });
    }
  };
  revert = async () => {
    if (this.state.phase !== 'failed' || this.preparing) return;
    this.commit = undefined;
    if (this.state.mounted === this.state.active && this.adapter) {
      try { await this.adapter.cancel(); this.cancel(); } catch { this.set({ reason: 'Previous ownership is not confirmed. Retry recovery before sending.' }); }
      return;
    }
    if (this.adapter) {
      try { await this.adapter.dispose(); this.unsubscribe?.(); this.adapter = null; }
      catch { this.set({ reason: 'Target cleanup could not be confirmed. Retry before restoring another interface.' }); return; }
    }
    const mode = this.previous ?? this.state.active;
    if (mode) this.set({ phase: 'initializing', mounted: mode, target: mode, mount: this.state.mount + 1 });
  };
  activate = () => { this.disposed = false; this.drive(); };
  dispose = () => { this.disposed = true; this.generation++; this.unsubscribe?.(); };
}

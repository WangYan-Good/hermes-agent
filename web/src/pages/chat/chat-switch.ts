import type { ChatMode } from './chat-mode';
import type { ChatSurfaceLifecycle } from './chat-surface-lifecycle';

export interface SwitchState {
  phase: 'initializing' | 'stable' | 'switch-requested' | 'waiting-for-idle' | 'switching' | 'failed';
  mounted: ChatMode;
  requested: ChatMode;
  owner: ChatMode | null;
  target: ChatMode | null;
  storedId: string | null;
  generation: number;
  blocked: string[];
  error: string;
}

/** Presentation is an effect of acknowledged ownership, never its authority. */
export class ChatSwitch {
  private state: SwitchState;
  private surface: ChatSurfaceLifecycle | null = null;
  private unsubscribe?: () => void;
  private timer?: ReturnType<typeof setTimeout>;
  private listeners = new Set<() => void>();
  private running = false;
  private cleanupPending = false;
  private everAttached = false;
  private stopped = false;
  private previous: ChatMode;
  private commit?: () => void;
  private cancelled = false;
  private deadline = Date.now() + 45_000;
  private onStable: (mode: ChatMode) => void;
  constructor(mode: ChatMode, storedId: string | null, onStable: (mode: ChatMode) => void = () => {}) {
    this.onStable = onStable;
    this.previous = mode;
    this.state = { phase: 'initializing', mounted: mode, requested: mode, owner: null, target: mode, storedId, generation: 0, blocked: [], error: '' };
  }
  onReady = (callback: (mode: ChatMode) => void) => { this.onStable = callback; };
  getSnapshot = () => this.state;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  private set(patch: Partial<SwitchState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(fn => fn()); }
  attach = (surface: ChatSurfaceLifecycle | null, generation: number) => {
    if (this.stopped || generation !== this.state.generation) return;
    this.unsubscribe?.();
    this.surface = surface;
    if (surface) {
      this.everAttached = true;
      surface.setInput(this.state.phase === 'stable');
      this.unsubscribe = surface.subscribe(this.wake);
      this.wake();
    }
  };
  request = (mode: ChatMode, commit?: () => void) => {
    if (this.stopped || this.state.phase !== 'stable') return false;
    if (mode === this.state.mounted) { commit?.(); return true; }
    this.previous = this.state.mounted; this.commit = commit; this.cancelled = false;
    this.surface?.setInput(false);
    this.set({ requested: mode, target: mode, phase: 'switch-requested', error: '', blocked: [] });
    this.wake(); return true;
  };
  private wake = () => { clearTimeout(this.timer); if (!this.running && !this.stopped) void this.drive(); };
  private async drive() {
    if (this.running || this.stopped || !this.surface || ['stable', 'failed'].includes(this.state.phase)) return;
    this.running = true;
    const surface = this.surface, generation = this.state.generation;
    const current = () => !this.stopped && this.surface === surface && this.state.generation === generation;
    try {
      if (this.cancelled && this.state.owner) {
        await surface.cancel();
        if (!current()) return;
        surface.setInput(true); this.commit = undefined; this.cancelled = false;
        this.set({ phase: 'stable', requested: this.state.mounted, target: null, blocked: [], error: '' });
      } else if (this.state.phase === 'initializing' || this.state.phase === 'switching') {
        const status = await surface.status();
        if (!current()) return;
        if (status.ready && (!this.state.storedId || status.storedId === this.state.storedId)) {
          if (this.cancelled) { await this.cleanupAndMount(this.previous); return; }
          surface.setInput(true);
          this.set({ phase: 'stable', owner: this.state.mounted, target: null, blocked: [] });
          this.commit?.(); this.commit = undefined;
          this.onStable(this.state.mounted);
        } else {
          this.set({ blocked: status.blocked });
          if (Date.now() > this.deadline) throw new Error('Interface initialization was not confirmed. Retry or return after cleanup.');
        }
      } else {
        const prepared = await surface.prepare();
        if (!current()) return;
        if (!prepared.ready || !prepared.storedId) {
          this.set({ phase: 'waiting-for-idle', blocked: prepared.blocked });
        } else {
          if (this.cancelled) return;
          // A zero-owner interval is intentional. Target input cannot exist yet.
          await surface.release();
          if (!current()) return;
          this.unsubscribe?.(); this.surface = null; this.everAttached = false;
          this.deadline = Date.now() + 45_000;
          this.set({ phase: 'switching', owner: null, storedId: prepared.storedId, mounted: this.state.target!, generation: generation + 1, blocked: [] });
        }
      }
    } catch (error) {
      if (current()) this.set({ phase: 'failed', error: error instanceof Error ? error.message : 'Switch could not be confirmed.' });
    } finally {
      this.running = false;
      if (!this.stopped && !['stable', 'failed'].includes(this.state.phase)) this.timer = setTimeout(this.wake, 500);
    }
  }
  failMount = () => { this.surface?.setInput(false); this.set({ phase: 'failed', error: 'Interface failed to load. Retry or return after cleanup.' }); };
  cancel = () => { this.cancelled = true; if (this.state.phase === 'failed') this.set({ phase: this.state.owner ? 'waiting-for-idle' : 'switching', error: '' }); this.wake(); };
  discard = async () => {
    try { await this.surface?.discard(); this.wake(); }
    catch { this.set({ phase: 'failed', error: 'Draft removal was not confirmed. Recover the source before switching.' }); }
  };
  retry = () => {
    if (this.state.owner) { this.set({ phase: 'waiting-for-idle', error: '' }); this.wake(); }
    else void this.cleanupAndMount(this.state.mounted);
  };
  revert = () => { if (this.state.owner) this.cancel(); else void this.cleanupAndMount(this.previous); };
  private async cleanupAndMount(mode: ChatMode) {
    if (this.cleanupPending || (this.running && !this.cancelled)) return;
    this.cleanupPending = true;
    const generation = this.state.generation;
    this.surface?.setInput(false);
    try {
      if (!this.surface && this.everAttached) throw new Error('Target cleanup unavailable. Ownership remains unconfirmed.');
      if (this.surface) await this.surface.dispose();
      if (this.stopped || generation !== this.state.generation) return;
      this.unsubscribe?.(); this.surface = null; this.everAttached = false; this.cancelled = false;
      if (mode === this.previous) this.commit = undefined;
      this.deadline = Date.now() + 45_000;
      this.set({ phase: 'switching', mounted: mode, target: mode, requested: mode, generation: generation + 1, blocked: [], error: '' });
    } catch { if (!this.stopped && generation === this.state.generation) this.set({ phase: 'failed', error: 'Target cleanup was not acknowledged. Another interface cannot be opened safely.' }); }
    finally { this.cleanupPending = false; }
  }
  stop = () => {
    this.stopped = true; clearTimeout(this.timer); this.unsubscribe?.(); this.commit = undefined;
    this.surface?.setInput(false);
    // Teardown must never be mistaken for a successful handoff.
    this.surface?.detach();
  };
}

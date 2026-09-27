import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ChatSwitch } from './chat-switch';
import type { ChatSurfaceLifecycle, SurfaceStatus } from './chat-surface-lifecycle';
class Surface implements ChatSurfaceLifecycle {
  view: SurfaceStatus = { ready: true, blocked: [], storedId: 'canonical' };
  input = false;
  listeners = new Set<() => void>();
  status = vi.fn(async () => this.view);
  prepare = vi.fn(async () => { this.input = false; return this.view; });
  cancel = vi.fn(async () => {});
  release = vi.fn(async () => { this.input = false; });
  discard = vi.fn(async () => { this.view = { ...this.view, ready: true, blocked: [] }; });
  dispose = vi.fn(async () => { this.input = false; });
  setInput = (enabled: boolean) => { this.input = enabled; };
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
}
let machines: ChatSwitch[];
beforeEach(() => { vi.useFakeTimers(); machines = []; });
afterEach(() => { machines.forEach(m => m.stop()); vi.useRealTimers(); });
async function flush() { for (let n = 0; n < 20; n++) await Promise.resolve(); }
async function setup(mode: 'native' | 'terminal' = 'native') {
  const machine = new ChatSwitch(mode, null); machines.push(machine);
  const source = new Surface(); machine.attach(source, 0); await flush();
  expect(machine.getSnapshot().phase).toBe('stable'); return { machine, source };
}
it.each(['native', 'terminal'] as const)('switches from %s only after release and commits only after target ready', async mode => {
  const { machine, source } = await setup(mode), commit = vi.fn();
  let ack!: () => void;
  source.release.mockImplementation(() => new Promise(resolve => { ack = resolve; }));
  machine.request(mode === 'native' ? 'terminal' : 'native', commit); await flush();
  expect(machine.getSnapshot().mounted).toBe(mode); expect(source.input).toBe(false); expect(commit).not.toHaveBeenCalled();
  ack(); await flush();
  expect(machine.getSnapshot()).toMatchObject({ owner: null, storedId: 'canonical', generation: 1 });
  const target = new Surface(); target.view.ready = false;
  machine.attach(target, 1); await flush();
  expect(target.input).toBe(false); expect(commit).not.toHaveBeenCalled();
  target.view.ready = true; await vi.advanceTimersByTimeAsync(501);
  expect(source.input && target.input).toBe(false); expect(target.input).toBe(true); expect(commit).toHaveBeenCalledTimes(1);
});
it.each(['turn', 'queue', 'approval', 'clarify', 'secret', 'sudo', 'mcp', 'draft', 'attachments', 'uncertain submit', 'connection recovery', 'composer'])('defers for %s without releasing or committing', async reason => {
  const { machine, source } = await setup(), commit = vi.fn();
  source.view = { ready: false, blocked: [reason], storedId: 'canonical' };
  machine.request('terminal', commit); await flush();
  expect(machine.getSnapshot()).toMatchObject({ phase: 'waiting-for-idle', mounted: 'native', owner: 'native' });
  expect(source.release).not.toHaveBeenCalled(); expect(source.discard).not.toHaveBeenCalled(); expect(commit).not.toHaveBeenCalled();
  source.view = { ready: true, blocked: [], storedId: 'canonical' };
  await vi.advanceTimersByTimeAsync(501);
  expect(source.release).toHaveBeenCalledOnce();
});
it('cancel requires ACK before source reopens', async () => {
  const { machine, source } = await setup();
  source.view.ready = false; machine.request('terminal'); await flush();
  let ack!: () => void; source.cancel.mockImplementation(() => new Promise(resolve => { ack = resolve; }));
  machine.cancel(); await flush(); expect(source.input).toBe(false);
  ack(); await flush(); expect(source.input).toBe(true); expect(machine.getSnapshot().phase).toBe('stable');
});
it('lost release ACK leaves the source authoritative and never mounts target', async () => {
  const { machine, source } = await setup(); source.release.mockRejectedValue(new Error('missing ACK'));
  machine.request('terminal'); await flush();
  expect(machine.getSnapshot()).toMatchObject({ phase: 'failed', owner: 'native', mounted: 'native' });
  expect(source.input).toBe(false);
});
it('target cleanup failure cannot reopen previous owner', async () => {
  const { machine } = await setup(), commit = vi.fn(); machine.request('terminal', commit); await flush();
  const target = new Surface(); target.status.mockRejectedValue(new Error('startup')); machine.attach(target, 1); await flush();
  target.dispose.mockRejectedValue(new Error('cleanup unknown')); machine.revert(); await flush();
  expect(machine.getSnapshot()).toMatchObject({ phase: 'failed', owner: null, mounted: 'terminal', generation: 1 });
  expect(target.input).toBe(false); expect(commit).not.toHaveBeenCalled();
});
it('authoritative target cleanup permits retry and revert with the durable identity', async () => {
  const { machine } = await setup(); machine.request('terminal'); await flush();
  const target = new Surface(); target.status.mockRejectedValue(new Error('startup')); machine.attach(target, 1); await flush();
  machine.revert(); await flush();
  expect(target.dispose).toHaveBeenCalledOnce();
  expect(machine.getSnapshot()).toMatchObject({ mounted: 'native', storedId: 'canonical', generation: 2, owner: null });
});
it('stale profile or mount callback cannot enable an owner or commit', async () => {
  const { machine, source } = await setup(), commit = vi.fn();
  let ack!: () => void; source.release.mockImplementation(() => new Promise(resolve => { ack = resolve; }));
  machine.request('terminal', commit); await flush(); const beforeStop = machine.getSnapshot(); machine.stop(); ack(); await flush();
  expect(machine.getSnapshot()).toEqual(beforeStop);
  const target = new Surface(); machine.attach(target, 1); await flush();
  expect(target.input).toBe(false); expect(commit).not.toHaveBeenCalled();
});
it('rejects another preference command while switching', async () => {
  const { machine, source } = await setup(); source.view.ready = false;
  expect(machine.request('terminal')).toBe(true); expect(machine.request('native')).toBe(false);
});
it('explicit draft discard is the only path that calls discard', async () => {
  const { machine, source } = await setup(); source.view = { ready: false, blocked: ['draft'], storedId: 'canonical' };
  machine.request('terminal'); await flush(); expect(source.discard).not.toHaveBeenCalled();
  await machine.discard(); await flush(); expect(source.discard).toHaveBeenCalledOnce();
});

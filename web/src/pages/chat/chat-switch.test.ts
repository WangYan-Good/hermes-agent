import { expect, it, vi } from 'vitest';
import { ChatSwitch, type ChatSurfaceLifecycle, type SurfaceStatus } from './chat-switch';

function surface() {
  let value: SurfaceStatus = { ready: true, blocked: null, draft: false };
  const listeners = new Set<() => void>();
  const adapter: ChatSurfaceLifecycle = {
    status: () => value,
    subscribe: fn => { listeners.add(fn); return () => { listeners.delete(fn); }; },
    prepare: vi.fn(async () => ({ storedId: 'durable-tip' })),
    cancel: vi.fn(async () => {}), release: vi.fn(async () => {}), dispose: vi.fn(async () => {}), discard: vi.fn(async () => {}),
  };
  return { adapter, update: (patch: Partial<SurfaceStatus>) => { value = { ...value, ...patch }; listeners.forEach(fn => fn()); } };
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
it.each(['native', 'terminal'] as const)('%s waits for idle and acknowledged release before giving the target ownership', async source => {
  const target = source === 'native' ? 'terminal' : 'native';
  const machine = new ChatSwitch(source, null); const old = surface();
  machine.register(old.adapter, 0);
  old.update({ blocked: 'running' });
  const commit = vi.fn(); machine.request(target, commit);
  expect(machine.getSnapshot().phase).toBe('waiting-for-idle');
  expect(old.adapter.release).not.toHaveBeenCalled(); expect(commit).not.toHaveBeenCalled();
  let release!: () => void;
  old.adapter.release = vi.fn(() => new Promise<void>(resolve => { release = resolve; }));
  old.update({ blocked: null }); await flush();
  expect(machine.getSnapshot().mounted).toBe(source);
  release(); await flush();
  expect(machine.getSnapshot()).toMatchObject({ mounted: target, resume: 'durable-tip', active: null });
  expect(commit).not.toHaveBeenCalled();
  const next = surface(); next.update({ ready: false }); machine.register(next.adapter, 1);
  expect(commit).not.toHaveBeenCalled(); next.update({ ready: true });
  expect(machine.getSnapshot().active).toBe(target); expect(commit).toHaveBeenCalledTimes(1);
  expect(old.adapter.prepare).toHaveBeenCalledTimes(1); expect(old.adapter.release).toHaveBeenCalledTimes(1);
});
it('cancel and repeated valid intent do not remount or commit an abandoned preference', async () => {
  const machine = new ChatSwitch('native', null); const source = surface(); machine.register(source.adapter, 0);
  source.update({ blocked: 'approval' }); const abandoned = vi.fn();
  machine.request('terminal', abandoned); machine.request('terminal', abandoned); machine.cancel(); source.update({ blocked: null }); await flush();
  expect(abandoned).not.toHaveBeenCalled(); expect(source.adapter.prepare).not.toHaveBeenCalled();
  expect(machine.getSnapshot()).toMatchObject({ active: 'native', mount: 0, target: null });
});
it('unknown acceptance, sensitive interactions and unsent drafts preserve the source owner', () => {
  for (const reason of ['unknown acceptance', 'sudo', 'approval', 'queued']) {
    const machine = new ChatSwitch('native', null); const source = surface(); machine.register(source.adapter, 0);
    source.update({ blocked: reason }); machine.request('terminal');
    expect(source.adapter.prepare).not.toHaveBeenCalled(); expect(machine.getSnapshot().mounted).toBe('native');
  }
  const machine = new ChatSwitch('native', null); const source = surface(); machine.register(source.adapter, 0);
  source.update({ draft: true }); machine.request('terminal'); expect(machine.getSnapshot().draft).toBe(true);
  expect(source.adapter.discard).not.toHaveBeenCalled(); expect(source.adapter.prepare).not.toHaveBeenCalled();
});
it('failed target keeps canonical identity and never commits its preference', async () => {
  const machine = new ChatSwitch('native', null); const source = surface(); machine.register(source.adapter, 0);
  const commit = vi.fn(); machine.request('terminal', commit); await flush();
  const target = surface(); target.update({ ready: false, error: true }); machine.register(target.adapter, 1); await flush();
  expect(machine.getSnapshot()).toMatchObject({ phase: 'failed', mounted: null, resume: 'durable-tip' });
  expect(commit).not.toHaveBeenCalled(); expect(target.adapter.dispose).toHaveBeenCalledTimes(1);
  machine.revert(); expect(machine.getSnapshot()).toMatchObject({ mounted: 'native', resume: 'durable-tip' });
});
it('unmount/profile replacement invalidates pending release and callbacks', async () => {
  const machine = new ChatSwitch('native', null); const source = surface(); machine.register(source.adapter, 0);
  let prepare!: (value: { storedId: string }) => void;
  source.adapter.prepare = () => new Promise(resolve => { prepare = resolve; });
  const commit = vi.fn(); machine.request('terminal', commit); machine.dispose(); prepare({ storedId: 'profile-a' }); await flush();
  expect(source.adapter.cancel).toHaveBeenCalledTimes(1); expect(source.adapter.release).not.toHaveBeenCalled(); expect(commit).not.toHaveBeenCalled();
});
it('last intent during an atomic handoff waits for its target to initialize', async () => {
  const machine = new ChatSwitch('native', null); const old = surface(); machine.register(old.adapter, 0);
  const abandoned = vi.fn(); const latest = vi.fn();
  machine.request('terminal', abandoned); machine.request('native', latest); await flush();
  const terminal = surface(); machine.register(terminal.adapter, 1); await flush();
  expect(machine.getSnapshot().mounted).toBe('native'); expect(terminal.adapter.release).toHaveBeenCalledTimes(1);
  expect(abandoned).not.toHaveBeenCalled(); expect(latest).not.toHaveBeenCalled();
  machine.register(surface().adapter, 2); expect(latest).toHaveBeenCalledTimes(1);
});
it('cannot restore a source until failed-target cleanup is confirmed', async () => {
  const machine = new ChatSwitch('native', null); const old = surface(); machine.register(old.adapter, 0);
  machine.request('terminal'); await flush();
  const target = surface(); target.adapter.dispose = vi.fn(async () => { throw new Error('offline'); });
  target.update({ ready: false, error: true }); machine.register(target.adapter, 1); await flush();
  expect(machine.getSnapshot()).toMatchObject({ phase: 'failed', mounted: 'terminal', active: null });
  await machine.revert(); expect(machine.getSnapshot().mounted).toBe('terminal');
  target.adapter.dispose = vi.fn(async () => {});
  await machine.revert(); expect(machine.getSnapshot()).toMatchObject({ mounted: 'native', resume: 'durable-tip' });
});
it('initialization failure permits another interface only after target disposal', async () => {
  const machine = new ChatSwitch('native', 'existing-durable'); const failed = surface();
  failed.update({ ready: false, error: true }); machine.register(failed.adapter, 0); await flush();
  const commit = vi.fn(); machine.request('terminal', commit);
  expect(failed.adapter.dispose).toHaveBeenCalledTimes(1);
  expect(machine.getSnapshot()).toMatchObject({ mounted: 'terminal', target: 'terminal', resume: 'existing-durable', phase: 'initializing' });
  expect(commit).not.toHaveBeenCalled(); machine.register(surface().adapter, 1);
  expect(commit).toHaveBeenCalledTimes(1);
});

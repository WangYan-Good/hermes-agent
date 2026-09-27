import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { buildWsUrl } from '@/lib/api';
import { PTY_PROTOCOL, TerminalLifecycle } from './terminal-lifecycle';
vi.mock('@/lib/api', () => ({ buildWsUrl: vi.fn(async () => 'ws://local/api/pty?ticket=fresh') }));
class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  binaryType = '';
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: (string | Uint8Array)[] = [];
  url: string;
  protocol: string;
  constructor(url: string, protocol: string) { this.url = url; this.protocol = protocol; Socket.instances.push(this); }
  send(data: string | Uint8Array) { this.sent.push(data); }
  frame(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  close(code = 1000) { this.readyState = 3; this.onclose?.({ code }); }
}
beforeEach(() => { Socket.instances = []; vi.stubGlobal('WebSocket', Socket); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks(); });
async function setup() {
  const output = vi.fn(), state = vi.fn();
  const lifecycle = new TerminalLifecycle({ profile: 'work', output, state });
  await lifecycle.connect();
  const socket = Socket.instances[0];
  socket.frame({ type: 'attached', instance: 'instance', control_confirmed: true });
  return { lifecycle, socket, output, state };
}
it('creates distinct secure ownership and control IDs across insecure HTTP reconnects', async () => {
  vi.stubGlobal('isSecureContext', false);
  vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
  const { lifecycle, socket } = await setup();
  try {
    const first = vi.mocked(buildWsUrl).mock.calls.at(-1)![1]!;
    const status = lifecycle.command('status');
    const request = JSON.parse(socket.sent.at(-1) as string);
    socket.frame({ type: 'control', id: request.id, result: { ready: true, confirmed: true } });
    expect(await status).toMatchObject({ ready: true });
    socket.close(1006);
    await vi.advanceTimersByTimeAsync(1000);
    const second = vi.mocked(buildWsUrl).mock.calls.at(-1)![1]!;
    expect(second.attach).toBe(first.attach);
    expect(second.instance).toBe('instance');
    const ids = [first.attach, first.generation, request.id, second.generation];
    for (const id of ids) expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    expect(new Set(ids).size).toBe(ids.length);
    expect(Socket.instances[1].sent).toEqual([]);
  } finally {
    lifecycle.detach();
  }
});
it('authenticates each connection, negotiates protocol, sends binary input and structured resize', async () => {
  const { lifecycle, socket, output } = await setup();
  expect(buildWsUrl).toHaveBeenCalledWith('/api/pty', expect.objectContaining({ profile: 'work' }));
  expect(socket.protocol).toBe(PTY_PROTOCOL);
  expect(lifecycle.input('{"action":"release"}')).toBe(true);
  expect(socket.sent[0]).toBeInstanceOf(Uint8Array);
  lifecycle.resize(100, 35);
  expect(JSON.parse(socket.sent[1] as string)).toEqual({ type: 'resize', cols: 100, rows: 35 });
  socket.onmessage?.({ data: new Uint8Array([65]).buffer });
  expect(output).toHaveBeenCalledWith(new Uint8Array([65]));
  socket.close(); await lifecycle.dispose();
});
it('freezes prepare input until cancel ACK and rejects release without ACK', async () => {
  const { lifecycle, socket } = await setup();
  const pending = lifecycle.command('prepare');
  expect(lifecycle.input('new input')).toBe(false);
  const req = JSON.parse(socket.sent.at(-1) as string);
  socket.frame({ type: 'control', id: req.id, result: { ready: true, ticket: 'ticket' } });
  await pending;
  const cancel = lifecycle.command('cancel');
  expect(lifecycle.input('still blocked')).toBe(false);
  const c = JSON.parse(socket.sent.at(-1) as string);
  socket.frame({ type: 'control', id: c.id, result: { cancelled: true } });
  await cancel;
  expect(lifecycle.input('accepted')).toBe(true);
  const release = lifecycle.command('release', 'ticket');
  const assertion = expect(release).rejects.toThrow('ACK missing');
  await vi.advanceTimersByTimeAsync(16000);
  await assertion;
  expect(lifecycle.input('uncertain')).toBe(false);
  socket.close(); await lifecycle.dispose();
});
it('reconnects only the acknowledged instance, gets fresh auth and never repeats input', async () => {
  const { lifecycle, socket } = await setup();
  lifecycle.input('once'); socket.close(1006);
  expect(lifecycle.input('lost')).toBe(false);
  await vi.advanceTimersByTimeAsync(1000);
  expect(buildWsUrl).toHaveBeenLastCalledWith('/api/pty', expect.objectContaining({ instance: 'instance', profile: 'work' }));
  expect(Socket.instances[1].sent).toEqual([]);
  Socket.instances[1].close(); await lifecycle.dispose();
});
it('does not restart an ambiguous startup or intentionally closed connection', async () => {
  const lifecycle = new TerminalLifecycle({ profile: '', output: vi.fn(), state: vi.fn() });
  await lifecycle.connect(); Socket.instances[0].close(1006);
  await vi.advanceTimersByTimeAsync(60000);
  expect(Socket.instances).toHaveLength(1);
  await lifecycle.dispose();
});
it('profile replacement/unmount during ticket acquisition cannot open a late socket', async () => {
  let finish!: (url: string) => void;
  vi.mocked(buildWsUrl).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const lifecycle = new TerminalLifecycle({ profile: 'old', output: vi.fn(), state: vi.fn() });
  const connect = lifecycle.connect();
  await lifecycle.dispose(); finish('ws://late'); await connect;
  expect(Socket.instances).toHaveLength(0);
});
it('queries the releasing generation after a lost ACK and viewer reconnect', async () => {
  const { lifecycle, socket } = await setup();
  const release = lifecycle.surface.release();
  const rejection = expect(release).rejects.toThrow('unconfirmed');
  const generation = JSON.parse(socket.sent.at(-1) as string).generation;
  socket.close(1006);
  await vi.advanceTimersByTimeAsync(0);
  Socket.instances[1].close(4409);
  await rejection;
  await vi.advanceTimersByTimeAsync(1000);
  const recovered = lifecycle.surface.prepare();
  await vi.advanceTimersByTimeAsync(0);
  expect(buildWsUrl).toHaveBeenLastCalledWith('/api/pty', expect.objectContaining({ receipt: '1', generation }));
  Socket.instances.at(-1)!.frame({ type: 'receipt', result: { released: true, stored_id: 'durable' } });
  expect(await recovered).toMatchObject({ released: true, storedId: 'durable' });
  await lifecycle.surface.dispose();
});

it('cancel resets an unaccepted release attempt so the next prepare uses the owner, retaining accepted input history', async () => {
  const { lifecycle, socket } = await setup();
  lifecycle.input('explicit input');
  const reply = (result: object) => socket.frame({ type: 'control', id: JSON.parse(socket.sent.at(-1) as string).id, result });
  const first = lifecycle.surface.prepare(); reply({ confirmed: true, ready: true, ticket: 'first', stored_id: 'durable' }); await first;
  const send = socket.send.bind(socket);
  vi.spyOn(socket, 'send').mockImplementation(data => {
    if (typeof data === 'string' && JSON.parse(data).action === 'release') throw new Error('not sent');
    send(data);
  });
  const release = lifecycle.surface.release();
  const failed = expect(release).rejects.toThrow('unconfirmed');
  await vi.advanceTimersByTimeAsync(0); Socket.instances.at(-1)!.close(4409); await failed;
  vi.mocked(socket.send).mockRestore();
  const cancel = lifecycle.surface.cancel(); reply({ cancelled: true }); await cancel;
  lifecycle.surface.setInput(true); expect(lifecycle.input('explicit after cancel')).toBe(true);
  const second = lifecycle.surface.prepare();
  expect(typeof socket.sent.at(-1)).toBe('string');
  expect(JSON.parse(socket.sent.at(-1) as string).action).toBe('prepare');
  reply({ confirmed: true, ready: true, ticket: 'second', stored_id: 'durable' }); await second;
  // Accepted input history must still rule out the pre-input abort path.
  const cleanup = lifecycle.surface.dispose();
  expect(JSON.parse(socket.sent.at(-1) as string).action).toBe('prepare');
  reply({ confirmed: true, ready: true, ticket: 'cleanup', stored_id: 'durable' });
  await vi.advanceTimersByTimeAsync(0);
  expect(JSON.parse(socket.sent.at(-1) as string).action).toBe('release');
  reply({ released: true }); await cleanup;
});

it('busy managed host unmount detaches locally without claiming release or reconnecting', async () => {
  const lifecycle = new TerminalLifecycle({ profile: '', managed: true, output: vi.fn(), state: vi.fn() });
  await lifecycle.connect(); const socket = Socket.instances[0];
  socket.frame({ type: 'attached', instance: 'busy', control_confirmed: true });
  lifecycle.surface.setInput(true); expect(lifecycle.input('explicit input')).toBe(true);
  const reply = (result: object) => socket.frame({ type: 'control', id: JSON.parse(socket.sent.at(-1) as string).id, result });
  const cleanup = lifecycle.surface.dispose(); const blocked = expect(cleanup).rejects.toThrow('blocked');
  reply({ confirmed: true, ready: false, blocked: ['turn'] }); await vi.advanceTimersByTimeAsync(0);
  reply({ cancelled: true }); await blocked;
  await lifecycle.dispose();
  expect(socket.readyState).toBe(3);
  expect(lifecycle.input('after unmount')).toBe(false);
  await expect(lifecycle.command('status')).rejects.toThrow();
  await vi.advanceTimersByTimeAsync(60000);
  expect(Socket.instances).toHaveLength(1);
  const commands = socket.sent.filter(value => typeof value === 'string').map(value => JSON.parse(value as string).action);
  expect(commands).not.toContain('release'); expect(commands).not.toContain('abort');
});

// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { TerminalLifecycle } from './terminal-lifecycle';
vi.mock('@/lib/api', () => ({ api: { buildWsUrl: vi.fn() } }));
afterEach(() => vi.useRealTimers());
function setup() {
  vi.useFakeTimers();
  const ws = Object.assign(new EventTarget(), { url: 'ws://localhost/api/pty?profile=work', readyState: WebSocket.OPEN, send: vi.fn(), close: vi.fn() });
  const sent = ws.send;
  const lifecycle = new TerminalLifecycle(); lifecycle.attach(ws as unknown as WebSocket);
  const reply = (result: object) => { const request = JSON.parse(sent.mock.calls.at(-1)![0]); lifecycle.frame(JSON.stringify({ handoff: true, id: request.id, result })); return request; };
  return { ws, sent, lifecycle, reply };
}
const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
it('socket open is not target ready; TUI session confirmation is required', async () => {
  const { ws, lifecycle, reply } = setup(); ws.dispatchEvent(new Event('open'));
  reply({ ready: false }); await flush(); expect(lifecycle.status().ready).toBe(false);
  lifecycle.frame(JSON.stringify({ handoff: true, changed: true }));
  reply({ ready: true, confirmed: true }); await flush(); expect(lifecycle.status().ready).toBe(true);
});
it('sends only binary terminal input and freezes it during preparation', async () => {
  const { ws, sent, lifecycle, reply } = setup(); lifecycle.setInputEnabled(true);
  ws.send('{"handoff":true,"action":"release"}'); expect(ArrayBuffer.isView(sent.mock.calls.at(-1)![0])).toBe(true);
  const prepared = lifecycle.prepare(); const before = sent.mock.calls.length;
  ws.send('never sent'); expect(sent.mock.calls).toHaveLength(before);
  const control = reply({ ready: true, ticket: 'authority', stored_id: 'canonical' });
  expect(control.profile).toBe('work'); expect(control.generation).toBeTruthy();
  expect(await prepared).toEqual({ storedId: 'canonical' });
  const release = lifecycle.release(); reply({ released: false }); await expect(release).rejects.toThrow();
  expect(ws.close).not.toHaveBeenCalled();
});
it('cancel asks the real owner even when the prepare ACK and local ticket were lost', async () => {
  const { lifecycle, reply, sent } = setup();
  const cancelled = lifecycle.cancel();
  expect(JSON.parse(sent.mock.calls.at(-1)![0]).action).toBe('cancel');
  expect(lifecycle.inputEnabled).toBe(false);
  reply({ cancelled: true }); await cancelled; expect(lifecycle.inputEnabled).toBe(true);
});
it('failed target cleanup must receive a registry release ACK before closing', async () => {
  const { lifecycle, reply, ws } = setup();
  const disposed = lifecycle.dispose(); expect(ws.close).not.toHaveBeenCalled();
  expect(reply({ released: true }).action).toBe('abort'); await disposed;
  expect(ws.close).toHaveBeenCalledTimes(1);
});

import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { NativeSession } from './native-session';
import { FakeNativeSocket, flushNative } from './fake-websocket.test-support';
import type { HistoryPage } from './native-history';

const history = vi.hoisted(() => vi.fn());
vi.mock('./native-history', () => ({ readHistory: history }));
vi.mock('@/lib/api', () => ({ buildWsUrl: async () => 'ws://localhost/api/ws', authedFetch: vi.fn() }));
vi.mock('@/lib/dashboard-auth-reload', () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let session: NativeSession;
const page = (messages: Record<string, unknown>[], limit = 100): HistoryPage => ({ session_id: 'stored', messages, pagination: { returned: messages.length, limit, offset: 0, order: 'latest' } });
beforeEach(() => { FakeNativeSocket.reset(); history.mockReset(); vi.stubGlobal('WebSocket', FakeNativeSocket); session = new NativeSession('work', 'stored'); });
afterEach(() => { session.stop(); vi.unstubAllGlobals(); });
it('buffers tool completion while REST hydrates and produces one card for a real tool ID', async () => {
  let finish!: (page: HistoryPage) => void;
  history.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  session.start(); await flushNative();
  const socket = FakeNativeSocket.instances[0];
  socket.event('tool.complete', { tool_id: 'call', name: 'read_file', result: { text: 'result' } });
  socket.event('tool.start', { tool_id: 'call', name: 'read_file' });
  finish(page([{ id: 1, role: 'user', content: 'Read' }, { id: 2, role: 'assistant', content: '', tool_calls: [{ id: 'call', function: { name: 'read_file', arguments: '{}' } }] }, { id: 3, role: 'tool', tool_call_id: 'call', content: '{"text":"result"}' }]));
  await flushNative();
  expect(session.getSnapshot().ready).toBe(true);
  const tools = session.getSnapshot().conversation.messages.flatMap(m => m.parts).filter(p => p.type === 'tool');
  expect(tools).toHaveLength(1); expect(tools[0].result).toEqual({ text: 'result' });
  expect(FakeNativeSocket.requests.find(r => r.method === 'session.resume')?.params.omit_messages).toBe(true);
});
it('discards a prior generation history response after switching sessions', async () => {
  let stale!: (page: HistoryPage) => void;
  history.mockImplementationOnce(() => new Promise(resolve => { stale = resolve; })).mockResolvedValue(page([{ id: 9, role: 'user', content: 'Current' }]));
  session.start(); await flushNative();
  session.select('other'); await flushNative();
  stale(page([{ id: 1, session_id: 'ancestor', role: 'user', content: 'STALE ancestor' }, { id: 2, session_id: 'tip', role: 'assistant', content: 'STALE tip' }])); await flushNative();
  expect(JSON.stringify(session.getSnapshot().conversation)).not.toContain('STALE');
  expect(JSON.stringify(session.getSnapshot().conversation)).toContain('Current');
});
it('pages before the oldest row and completes calls crossing the page boundary', async () => {
  history.mockResolvedValueOnce(page([{ id: 3, role: 'tool', tool_call_id: 'call', tool_name: 'read_file', content: 'done' }], 1)).mockResolvedValueOnce(page([{ id: 1, role: 'user', content: 'Read' }, { id: 2, role: 'assistant', content: '', tool_calls: [{ id: 'call', function: { name: 'read_file', arguments: '{"path":"a"}' } }] }]));
  session.start(); await flushNative(); await session.loadOlder();
  expect(history.mock.calls[1].slice(0, 3)).toEqual(['work', 'stored', 3]);
  const tools = session.getSnapshot().conversation.messages.flatMap(m => m.parts).filter(p => p.type === 'tool');
  expect(tools).toHaveLength(1); expect(tools[0].args).toEqual({ path: 'a' }); expect(tools[0].result).toBe('done');
});

const turn = (id: number, text: string) => [
  { id, role: 'user', content: text, display_metadata: { turn_id: `turn-${id}` } },
  { id: id + 1, role: 'assistant', content: `Answer ${text}`, display_metadata: { turn_id: `turn-${id}` } },
];
const visibleText = () => session.getSnapshot().conversation.messages.flatMap(m => m.parts.map(p => p.text));

it.each([false, true])('preserves history on failed reconnect and retries latest before paging (rotation=%s)', async rotate => {
  let stored = 'stored';
  let inflight = false;
  FakeNativeSocket.responder = (request, socket) => socket.reply(request, {
    session_id: 'runtime', session_key: stored, running: inflight, messages: [],
    ...(inflight ? { inflight: { turn_id: 'turn-current', user: 'Current', assistant: 'Partial', streaming: true } } : {}),
  });
  history.mockResolvedValueOnce(page(turn(10, 'Old'), 2));
  session.start(); await flushNative();
  expect(visibleText()).toContain('Old');
  stored = rotate ? 'compressed' : 'stored'; inflight = true;
  history.mockRejectedValueOnce(new Error('temporary REST failure'));
  FakeNativeSocket.instances[0].close(1006); session.retry(); await flushNative();
  expect(visibleText()).toContain('Old');
  expect(visibleText()).toContain('Partial');
  expect(session.getSnapshot().conversation.running).toBe(true);
  expect(session.getSnapshot().control.notice).toMatch(/history.*unavailable/i);
  expect(FakeNativeSocket.requests.filter(r => r.method === 'prompt.submit')).toHaveLength(0);

  history.mockResolvedValueOnce({ ...page([...turn(10, 'Old'), ...turn(12, 'New')], 4), session_id: stored });
  await session.loadOlder();
  expect(history.mock.calls[2].slice(0, 3)).toEqual(['work', stored, undefined]);
  expect(visibleText().filter(t => t === 'New')).toHaveLength(1);
  expect(visibleText().filter(t => t === 'Old')).toHaveLength(1);
  expect(visibleText()).toContain('Partial');
  history.mockResolvedValueOnce({ ...page(turn(8, 'Earlier')), session_id: stored });
  await session.loadOlder();
  expect(history.mock.calls[3].slice(0, 3)).toEqual(['work', stored, 10]);
  expect(visibleText()).toContain('Earlier');
  expect(FakeNativeSocket.requests.filter(r => r.method === 'prompt.submit')).toHaveLength(0);
});

it('aborts and ignores an older-page response after selecting a different stored session', async () => {
  let finish!: (value: HistoryPage) => void;
  history.mockResolvedValueOnce(page(turn(10, 'A'), 2))
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ ...page(turn(50, 'B')), session_id: 'other' });
  FakeNativeSocket.responder = (request, socket) => socket.reply(request, {
    session_id: request.params.session_id === 'other' ? 'runtime-b' : 'runtime',
    session_key: request.params.session_id, running: false,
  });
  session.start(); await flushNative();
  const pending = session.loadOlder(); await flushNative();
  const signal = history.mock.calls[1][3] as AbortSignal;
  session.select('other'); await flushNative();
  finish(page(turn(2, 'STALE'))); await pending;
  expect(signal?.aborted).toBe(true);
  expect(visibleText()).toEqual(['B', 'Answer B']);
});

it('coalesces simultaneous older-page loads', async () => {
  let finish!: (value: HistoryPage) => void;
  history.mockResolvedValueOnce(page(turn(10, 'Old'), 2))
    .mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  session.start(); await flushNative();
  const first = session.loadOlder(); const second = session.loadOlder(); await flushNative();
  expect(history).toHaveBeenCalledTimes(2);
  finish(page(turn(8, 'Earlier'))); await Promise.all([first, second]);
  expect(visibleText().filter(t => t === 'Earlier')).toHaveLength(1);
});

it('rebuilds the retry cursor from latest when disconnected turns exceed a page', async () => {
  FakeNativeSocket.responder = (request, socket) => socket.reply(request, { session_id: 'runtime', session_key: 'stored', running: false, messages: [] });
  history.mockResolvedValueOnce(page(turn(10, 'Old'), 2)).mockRejectedValueOnce(new Error('unavailable'));
  session.start(); await flushNative();
  FakeNativeSocket.instances[0].close(1006); session.retry(); await flushNative();
  expect(visibleText()).toContain('Old');
  history.mockResolvedValueOnce(page(turn(100, 'Latest'), 2));
  await session.loadOlder();
  history.mockResolvedValueOnce(page(turn(98, 'Gap')));
  await session.loadOlder();
  expect(history.mock.calls.at(-1)?.slice(0, 3)).toEqual(['work', 'stored', 100]);
  expect(visibleText()).toContain('Gap');
});

it('fetches latest when an older-page response reports a rotated stored identity', async () => {
  history.mockResolvedValueOnce(page(turn(10, 'Old'), 2))
    .mockResolvedValueOnce({ ...page(turn(8, 'Partial ancestor')), session_id: 'rotated' })
    .mockResolvedValueOnce({ ...page(turn(100, 'Current')), session_id: 'rotated' });
  session.start(); await flushNative();
  await session.loadOlder();
  expect(history.mock.calls[2]?.slice(0, 3)).toEqual(['work', 'rotated', undefined]);
  expect(visibleText()).toContain('Current');
});

it('retains a hydrated compression lineage on reconnect failure and rebuilds it once on latest retry', async () => {
  const lineage = [...turn(1, 'Ancestor A').map(row => ({ ...row, session_id: 'A' })),
    ...turn(3, 'Ancestor B').map(row => ({ ...row, session_id: 'B' })),
    ...turn(5, 'Tip C').map(row => ({ ...row, session_id: 'C' }))];
  FakeNativeSocket.responder = (request, socket) => socket.reply(request, { session_id: 'runtime', session_key: 'C', running: false, messages: [] });
  history.mockResolvedValueOnce({ ...page(lineage), session_id: 'C' }).mockRejectedValueOnce(new Error('503'));
  session.start(); await flushNative();
  FakeNativeSocket.instances[0].close(1006); session.retry(); await flushNative();
  for (const text of ['Ancestor A', 'Ancestor B', 'Tip C']) expect(visibleText().filter(t => t === text)).toHaveLength(1);
  history.mockResolvedValueOnce({ ...page(lineage), session_id: 'C' });
  await session.loadOlder();
  expect(history.mock.calls.at(-1)?.slice(0, 3)).toEqual(['work', 'C', undefined]);
  for (const text of ['Ancestor A', 'Ancestor B', 'Tip C']) expect(visibleText().filter(t => t === text)).toHaveLength(1);
  expect(FakeNativeSocket.requests.filter(r => r.method === 'prompt.submit')).toHaveLength(0);
});

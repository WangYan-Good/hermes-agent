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
  stale(page([{ id: 1, role: 'user', content: 'STALE' }])); await flushNative();
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

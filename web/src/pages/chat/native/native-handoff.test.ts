import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ChatSwitch } from '../chat-switch';
import { NativeSession } from './native-session';
import { FakeNativeSocket, flushNative } from './fake-websocket.test-support';
vi.mock('@/lib/api', () => ({ authedFetch: vi.fn(), fetchJSON: vi.fn().mockResolvedValue({ session_id: 'stored', messages: [], pagination: { returned: 0, limit: 100 } }), buildWsUrl: vi.fn(async () => 'ws://localhost/api/ws') }));
vi.mock('@/lib/dashboard-auth-reload', () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let session: NativeSession;
let machine: ChatSwitch;
let busy = false;
const requests = (method: string) => FakeNativeSocket.requests.filter(r => r.method === method);
beforeEach(async () => {
  vi.useFakeTimers(); FakeNativeSocket.reset(); busy = false; vi.stubGlobal('WebSocket', FakeNativeSocket);
  FakeNativeSocket.responder = (request, socket) => {
    if (request.method === 'session.handoff') return socket.reply(request, { ready: !busy, ticket: 'authority', stored_id: 'stored', released: request.params.action === 'release' });
    if (request.method === 'session.activate' || request.method === 'session.resume') return socket.reply(request, { session_id: 'runtime', stored_session_id: 'stored', running: busy, messages: [] });
    FakeNativeSocket.defaultResponse(request, socket);
  };
  session = new NativeSession('profile-a', null); machine = new ChatSwitch('native', null); machine.register(session.lifecycle, 0); session.start(); await flushNative();
});
afterEach(() => { machine.dispose(); session.stop(); vi.useRealTimers(); vi.unstubAllGlobals(); });
it('streaming and disconnect at the switch boundary recover without replay', async () => {
  busy = true; await session.submit('once'); machine.request('terminal');
  expect(machine.getSnapshot().phase).toBe('waiting-for-idle'); expect(requests('session.handoff')).toHaveLength(0);
  FakeNativeSocket.instances[0].close(1006); await vi.advanceTimersByTimeAsync(1000); await flushNative();
  expect(machine.getSnapshot().mounted).toBe('native'); expect(requests('prompt.submit')).toHaveLength(1);
  busy = false; FakeNativeSocket.instances.at(-1)!.event('message.complete', { text: 'done' }); await flushNative();
  expect(machine.getSnapshot().mounted).toBe('terminal'); expect(requests('prompt.submit')).toHaveLength(1);
  expect(FakeNativeSocket.instances.filter(s => s.readyState === 1)).toHaveLength(0);
});
it('lost submit ACK retains recovery authority until a live idle snapshot arrives', async () => {
  const base = FakeNativeSocket.responder;
  FakeNativeSocket.responder = (request, socket) => {
    if (request.method === 'prompt.submit') { busy = true; socket.close(1006); return; }
    base(request, socket);
  };
  const submitted = session.submit('unknown'); await flushNative(); machine.request('terminal');
  await submitted; await vi.advanceTimersByTimeAsync(1000); await flushNative();
  expect(requests('prompt.submit')).toHaveLength(1); expect(machine.getSnapshot().mounted).toBe('native');
  busy = false; FakeNativeSocket.instances.at(-1)!.event('session.handoff_status'); await flushNative();
  expect(machine.getSnapshot().mounted).toBe('terminal'); expect(requests('prompt.submit')).toHaveLength(1);
});
it.each(['approval', 'sudo'])('a pending %s keeps the only responder alive', async kind => {
  FakeNativeSocket.instances[0].event(`${kind}.request`, { request_id: 'request', command: 'test', choices: ['once', 'deny'] });
  machine.request('terminal'); await flushNative();
  expect(machine.getSnapshot().mounted).toBe('native');
  expect(requests(`${kind}.respond`)).toHaveLength(0);
  expect(requests('session.handoff')).toHaveLength(0);
});
it('draft text must be explicitly discarded and is never submitted by switching', async () => {
  session.setDraft('unsent'); machine.request('terminal'); await flushNative();
  expect(machine.getSnapshot().draft).toBe(true); expect(session.draftText).toBe('unsent');
  await machine.discard(); await flushNative();
  expect(machine.getSnapshot().mounted).toBe('terminal'); expect(requests('prompt.submit')).toHaveLength(0);
});

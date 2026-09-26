// @vitest-environment jsdom
import { StrictMode, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, useLocation, useNavigate } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ChatPage from './ChatPage';
import { FakeNativeSocket, flushNative } from './chat/native/fake-websocket.test-support';

const profile = vi.hoisted(() => ({ profile: '' }));
vi.mock('@/contexts/useProfileScope', () => ({ useProfileScope: () => profile }));
vi.mock('@/lib/api', () => ({ authedFetch: vi.fn(), fetchJSON: vi.fn().mockRejectedValue(new Error('history unavailable')), HERMES_BASE_PATH: '', buildWsUrl: vi.fn(async () => 'ws://localhost/api/ws?ticket=fresh') }));
vi.mock('@/lib/dashboard-auth-reload', () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let container: HTMLDivElement;
let root: Root;
function Harness() {
  const location = useLocation(); const navigate = useNavigate();
  return <><ChatPage isActive={location.pathname === '/chat'} />
    <button data-nav="away" onClick={() => navigate('/sessions?learn=hidden')} />
    <button data-nav="back" onClick={() => navigate('/chat')} />
    <button data-nav="learn" onClick={() => navigate('/chat?learn=debugging&chat_mode=terminal&other=keep#anchor')} />
    <button data-nav="history" onClick={() => navigate(-1)} />
    <output>{location.pathname}{location.search}{location.hash}</output></>;
}
beforeEach(() => {
  const values = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: vi.fn((key: string) => values.delete(key)),
    clear: () => values.clear(),
  });
  profile.profile = ''; FakeNativeSocket.reset(); localStorage.clear(); sessionStorage.clear();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('WebSocket', FakeNativeSocket);
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollTo = vi.fn();
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function render(path = '/chat') {
  await act(async () => { root.render(<StrictMode><MemoryRouter initialEntries={[path]}><Harness /></MemoryRouter></StrictMode>); await flushNative(); });
}
async function click(selector: string) { await act(async () => { (container.querySelector(selector) as HTMLElement).click(); await flushNative(); }); }
async function draft(text: string) {
  await act(async () => {
    const input = container.querySelector('textarea')!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, text);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
function expectNativeOnly() {
  expect(container.querySelector('[aria-label="Native Chat"]')).not.toBeNull();
  expect(container.querySelector('.xterm')).toBeNull();
  expect(container.textContent).not.toContain('Chat Interface');
  expect(FakeNativeSocket.instances).toHaveLength(1);
  expect(FakeNativeSocket.instances[0].url).toContain('/api/ws');
  expect(FakeNativeSocket.requests.filter(r => r.method === 'prompt.submit')).toHaveLength(0);
}
it.each(['terminal', 'native', 'garbage'])('ignores obsolete browser and URL value %s and cleans only the obsolete key', async value => {
  localStorage.setItem('hermes.dashboard.chat.mode', value); localStorage.setItem('unrelated', 'keep');
  const remove = vi.spyOn(localStorage, 'removeItem');
  await render(`/chat?chat_mode=${value}&chat_mode=terminal&other=keep#anchor`);
  expectNativeOnly();
  expect(localStorage.getItem('hermes.dashboard.chat.mode')).toBeNull();
  expect(localStorage.getItem('unrelated')).toBe('keep');
  expect(remove.mock.calls.filter(([key]) => key === 'hermes.dashboard.chat.mode')).toHaveLength(1);
  expect(container.querySelector('output')?.textContent).toBe('/chat?other=keep#anchor');
  await click('[data-nav="away"]'); await click('[data-nav="back"]');
  expectNativeOnly();
  expect(remove.mock.calls.filter(([key]) => key === 'hermes.dashboard.chat.mode')).toHaveLength(1);
});
it('starts without reading profile preferences and tolerates blocked localStorage', async () => {
  vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
  await render(); expectNativeOnly();
});
it('tolerates a failed obsolete-key removal', async () => {
  vi.spyOn(localStorage, 'removeItem').mockImplementation(() => { throw new Error('blocked'); });
  await render('/chat?chat_mode=terminal'); expectNativeOnly();
});
it('does not clean the key or URL when initialization fails', async () => {
  localStorage.setItem('hermes.dashboard.chat.mode', 'terminal');
  FakeNativeSocket.responder = (_request, socket) => socket.close(4401);
  await render('/chat?chat_mode=terminal');
  expect(localStorage.getItem('hermes.dashboard.chat.mode')).toBe('terminal');
  expect(container.querySelector('output')?.textContent).toContain('chat_mode=terminal');
  expect(FakeNativeSocket.requests.some(r => r.method === 'prompt.submit')).toBe(false);
});
it('preserves durable resume and seeds learn as an unsent draft', async () => {
  await render('/chat?resume=saved&learn=debugging&chat_mode=terminal&other=keep#anchor');
  expectNativeOnly();
  expect(FakeNativeSocket.requests[0]).toMatchObject({ method: 'session.resume', params: { session_id: 'saved', allow_auto_continue: false } });
  expect(container.querySelector('textarea')?.value).toBe('/learn debugging');
  expect(container.querySelector('output')?.textContent).toBe('/chat?resume=stored&other=keep#anchor');
  await click('[data-nav="away"]'); await click('[data-nav="back"]'); await click('[data-nav="history"]');
  expectNativeOnly(); expect(container.querySelector('textarea')?.value).toBe('/learn debugging');
});
it.each([true, false])('preserves an existing draft until an explicit learn decision (append=%s)', async append => {
  await render(); await draft('unsent'); await click('[data-nav="learn"]');
  expect(container.querySelector('textarea')?.value).toBe('unsent');
  expect(container.textContent).toContain('A learning request is waiting');
  const button = [...container.querySelectorAll('button')].find(b => b.textContent === (append ? 'Append to draft' : 'Ignore'))!;
  await act(async () => { button.click(); await flushNative(); });
  expect(container.querySelector('textarea')?.value).toBe(append ? 'unsent\n/learn debugging' : 'unsent');
  expect(container.querySelector('output')?.textContent).toBe('/chat?other=keep#anchor');
  expectNativeOnly();
});

it('isolates the Native host and draft across profile switches without resuming an old identity', async () => {
  await render(); await draft('old profile draft');
  const previous = FakeNativeSocket.instances[0];
  profile.profile = 'other';
  await render();
  expect(previous.readyState).toBe(3);
  expect(FakeNativeSocket.instances.filter(socket => socket.readyState === 1)).toHaveLength(1);
  expect(FakeNativeSocket.requests.filter(request => request.method === 'session.create').map(request => request.params.profile)).toEqual(['', 'other']);
  expect(FakeNativeSocket.requests.filter(request => request.method === 'session.resume')).toHaveLength(0);
  expect(FakeNativeSocket.requests.filter(request => request.method === 'prompt.submit')).toHaveLength(0);
  expect(container.querySelector('textarea')?.value).toBe('');
});

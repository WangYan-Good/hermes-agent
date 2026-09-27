// @vitest-environment jsdom
import { StrictMode, act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter, useLocation, useNavigate } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ChatPage from './ChatPage';
import './chat/native/NativeChatPage';
import { $browserMode, $profileModes, requestBrowserMode } from './chat/chat-preferences';
import { FakeNativeSocket, flushNative } from './chat/native/fake-websocket.test-support';

const profile = vi.hoisted(() => ({ profile: '' }));
vi.mock('@/contexts/useProfileScope', () => ({ useProfileScope: () => profile }));
vi.mock('@/lib/api', () => ({ api: { getConfig: vi.fn().mockResolvedValue({ dashboard: { chat: { default_mode: 'native' } } }) }, authedFetch: vi.fn(), fetchJSON: vi.fn().mockRejectedValue(new Error('history unavailable')), HERMES_BASE_PATH: '', buildWsUrl: vi.fn(async () => 'ws://localhost/api/ws?ticket=fresh') }));
vi.mock('@/lib/dashboard-auth-reload', () => ({ clearDashboardTokenReloadAttempt: vi.fn(), maybeReloadForLoopbackWsAuthFailure: vi.fn() }));
let container: HTMLDivElement;
let root: Root;
function Harness() {
  const location = useLocation(); const navigate = useNavigate();
  return <><ChatPage isActive={location.pathname === '/chat'} />
    <button data-nav="away" onClick={() => navigate('/sessions?learn=hidden')} />
    <button data-nav="back" onClick={() => navigate('/chat')} />
    <button data-nav="learn" onClick={() => navigate('/chat?learn=debugging&chat_mode=native&other=keep#anchor')} />
    <button data-nav="terminal" onClick={() => navigate('/chat?resume=stored&chat_mode=terminal&other=keep#anchor')} />
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
  $browserMode.set(null); $profileModes.set({});
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
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)); await flushNative(); });
}
async function click(selector: string) { await act(async () => { (container.querySelector(selector) as HTMLElement).click(); await flushNative(); }); await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); await flushNative(); }); }
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
  expect(container.textContent).toContain('Chat Interface');
  expect(FakeNativeSocket.instances).toHaveLength(1);
  expect(FakeNativeSocket.instances[0].url).toContain('/api/ws');
  expect(FakeNativeSocket.requests.filter(r => r.method === 'prompt.submit')).toHaveLength(0);
}
it('defaults to Native and exposes the live selector without replay', async () => {
  await render(); expectNativeOnly();
});
it('ignores unknown mode values without deleting browser preferences', async () => {
  localStorage.setItem('hermes.dashboard.chat.mode', 'unknown');
  await render('/chat?chat_mode=unknown&other=keep#anchor'); expectNativeOnly();
  expect(localStorage.getItem('hermes.dashboard.chat.mode')).toBe('unknown');
  expect(container.querySelector('output')?.textContent).toBe('/chat?other=keep#anchor');
});
it('works when localStorage access is blocked', async () => {
  vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
  await render(); expectNativeOnly();
});
it('preserves durable resume and seeds learn as an unsent draft', async () => {
  await render('/chat?resume=stored&learn=debugging&chat_mode=native&other=keep#anchor');
  expectNativeOnly();
  expect(FakeNativeSocket.requests[0]).toMatchObject({ method: 'session.resume', params: { session_id: 'stored', allow_auto_continue: false } });
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
  await act(async () => { button.click(); await new Promise(resolve => setTimeout(resolve, 20)); await flushNative(); });
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

vi.mock('./chat/TerminalChatPage', () => ({ default: function TerminalStub({ onSurface }: { onSurface?: (value: import('./chat/chat-surface-lifecycle').ChatSurfaceLifecycle | null) => void }) {
  useEffect(() => {
    const status = async () => ({ ready: true, blocked: [], storedId: 'stored' });
    onSurface?.({ status, prepare: status, cancel: async () => {}, release: async () => {}, discard: async () => {}, dispose: async () => {}, detach: () => {}, setInput: () => {}, subscribe: () => () => {} });
    return () => onSurface?.(null);
  }, [onSurface]);
  return <section aria-label="Terminal Chat" />;
} }));
it('URL Terminal mounts only Terminal and opens no Native session', async () => {
  await render('/chat?chat_mode=terminal');
  expect(container.querySelector('[aria-label="Terminal Chat"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="Native Chat"]')).toBeNull();
  expect(FakeNativeSocket.requests.filter(r => ['session.create', 'session.resume', 'prompt.submit'].includes(r.method))).toHaveLength(0);
});

it('re-evaluates an unconsumed URL after hidden completion, preference change and browser Back', async () => {
  await render('/chat?resume=stored'); await draft('blocks URL switching'); await click('[data-nav="terminal"]');
  expect(container.querySelector('[aria-label="Native Chat"]')).not.toBeNull();
  await click('[data-nav="away"]');
  const discard = [...container.querySelectorAll('button')].find(b => b.textContent === 'Discard draft and switch')!;
  await act(async () => { discard.click(); await flushNative(); });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 550)); await flushNative(); });
  expect(container.querySelector('[aria-label="Terminal Chat"]')).not.toBeNull();
  await act(async () => { await requestBrowserMode('', 'native'); await flushNative(); });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 550)); await flushNative(); });
  expect(container.querySelector('[aria-label="Native Chat"]')).not.toBeNull();
  await click('[data-nav="history"]');
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 550)); await flushNative(); });
  expect(container.querySelector('[aria-label="Terminal Chat"]')).not.toBeNull();
  expect(container.querySelector('output')?.textContent).toBe('/chat?resume=stored&other=keep#anchor');
  expect(FakeNativeSocket.requests.filter(r => r.method === 'prompt.submit')).toHaveLength(0);
});

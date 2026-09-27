/* Production Dashboard + real WS/PTY fixture; no user prompts are submitted.
 * Run against tests/e2e/fixtures/chat_switching_server.py after web/TUI builds.
 * The loopback fixture retains its auth model; Chromium explicitly models the
 * supported LAN HTTP capabilities before any application module executes.
 */
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const base = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const evidence = async () => (await fetch(`${base}/c2-review-evidence`)).json();
async function waitFor(read, accept, label) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    const value = await read();
    if (accept(value)) return value;
    await delay(100);
  }
  throw new Error(`Timeout: ${label}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHAT_E2E_BROWSER || '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage(); page.setDefaultTimeout(60000);
  const errors = [], sockets = []; let submissions = 0, doubleOwner = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('websocket', socket => {
    const url = new URL(socket.url());
    const record = { path: url.pathname, generation: url.searchParams.get('generation'), closed: false };
    sockets.push(record); socket.on('close', () => { record.closed = true; });
    socket.on('framesent', ({ payload }) => {
      try { if (JSON.parse(String(payload)).method === 'prompt.submit') submissions++; } catch { /* Binary PTY frames. */ }
    });
  });
  const selector = () => page.getByLabel('Chat Interface', { exact: true });
  async function ready(mode) {
    await page.waitForFunction(expected => {
      const select = document.querySelector('select[aria-label="Chat Interface"]');
      return select && !select.disabled && select.value === expected;
    }, mode);
    assert.equal(await page.locator(`[aria-label="${mode === 'native' ? 'Native' : 'Terminal'} Chat"]`).count(), 1);
    assert.equal(await page.locator('[aria-label="Native Chat"], [aria-label="Terminal Chat"]').count(), 1);
    const state = await evidence();
    if (state.live.length > 1) doubleOwner++;
    assert.equal(state.live.length, 1);
    assert(!/Interface failed to load|The chat interface could not load/.test(await page.locator('body').innerText()));
    return state;
  }
  try {
    await fetch(`${base}/c2-reset`, { method: 'POST' });
    const initial = await evidence();
    await page.addInitScript(() => {
      Object.defineProperty(globalThis.crypto, 'randomUUID', { configurable: true, value: undefined });
      Object.defineProperty(globalThis, 'isSecureContext', { configurable: true, value: false });
      window.insecureHttpMaxSurfaces = 0;
      new MutationObserver(() => {
        window.insecureHttpMaxSurfaces = Math.max(window.insecureHttpMaxSurfaces, document.querySelectorAll('[aria-label="Native Chat"], [aria-label="Terminal Chat"]').length);
      }).observe(document, { childList: true, subtree: true });
    });
    await page.goto(`${base}/chat`);
    assert.deepEqual(await page.evaluate(() => ({ secure: isSecureContext, uuid: typeof crypto.randomUUID, entropy: typeof crypto.getRandomValues })), { secure: false, uuid: 'undefined', entropy: 'function' });
    await ready('native');
    assert(sockets.some(socket => socket.path === '/api/ws'));
    await page.locator('input[type=file]').setInputFiles({ name: 'http-uuid.txt', mimeType: 'text/plain', buffer: Buffer.from('unsent UUID regression') });
    await waitFor(evidence, state => state.attachment_states.includes('uploaded'), 'attachment uploaded without submission');
    await page.getByRole('button', { name: 'Remove http-uuid.txt', exact: true }).click();
    await waitFor(evidence, state => state.attachment_states.every(value => value === 'cancelled'), 'authoritative attachment cancellation');
    await selector().selectOption('terminal');
    const terminal = await ready('terminal');
    assert.equal(terminal.pty.length, 1);
    const stored = terminal.live[0].stored;
    assert(stored); assert.equal(terminal.live[0].terminal, true);
    assert.equal(sockets.filter(socket => socket.path === '/api/ws' && !socket.closed).length, 0);
    await fetch(`${base}/c1-drop-viewer`, { method: 'POST' });
    await waitFor(() => Promise.resolve(sockets), values => values.filter(socket => socket.path === '/api/pty').length === 2, 'Terminal reconnect');
    const reconnected = await waitFor(evidence, state => state.pty.length === 1 && state.pty[0].viewer && state.pty[0].owner, 'same-instance owner reconnect');
    assert.equal(reconnected.pty[0].instance, terminal.pty[0].instance);
    assert.equal(reconnected.pty[0].pid, terminal.pty[0].pid);
    // PTY reattach writes Ctrl-L and xterm answers terminal queries. Raw byte
    // counts include those protocol exchanges; assert no prompt replay below.
    const generations = sockets.filter(socket => socket.path === '/api/pty').map(socket => socket.generation);
    assert.equal(new Set(generations).size, 2);
    await ready('terminal');
    await selector().selectOption('native');
    const final = await ready('native');
    assert.equal(final.live[0].stored, stored); assert.equal(final.pty.length, 0);
    assert.equal(final.submissions.length - initial.submissions.length, 0);
    assert.equal(submissions, 0); assert.equal(doubleOwner, 0);
    assert.equal(await page.evaluate(() => window.insecureHttpMaxSurfaces), 1);
    assert.equal(await page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), 'native');
    assert.deepEqual(errors, []);
    if (process.env.CHAT_E2E_SCREENSHOT) await page.screenshot({ path: process.env.CHAT_E2E_SCREENSHOT });
    console.log(JSON.stringify({ mode: 'SIMULATED randomUUID-unavailable PRODUCTION BUILD', native: 'PASS', terminal: 'PASS', attachments: 'PASS', roundtrip: 'PASS', sameInstanceReconnect: 'PASS', automaticSubmissions: 0, doubleOwner, storedId: stored, errors }));
  } catch (error) {
    console.error('errors', errors);
    console.error('page', (await page.locator('body').innerText().catch(() => '')).slice(-4000));
    throw error;
  } finally {
    await browser.close();
    await fetch(`${base}/c2-reset`, { method: 'POST' });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

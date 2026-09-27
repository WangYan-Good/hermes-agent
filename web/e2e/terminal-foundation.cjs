/* Real production auth + PTY + Ink + gateway + Agent + SessionDB. */
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const base = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const frontend = process.env.CHAT_TERMINAL_E2E_URL || 'http://127.0.0.1:5173';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(read, accept, label) {
  const end = Date.now() + 90000;
  while (Date.now() < end) {
    try {
      const value = await read();
      if (accept(value)) return value;
    } catch { /* Read-only readiness may race server startup. */ }
    await delay(200);
  }
  throw new Error(`Timed out: ${label}`);
}
const evidence = async () => (await fetch(`${base}/c1-evidence`)).json();
async function gateway() {
  const socket = new WebSocket(base.replace('http', 'ws') + '/api/ws?token=p7-local');
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  let next = 0;
  return {
    close: () => socket.close(),
    rpc: (method, params) => new Promise((resolve, reject) => {
      const id = String(++next);
      const listener = event => {
        for (const line of String(event.data).split('\n').filter(Boolean)) {
          const frame = JSON.parse(line);
          if (frame.id === id) { clearTimeout(timer); socket.removeEventListener('message', listener); resolve(frame); }
        }
      };
      const timer = setTimeout(() => { socket.removeEventListener('message', listener); reject(new Error('Gateway reply timeout')); }, 30000);
      socket.addEventListener('message', listener);
      socket.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    }),
  };
}
async function preInputReconnect() {
  const attach = crypto.randomUUID();
  const connect = async instance => {
    const generation = crypto.randomUUID();
    const query = new URLSearchParams({ token: 'p7-local', attach, generation });
    if (instance) query.set('instance', instance);
    const socket = new WebSocket(base.replace('http', 'ws') + '/api/pty?' + query, 'hermes.pty-control.v1');
    const attached = await new Promise((resolve, reject) => {
      socket.addEventListener('error', reject, { once: true });
      socket.addEventListener('message', event => {
        if (typeof event.data === 'string') {
          const frame = JSON.parse(event.data);
          if (frame.type === 'attached') resolve(frame);
        }
      });
    });
    return { socket, attached, generation };
  };
  const first = await connect();
  const initial = await waitFor(evidence, e => e.pty.length === 1 && e.pty[0].owner, 'pre-input owner');
  assert.equal(initial.pty[0].accepted, false);
  const detached = new Promise(resolve => first.socket.addEventListener('close', resolve, { once: true }));
  await fetch(`${base}/c1-drop-viewer`, { method: 'POST' });
  await detached;
  await waitFor(evidence, e => e.pty.length === 1 && !e.pty[0].viewer, 'pre-input retention');
  const second = await connect(first.attached.instance);
  const reattached = await evidence();
  assert.equal(second.attached.instance, first.attached.instance);
  assert.equal(reattached.pty[0].pid, initial.pty[0].pid);
  assert.equal(reattached.pty[0].accepted, false);
  assert.equal(reattached.submissions.length, 0);
  const aborted = new Promise(resolve => second.socket.addEventListener('message', event => {
    if (typeof event.data === 'string') {
      const frame = JSON.parse(event.data);
      if (frame.id === 'pre-input-abort') resolve(frame);
    }
  }));
  second.socket.send(JSON.stringify({ type: 'control', id: 'pre-input-abort', action: 'abort',
    instance: second.attached.instance, generation: second.generation }));
  assert.equal((await aborted).result.released, true);
  await waitFor(evidence, e => e.pty.length === 0 && e.live.length === 0, 'pre-input abort cleanup');
  assert.equal((await (await fetch(`${base}/c1-process/${initial.pty[0].pid}`)).json()).alive, false);
  console.log('PASS acknowledged zero-input reconnect: same instance/process, no prompt, explicit abort cleanup');
}
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHAT_E2E_BROWSER || '/usr/bin/chromium', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  page.setDefaultTimeout(90000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await waitFor(evidence, e => Array.isArray(e.pty), 'Dashboard readiness');
    await preInputReconnect();
    await page.goto(`${frontend}/e2e/terminal-foundation.html`);
    await page.waitForFunction(() => window.terminal);
    const initial = await waitFor(evidence, e => e.pty.length === 1 && e.pty[0].owner && e.live.length > 0, 'real owner');
    const pid = initial.pty[0].pid;
    const agent = await gateway();
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), s => s.ready, 'authoritative readiness');
    // Harmless real terminal tool call, deterministic only at provider HTTP.
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.type('P7-TUI P7-LONG', { delay: 150 });
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), s => s.blocked?.includes('composer'), 'typed composer');
    await delay(500);
    await page.keyboard.press('Enter');
    const durable = await waitFor(evidence, e => e.submissions.length === 1 && e.durable.some(s => s.message_count >= 3) && e.live.every(s => !s.running), 'durable tool turn');
    assert.equal(durable.submissions[0].automatic, false);
    const denied = await agent.rpc('session.resume', { session_id: durable.durable[0].id, allow_auto_continue: false });
    assert.equal(denied.error?.code, 4030, 'Native cannot take a live Terminal owner');
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), s => s.ready, 'turn settled');
    await fetch(`${base}/c1-drop-viewer`, { method: 'POST' });
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), status => status.ready, 'reattached viewer');
    assert.equal((await evidence()).pty[0].pid, pid, 'reconnect reuses the real process');
    const prepared = await page.evaluate(() => window.terminal.command('prepare'));
    assert(prepared.ready && prepared.ticket);
    assert.equal(await page.evaluate(() => window.terminal.input('MUST NOT SUBMIT\r')), false);
    await page.evaluate(() => window.terminal.command('cancel'));
    // Exercise raw Unicode display, resize and the xterm base renderer.
    const start = Date.now();
    await page.evaluate(() => window.terminal.input('中文 display check'));
    const blocked = await page.evaluate(() => window.terminal.command('prepare'));
    assert.equal(blocked.ready, false);
    assert(blocked.blocked.includes('composer'));
    await page.evaluate(() => window.terminal.command('cancel'));
    await page.keyboard.press('Control+u');
    await page.evaluate(() => window.terminal.input('\x15'));
    await page.screenshot({ path: '/tmp/ui-c1-terminal-unicode.png' });
    await page.setViewportSize({ width: 900, height: 650 });
    await page.mouse.wheel(0, -200);
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), s => s.ready, 'composer cleared');
    const ready = await page.evaluate(() => window.terminal.command('prepare'));
    const released = await page.evaluate(ticket => window.terminal.command('release', ticket), ready.ticket);
    assert.equal(released.released, true);
    const final = await waitFor(evidence, e => e.pty.length === 0 && e.live.length === 0, 'PTY and gateway cleanup');
    assert.equal((await (await fetch(`${base}/c1-process/${pid}`)).json()).alive, false);
    const resumed = await agent.rpc('session.resume', { session_id: released.stored_id, allow_auto_continue: false });
    assert(!resumed.error, JSON.stringify(resumed));
    assert(resumed.result.messages.length >= 3, 'Current resume reads Terminal durable history');
    assert(JSON.stringify(resumed.result.messages).includes('line 299: 中文 terminal output'), 'Long Unicode output reached the durable history');
    await agent.rpc('session.close', { session_id: resumed.result.session_id });
    agent.close();
    assert.equal((await evidence()).submissions.length, 1);
    assert.equal(final.submissions.length, 1);
    await page.goto(`${frontend}/e2e/terminal-foundation.html?profile=work`);
    await waitFor(evidence, e => e.pty.length === 1 && e.pty[0].profile === 'work' && e.pty[0].owner, 'profile owner');
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), s => s.ready, 'profile readiness');
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.type('profile turn', { delay: 150 });
    await delay(500); await page.keyboard.press('Enter');
    const profileState = await waitFor(evidence, e => e.profile_durable.some(s => s.message_count >= 2) && e.submissions.length === 2 && e.live.every(s => !s.running), 'profile durable turn');
    assert.equal(profileState.durable.length, final.durable.length, 'profile turn does not write the default DB');
    await waitFor(() => page.evaluate(() => window.terminal.command('status')), s => s.ready, 'profile idle');
    const profileReady = await page.evaluate(() => window.terminal.command('prepare'));
    assert((await page.evaluate(ticket => window.terminal.command('release', ticket), profileReady.ticket)).released);
    await waitFor(evidence, e => e.pty.length === 0 && e.live.length === 0, 'profile cleanup');
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log(JSON.stringify({ gate: 'real-pty', pid, stored: released.stored_id, submissions: 2, automatic: 0, profileIsolation: true, reconnect: true, nativeResume: true, pty: 0, live: 0, unicodeResizeMs: Date.now() - start, webgl: false }));
  } catch (error) {
    await page.screenshot({ path: '/tmp/ui-c1-terminal.png' }).catch(() => {});
    console.error('last status', await page.evaluate(() => window.terminal?.command('status')).catch(e => String(e)));
    throw error;
  } finally {
    await page.evaluate(() => window.unmount?.()).catch(() => {});
    await delay(1000);
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

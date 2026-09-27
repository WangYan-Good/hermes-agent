/* Real App + WS + PTY + Ink + Agent + SessionDB; deterministic provider only. */
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const base = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const frontend = process.env.CHAT_TERMINAL_E2E_URL || 'http://127.0.0.1:5173';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(read, accept, label) {
  const end = Date.now() + 90000;
  while (Date.now() < end) {
    const value = await read();
    if (accept(value)) return value;
    await delay(200);
  }
  throw new Error(`Timed out: ${label}`);
}
const evidence = async () => (await fetch(`${base}/c1-evidence`)).json();
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  const page = await browser.newPage(); page.setDefaultTimeout(90000);
  const sockets = [], errors = []; let nativeSubmits = 0;
  page.on('pageerror', e => errors.push(e.message));
  page.on('websocket', ws => {
    const record = { path: new URL(ws.url()).pathname, closed: false }; sockets.push(record);
    ws.on('close', () => { record.closed = true; });
    ws.on('framesent', frame => { try { if (JSON.parse(String(frame.payload)).method === 'prompt.submit') nativeSubmits++; } catch {} });
  });
  try {
    await page.goto(`${frontend}/chat`);
    await page.getByLabel('Message Hermes', { exact: true }).waitFor();
    await waitFor(() => page.getByLabel('Chat Interface', { exact: true }).isEnabled(), Boolean, 'Native ready');
    await page.getByLabel('Message Hermes', { exact: true }).fill('C2 Native explicit turn');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    const first = await waitFor(evidence, e => e.submissions.length === 1 && e.durable.some(s => s.message_count >= 2) && e.live.every(s => !s.running), 'Native durable turn');
    const stored = first.durable[0].id;
    await page.getByLabel('Chat Interface', { exact: true }).selectOption('terminal');
    await page.locator('.xterm-helper-textarea').waitFor();
    await waitFor(() => page.getByLabel('Chat Interface', { exact: true }).isEnabled(), Boolean, 'Terminal ready');
    let current = await evidence();
    assert.equal(current.pty.length, 1); assert.equal(current.live.length, 1); assert.equal(current.live[0].stored, stored); assert(current.live[0].terminal);
    assert.equal(sockets.filter(s => s.path === '/api/ws' && !s.closed).length, 0);
    await page.locator('.xterm-helper-textarea').focus();
    await page.keyboard.type('C2 Terminal explicit turn', { delay: 100 }); await delay(500); await page.keyboard.press('Enter');
    await waitFor(evidence, e => e.submissions.length === 2 && e.live.every(s => !s.running), 'Terminal turn');
    await delay(600);
    await page.getByLabel('Chat Interface', { exact: true }).selectOption('native');
    await page.getByLabel('Message Hermes', { exact: true }).waitFor();
    await waitFor(() => page.getByLabel('Chat Interface', { exact: true }).isEnabled(), Boolean, 'Native resumed');
    current = await evidence(); assert.equal(current.pty.length, 0); assert.equal(current.live.length, 1); assert.equal(current.live[0].stored, stored);
    await page.getByLabel('Message Hermes', { exact: true }).fill('C2 Native final explicit turn');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    const final = await waitFor(evidence, e => e.submissions.length === 3 && e.live.every(s => !s.running), 'final turn');
    assert.equal(final.durable.length, 1); assert.equal(final.durable[0].id, stored);
    assert.equal(final.submissions.filter(s => s.automatic).length, 0); assert.equal(nativeSubmits, 2); assert.deepEqual(errors, []);
    console.log(JSON.stringify({ scenario: 'roundtrip', explicitUserSubmits: 3, browserPromptSubmits: nativeSubmits, backendPromptSubmits: final.submissions.length, automatic: 0, durableId: stored, ptyProcesses: final.pty.length, nativeAgentWS: sockets.filter(s => s.path === '/api/ws' && !s.closed).length }));
  } catch (error) {
    console.error('page', (await page.locator('body').innerText()).slice(-6000));
    console.error('evidence', await evidence()); console.error('errors', errors);
    await page.screenshot({ path: '/tmp/c2-switch-failure.png' }).catch(() => {});
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

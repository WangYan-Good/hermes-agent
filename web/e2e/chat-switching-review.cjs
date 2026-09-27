/* Production Dashboard review regressions; only provider HTTP is deterministic. */
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const base = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const evidence = async () => (await fetch(`${base}/c2-review-evidence`)).json();
async function waitFor(read, accept, label) {
  const until = Date.now() + 90000;
  while (Date.now() < until) { const result = await read(); if (accept(result)) return result; await delay(200); }
  throw new Error(`Timeout: ${label}`);
}
(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] });
  let page; const scenarios = []; let doubleOwner = 0;
  async function open(mode = 'native') {
    await page?.close(); await fetch(`${base}/c2-reset`, { method: 'POST' });
    page = await browser.newPage(); page.setDefaultTimeout(90000);
    await page.addInitScript(() => {
      const Original = window.WebSocket;
      window.c2DropRelease = null; window.c2Dropped = 0;
      window.WebSocket = class extends Original {
        send(data) {
          if (typeof data === 'string') {
            const frame = JSON.parse(data);
            const native = frame.method === 'native.presentation' && frame.params.action === 'release';
            const terminal = frame.type === 'control' && frame.action === 'release';
            if ((native && window.c2DropRelease === 'native') || (terminal && window.c2DropRelease === 'terminal')) {
              window.c2DropRelease = null; window.c2Dropped++; throw new Error('Injected release transport failure before send');
            }
          }
          super.send(data);
        }
      };
    });
    await page.goto(`${base}/chat?chat_mode=${mode}`); await ready(mode);
  }
  const selector = () => page.getByLabel('Chat Interface', { exact: true });
  async function ready(mode) {
    await page.waitForFunction(expected => {
      const select = document.querySelector('select[aria-label="Chat Interface"]');
      return select && !select.disabled && select.value === expected;
    }, mode);
    const count = await page.locator('[aria-label="Native Chat"], [aria-label="Terminal Chat"]').count();
    if (count > 1) doubleOwner++; assert.equal(count, 1);
  }
  async function sendNative(text) { await page.getByLabel('Message Hermes', { exact: true }).fill(text); await page.getByRole('button', { name: 'Send', exact: true }).click(); }
  async function sendTerminal(text) { await page.locator('.xterm-helper-textarea').focus(); await page.keyboard.type(text, { delay: 70 }); await delay(400); await page.keyboard.press('Enter'); }
  async function settled(count) { return waitFor(evidence, e => e.submissions.length === count && e.live.every(s => !s.running), 'explicit turn settled'); }
  async function profileWork() {
    await page.locator('#hermes-profile-switcher').click();
    await page.getByRole('option', { name: 'work', exact: true }).click();
  }
  try {
    await open(); let start = await evidence();
    await page.evaluate(() => { window.c2DropRelease = 'native'; });
    await selector().selectOption('terminal'); await page.getByRole('button', { name: 'Return to previous interface', exact: true }).waitFor();
    assert.equal((await evidence()).pty.length, 0);
    await page.getByRole('button', { name: 'Cancel switch', exact: true }).click(); await ready('native');
    await sendNative('Review Native explicit after cancel'); await settled(start.submissions.length + 1);
    // Attachments remain unsent and require real cancellation before switching.
    await page.locator('input[type=file]').setInputFiles({ name: 'review.txt', mimeType: 'text/plain', buffer: Buffer.from('unsent attachment') });
    await waitFor(evidence, e => e.attachment_states.some(s => !['cancelled', 'submitted'].includes(s)), 'attachment prepared');
    await selector().selectOption('terminal');
    await page.getByRole('button', { name: 'Discard draft and switch', exact: true }).click(); await ready('terminal');
    assert((await evidence()).attachment_states.every(s => ['cancelled', 'submitted'].includes(s)));
    assert.equal(await page.evaluate(() => window.c2Dropped), 1);
    scenarios.push('Native release not accepted; cancel ACK; explicit submit; authoritative attachment discard; second switch');
    await sendTerminal('Review Terminal before failed release'); await settled(start.submissions.length + 2);
    await page.evaluate(() => { window.c2DropRelease = 'terminal'; });
    await selector().selectOption('native'); await page.getByRole('button', { name: 'Return to previous interface', exact: true }).waitFor();
    assert.equal((await evidence()).live.filter(s => !s.terminal).length, 0);
    await page.getByRole('button', { name: 'Cancel switch', exact: true }).click(); await ready('terminal');
    await sendTerminal('Review Terminal explicit after cancel'); await settled(start.submissions.length + 3);
    await selector().selectOption('native'); await ready('native');
    assert.equal(await page.evaluate(() => window.c2Dropped), 2);
    scenarios.push('Terminal unaccepted release; unavailable receipt; cancel ACK; explicit input; second prepare and release');

    await open('terminal'); start = await evidence();
    await sendTerminal('P7-BUSY Review busy unmount');
    const busy = await waitFor(evidence, e => e.live.some(s => s.running), 'Terminal busy');
    const old = busy.viewers[0];
    await profileWork();
    const detached = await waitFor(evidence, e => e.viewers.some(v => v.instance === old.instance && !v.viewer && v.detached_at !== null), 'old viewer detached');
    assert.equal(detached.viewers.find(v => v.instance === old.instance).pid, old.pid);
    await delay(2500);
    assert.equal((await evidence()).viewers.find(v => v.instance === old.instance)?.viewer, false);
    assert.equal((await evidence()).submissions.length - start.submissions.length, 1);
    scenarios.push('Busy Terminal profile unmount detaches old viewer, preserves process for authoritative retention, no reconnect');

    await open(); start = await evidence();
    await sendNative('P7-BUSY Review Native orphan');
    const nativeBusy = await waitFor(evidence, e => e.live.some(s => s.running), 'Native busy');
    const authority = nativeBusy.authorities.find(a => !a.closed && !a.released && !a.completed);
    assert(authority); await profileWork();
    const retired = await waitFor(evidence, e => e.authorities.some(a => a.generation === authority.generation && a.completed > 0), 'orphan finalization retires authority');
    assert.equal(retired.authorities.find(a => a.generation === authority.generation).released, false);
    assert.equal(retired.submissions.length - start.submissions.length, 1);
    scenarios.push('Busy Native profile unmount; natural turn completion and orphan reaping; bounded authority retirement without false release');

    await open(); start = await evidence();
    await sendNative('Review establish durable URL session');
    const durable = await settled(start.submissions.length + 1); const stored = durable.live[0].stored;
    await sendNative('P7-BUSY Review URL pending'); await waitFor(evidence, e => e.live.some(s => s.running), 'URL source busy');
    await page.evaluate(path => {
      history.pushState({ ...history.state, idx: (history.state.idx || 0) + 1, key: crypto.randomUUID() }, '', path);
      dispatchEvent(new PopStateEvent('popstate'));
    }, `/chat?resume=${stored}&chat_mode=terminal&other=keep#anchor`);
    await page.getByRole('button', { name: 'Cancel switch', exact: true }).waitFor();
    await page.getByRole('link', { name: 'Config', exact: true }).first().click();
    await waitFor(evidence, e => e.live.some(s => s.terminal && s.stored === stored), 'URL switch finishes hidden');
    await ready('terminal');
    await page.getByLabel('Browser preference', { exact: true }).selectOption('native'); await ready('native');
    await page.goBack(); await ready('terminal');
    await page.waitForFunction(() => !location.search.includes('chat_mode='));
    assert.equal(new URL(page.url()).searchParams.get('other'), 'keep'); assert.equal(new URL(page.url()).hash, '#anchor');
    assert.equal((await evidence()).submissions.length - start.submissions.length, 2);
    assert.equal((await evidence()).live.filter(s => s.stored === stored).length, 1);
    scenarios.push('Hidden URL switch; Settings changes hidden host to Native; Browser Back honors Terminal and consumes URL preserving other query/hash');
    const final = await evidence(); assert.equal(final.submissions.filter(s => s.automatic).length, 0);
    console.log(JSON.stringify({ scenarios, passed: scenarios.length, automatic: 0, doubleOwner }));
  } catch (error) {
    console.error('page', (await page.locator('body').innerText().catch(() => '')).slice(-4500));
    console.error('evidence', await evidence()); throw error;
  } finally { await browser.close(); await fetch(`${base}/c2-reset`, { method: 'POST' }); }
})().catch(error => { console.error(error); process.exitCode = 1; });

const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const base = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const frontend = process.env.CHAT_TERMINAL_E2E_URL || 'http://127.0.0.1:5173';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const evidence = async () => (await fetch(`${base}/c1-evidence`)).json();
async function waitFor(read, accept, label) {
  const end = Date.now() + 90000;
  while (Date.now() < end) { const value = await read(); if (accept(value)) return value; await delay(200); }
  throw new Error(`Timed out: ${label}`);
}
(async () => {
  await waitFor(() => evidence().catch(() => null), Boolean, 'server ready');
  const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  let page;
  const scenarios = [];
  async function open(script) {
    await page?.close(); await fetch(`${base}/c2-reset`, { method: 'POST' });
    page = await browser.newPage(); page.setDefaultTimeout(90000);
    if (script) await page.addInitScript(script);
    await page.goto(`${frontend}/chat`);
    await waitFor(() => page.getByLabel('Chat Interface', { exact: true }).isEnabled(), Boolean, 'initial ready');
  }
  const selector = () => page.getByLabel('Chat Interface', { exact: true });
  async function switchTo(mode) { await selector().selectOption(mode); await waitFor(() => selector().isEnabled(), Boolean, `${mode} ready`); }
  async function send(text) { await page.getByLabel('Message Hermes', { exact: true }).fill(text); await page.getByRole('button', { name: 'Send', exact: true }).click(); }
  try {
    await open();
    let start = await evidence();
    await send('P7-BUSY C2 Native busy');
    await waitFor(evidence, e => e.live.some(s => s.running), 'Native running');
    await selector().selectOption('terminal');
    await page.getByRole('button', { name: 'Cancel switch', exact: true }).waitFor();
    assert(await page.getByLabel('Native Chat', { exact: true }).count()); assert.equal((await evidence()).pty.length, 0);
    await page.getByRole('link', { name: 'Sessions', exact: true }).first().click();
    await waitFor(evidence, e => e.pty.length === 1 && e.live.some(s => s.terminal), 'hidden switch finishes');
    await page.getByRole('link', { name: 'Chat', exact: true }).first().click();
    await waitFor(() => selector().isEnabled(), Boolean, 'visible Terminal ready');
    let current = await evidence();
    const stored = current.live.find(s => s.terminal).stored, pid = current.pty[0].pid;
    assert.equal(current.submissions.length - start.submissions.length, 1);
    await fetch(`${base}/c1-drop-viewer`, { method: 'POST' });
    await waitFor(evidence, e => e.pty.length === 1 && e.pty[0].viewer, 'reconnect without user input');
    await delay(1200);
    assert.equal((await evidence()).pty[0].pid, pid);
    assert.equal((await evidence()).submissions.length - start.submissions.length, 1);
    scenarios.push('Native busy defer; hidden route completion; same-instance reconnect without user input');
    await page.locator('.xterm-helper-textarea').focus(); await page.keyboard.type('P7-BUSY C2 Terminal busy', { delay: 100 }); await delay(400); await page.keyboard.press('Enter');
    await waitFor(evidence, e => e.live.some(s => s.running), 'Terminal running');
    await selector().selectOption('native');
    assert.equal((await evidence()).pty.length, 1);
    await waitFor(() => selector().isEnabled(), Boolean, 'Native after busy');
    assert.equal((await evidence()).live.find(s => !s.terminal).stored, stored);
    assert.equal((await evidence()).submissions.length - start.submissions.length, 2);
    scenarios.push('Terminal busy defer; same durable identity');
    // Explicit draft discard, never submit or transfer it.
    await page.getByLabel('Message Hermes', { exact: true }).fill('UNSENT MUST NOT REPLAY');
    await selector().selectOption('terminal');
    await page.getByRole('button', { name: 'Discard draft and switch', exact: true }).waitFor();
    assert.equal((await evidence()).pty.length, 0);
    await page.getByRole('button', { name: 'Cancel switch', exact: true }).click();
    await waitFor(() => selector().isEnabled(), Boolean, 'cancel acknowledged');
    assert.equal(await page.getByLabel('Message Hermes', { exact: true }).inputValue(), 'UNSENT MUST NOT REPLAY');
    await selector().selectOption('terminal');
    await page.getByRole('button', { name: 'Discard draft and switch', exact: true }).click();
    await waitFor(() => selector().isEnabled(), Boolean, 'discard then switch');
    assert.equal((await evidence()).submissions.length - start.submissions.length, 2);
    scenarios.push('draft wait; acknowledged cancel; explicit discard; zero replay');
    await fetch(`${base}/c2-fail-native`, { method: 'POST' });
    await selector().selectOption('native');
    await page.getByRole('button', { name: 'Return to previous interface', exact: true }).waitFor();
    assert.equal(await page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), 'terminal');
    await page.getByRole('button', { name: 'Return to previous interface', exact: true }).click();
    await waitFor(() => selector().isEnabled(), Boolean, 'failed Native cleanup then revert');
    assert.equal((await evidence()).live.find(s => s.terminal).stored, stored);
    scenarios.push('failed target cleanup and revert preserve conversation and preference');
    await open(() => {
      const Original = window.WebSocket;
      window.WebSocket = class extends Original {
        constructor(...args) {
          super(...args); let submit = null; const send = this.send.bind(this);
          this.send = data => { try { const request = JSON.parse(data); if (request.method === 'prompt.submit') submit = request.id; } catch {} send(data); };
          this.addEventListener('message', event => {
            try { if (submit !== null && JSON.parse(event.data).id === submit) { submit = null; event.stopImmediatePropagation(); this.close(); } } catch {}
          });
        }
      };
    });
    start = await evidence(); await send('P7-BUSY C2 lost submit ACK');
    await selector().selectOption('terminal');
    await waitFor(() => selector().isEnabled(), Boolean, 'recover uncertain submit then switch');
    current = await evidence(); assert.equal(current.submissions.length - start.submissions.length, 1);
    assert.equal(current.submissions.filter(s => s.automatic).length, 0);
    scenarios.push('lost Native prompt.submit ACK; recover; switch; exactly one submission');
    console.log(JSON.stringify({ scenarios, automatic: 0, passed: scenarios.length }));
  } catch (error) {
    console.error('page', (await page.locator('body').innerText()).slice(-4500)); console.error('evidence', await evidence()); throw error;
  } finally { await browser.close(); await fetch(`${base}/c2-reset`, { method: 'POST' }); }
})().catch(error => { console.error(error); process.exitCode = 1; });

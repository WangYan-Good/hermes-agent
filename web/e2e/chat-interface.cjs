/* Run against tests/e2e/fixtures/chat_handoff_server.py in a disposable container. */
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const baseURL = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const launch = { headless: true, args: ['--no-sandbox'] };
if (process.env.CHAT_E2E_BROWSER) launch.executablePath = process.env.CHAT_E2E_BROWSER;

(async () => {
  const browser = await chromium.launch(launch);
  const evidence = [];
  async function open({ terminal = false, ambiguous = false, path = '/chat' } = {}) {
    const page = await browser.newPage({ baseURL });
    page.setDefaultTimeout(30000);
    const sockets = []; let submits = 0;
    page.on('websocket', ws => {
      const url = new URL(ws.url());
      const socket = { path: url.pathname, profile: url.searchParams.get('profile'), resume: url.searchParams.get('resume'), closed: false, methods: [] };
      sockets.push(socket);
      ws.on('close', () => socket.closed = true);
      ws.on('framesent', event => {
        try { const request = JSON.parse(String(event.payload)); if (request.method) socket.methods.push(request.method); if (request.method === 'prompt.submit') submits++; } catch { /* raw PTY bytes */ }
      });
    });
    if (terminal) await page.addInitScript(() => localStorage.setItem('hermes.dashboard.chat.mode', 'terminal'));
    if (ambiguous) await page.addInitScript(() => {
      const Original = window.WebSocket;
      window.WebSocket = class extends Original {
        constructor(...args) {
          super(...args); let id = null; const send = this.send.bind(this);
          this.send = data => { try { const request = JSON.parse(data); if (request.method === 'prompt.submit') id = request.id; } catch {} send(data); };
          this.addEventListener('message', event => {
            try { const response = JSON.parse(event.data); if (id !== null && response.id === id) { id = null; event.stopImmediatePropagation(); this.close(); } } catch {}
          });
        }
      };
    });
    await page.goto(path);
    return { page, sockets, submits: () => submits };
  }
  const active = (page, mode) => page.waitForFunction(mode => document.body.textContent.includes(`Active: ${mode}`), mode, { timeout: 60000 });
  const choose = (page, mode) => page.locator('#chat-interface-live').selectOption(mode);
  async function send(page, text) {
    await page.waitForFunction(() => { const input = document.querySelector('textarea[aria-label="Message Hermes"]'); return input && !input.disabled; });
    await page.getByRole('textbox', { name: 'Message Hermes' }).fill(text);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
  }
  try {
    // Real default/config API contract and durable round-trip.
    const idle = await open(); const page = idle.page;
    await active(page, 'native');
    const api = path => page.request.get(path, { headers: { Authorization: 'Bearer p6-local' } }).then(response => response.json());
    assert.equal((await api('/api/config/defaults')).dashboard.chat.default_mode, 'native');
    assert.deepEqual((await api('/api/config/schema')).fields['dashboard.chat.default_mode'].options, ['native', 'terminal']);
    assert.equal(idle.sockets.filter(socket => socket.path === '/api/pty').length, 0);
    assert.equal(await page.evaluate(() => performance.getEntriesByType('resource').some(r => /xterm|TerminalChatPage/.test(r.name))), false);
    await page.getByLabel('Attach files or images').setInputFiles({ name: 'roundtrip.txt', mimeType: 'text/plain', buffer: Buffer.from('P6 real attachment') });
    await page.getByText('uploaded', { exact: true }).waitFor();
    await send(page, 'P6-IDLE');
    await page.getByText('P6 controlled answer.', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Stop', exact: true }).waitFor({ state: 'hidden' });
    const durable = new URL(page.url()).searchParams.get('resume'); assert.ok(durable);
    const historyPath = `/api/sessions/${durable}/messages?view=display&include_compacted=true&order=latest&limit=100`;
    const beforeHistory = await api(historyPath);
    assert.ok(beforeHistory.messages.some(m => m.tool_name === 'read_file' || JSON.stringify(m.tool_calls ?? []).includes('read_file')));
    assert.ok(JSON.stringify(beforeHistory).includes('roundtrip.txt'));
    await choose(page, 'terminal'); await active(page, 'terminal');
    assert.ok(idle.sockets.find(s => s.path === '/api/pty' && s.resume === durable && !s.closed));
    await choose(page, 'native'); await active(page, 'native');
    assert.equal(new URL(page.url()).searchParams.get('resume'), durable);
    assert.equal(idle.submits(), 1);
    assert.deepEqual((await api(historyPath)).messages, beforeHistory.messages);
    assert.equal(await page.getByText('P6-IDLE', { exact: false }).count(), 1);
    assert.equal(await page.getByText('P6 controlled answer.', { exact: true }).count(), 1);
    assert.equal(idle.sockets.filter(s => s.path === '/api/pty' && !s.closed).length, 0);
    assert.equal(idle.sockets.filter(s => s.path === '/api/ws' && !s.closed).length, 1);
    await page.getByRole('textbox', { name: 'Message Hermes' }).fill('UNSENT');
    await choose(page, 'terminal');
    await page.getByRole('button', { name: 'Discard draft and switch' }).waitFor();
    assert.equal(idle.submits(), 1);
    await page.getByRole('button', { name: 'Cancel switch', exact: true }).click();
    assert.equal(await page.getByRole('textbox', { name: 'Message Hermes' }).inputValue(), 'UNSENT');
    const backend = await api('/p6-evidence');
    assert.equal(backend.submissions.length, 1); assert.equal(backend.submissions[0].automatic, false);
    assert.equal(backend.wire_metadata, false);
    evidence.push({ scenario: 'default-idle-durable-roundtrip-draft-cancel', backend, submits: idle.submits(), durable, sockets: idle.sockets });
    await page.close();

    const legacy = await open({ terminal: true }); await active(legacy.page, 'terminal');
    assert.equal(legacy.sockets.filter(s => s.path === '/api/pty' && !s.closed).length, 1);
    assert.equal(legacy.sockets.some(s => s.methods.includes('prompt.submit')), false);
    evidence.push({ scenario: 'explicit-terminal', sockets: legacy.sockets }); await legacy.page.close();

    for (const ambiguous of [false, true]) {
      const run = await open({ ambiguous }); const page = run.page; await active(page, 'native');
      await send(page, `P6-BUSY-${ambiguous}`); await choose(page, 'terminal');
      await page.waitForFunction(() => document.body.textContent.includes('Switching to terminal'));
      assert.equal(await page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), null);
      assert.equal(run.submits(), 1); assert.equal(run.sockets.filter(s => s.path === '/api/pty').length, 0);
      if (!ambiguous) await page.getByRole('link', { name: 'Config', exact: true }).click();
      await active(page, 'terminal'); assert.equal(run.submits(), 1);
      if (!ambiguous) {
        assert.ok(new URL(page.url()).pathname.endsWith('/config'));
        assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('xterm-helper-textarea')), false);
        await page.getByRole('link', { name: 'Chat', exact: true }).click();
      }
      // Wait for the real resume paint overlay before typing into xterm.
      await page.waitForTimeout(2000);
      await page.locator('.xterm-helper-textarea').focus();
      await page.keyboard.type('P6-BUSY-TERMINAL', { delay: 40 });
      await page.waitForTimeout(150); await page.keyboard.press('Enter');
      await page.waitForFunction(async () => (await (await fetch('/p6-evidence')).json()).sessions.some(s => s.running));
      await choose(page, 'native');
      await page.waitForFunction(() => document.body.textContent.includes('Switching to native'));
      assert.equal(run.sockets.filter(s => s.path === '/api/pty' && !s.closed).length, 1);
      await active(page, 'native');
      assert.equal(run.submits(), 1); assert.equal(run.sockets.filter(s => s.path === '/api/pty' && !s.closed).length, 0);
      evidence.push({ scenario: ambiguous ? 'lost-ack-and-busy-both-directions' : 'busy-both-directions', submits: run.submits(), sockets: run.sockets }); await page.close();
    }

    const control = await browser.newPage({ baseURL });
    await control.request.post('/p6-control', { data: { action: 'history' } });
    const lineage = await open({ path: '/chat?resume=p6-branch-a' }); await active(lineage.page, 'native');
    const before = await (await control.request.get('/p6-evidence')).json();
    const lineageHistory = async () => (await (await control.request.get('/api/sessions/p6-branch-a/messages?view=display&include_compacted=true&order=latest&limit=100', { headers: { Authorization: 'Bearer p6-local' } })).json()).messages;
    const lineageRows = await lineageHistory();
    assert.equal(new URL(lineage.page.url()).searchParams.get('resume'), 'p6-branch-b');
    await choose(lineage.page, 'terminal'); await active(lineage.page, 'terminal');
    await choose(lineage.page, 'native'); await active(lineage.page, 'native');
    assert.equal(await lineage.page.getByText('P6 lineage user', { exact: true }).count(), 1);
    assert.equal(await lineage.page.getByText('Excluded branch ancestor', { exact: true }).count(), 0);
    assert.deepEqual(await lineageHistory(), lineageRows);
    const after = await (await control.request.get('/p6-evidence')).json();
    assert.equal(after.submissions.length, before.submissions.length); assert.equal(lineage.submits(), 0);
    evidence.push({ scenario: 'compressed-branch-cross-segment-tool-crash-marker-roundtrip', automaticSubmissions: 0, sockets: lineage.sockets });
    await lineage.page.close();
    await control.request.post('/p6-control', { data: { action: 'profiles' } });
    const scoped = await open({ path: '/chat?profile=p6-a' }); await active(scoped.page, 'native');
    await send(scoped.page, 'P6-BUSY-PROFILE'); await choose(scoped.page, 'terminal');
    await scoped.page.locator('#hermes-profile-switcher').click();
    await scoped.page.getByRole('option', { name: 'p6-b', exact: true }).click();
    await active(scoped.page, 'terminal');
    const profilePty = scoped.sockets.find(s => s.path === '/api/pty' && !s.closed);
    assert.equal(profilePty.profile, 'p6-b'); assert.equal(profilePty.resume, null);
    assert.equal(await scoped.page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), null);
    evidence.push({ scenario: 'profile-switch-cancels-pending', sockets: scoped.sockets }); await scoped.page.close();

    await control.request.post('/p6-control', { data: { action: 'plugin' } });
    const pluginPage = await browser.newPage({ baseURL }); let pluginSockets = 0;
    pluginPage.on('websocket', () => pluginSockets++);
    await pluginPage.addInitScript(() => sessionStorage.setItem('hermes:plugin-manifests', '[]'));
    await pluginPage.route('**/api/dashboard/plugins', async route => { const response = await route.fetch(); await new Promise(resolve => setTimeout(resolve, 750)); await route.fulfill({ response }); });
    await pluginPage.goto('/chat'); await pluginPage.getByText('P6 plugin owns chat').waitFor();
    assert.equal(pluginSockets, 0);
    await pluginPage.getByRole('link', { name: 'Config', exact: true }).click();
    await pluginPage.locator('#chat-browser-preference').selectOption('terminal');
    assert.equal(pluginSockets, 0);
    evidence.push({ scenario: 'late-plugin-manifest-and-settings', sockets: pluginSockets });
    await control.request.post('/p6-control', { data: { action: 'remove-plugin' } });
    await pluginPage.close(); await control.close();
    console.log(JSON.stringify({ result: 'PASS', evidence }, null, 2));
  } catch (error) {
    for (const page of browser.contexts().flatMap(c => c.pages())) {
      console.error(page.url(), (await page.locator('body').innerText().catch(() => '')).slice(-2500));
    }
    console.error(error); process.exitCode = 1;
  } finally { await browser.close(); }
})();

/* Real Dashboard auth, HTTP/WS, AIAgent and SessionDB. Only the model HTTP boundary is controlled. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('@playwright/test');
const baseURL = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const launch = { headless: true, args: ['--no-sandbox'] };
if (process.env.CHAT_E2E_BROWSER) launch.executablePath = process.env.CHAT_E2E_BROWSER;

(async () => {
  const deadline = Date.now() + 60000;
  while (true) {
    try { if ((await fetch(`${baseURL}/api/status`)).ok) break; } catch {}
    if (Date.now() >= deadline) throw new Error('Dashboard fixture did not become ready');
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  const browser = await chromium.launch(launch);
  const evidence = [];
  const openPages = [];
  const token = 'p7-local';
  async function open({ preference, blocked = false, ambiguous = false, path = '/chat', delayedPlugin = false, cachedOverride = false, viewport } = {}) {
    const page = await browser.newPage({ baseURL, viewport });
    openPages.push(page); page.setDefaultTimeout(30000);
    const sockets = []; const errors = []; const resources = []; let submits = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => resources.push(request.url()));
    page.on('websocket', ws => {
      const url = new URL(ws.url());
      const socket = { path: url.pathname, closed: false, methods: [], events: [] };
      sockets.push(socket); ws.on('close', () => { socket.closed = true; });
      ws.on('framereceived', event => {
        try { const response = JSON.parse(String(event.payload)); if (response.method === 'event') socket.events.push({ type: response.params.type, name: response.params.payload?.name }); } catch {}
      });
      ws.on('framesent', event => {
        try {
          const request = JSON.parse(String(event.payload));
          if (request.method) socket.methods.push(request.method);
          if (request.method === 'prompt.submit') submits++;
        } catch { /* No terminal byte transport is expected. */ }
      });
    });
    if (preference !== undefined) await page.addInitScript(value => {
      localStorage.setItem('hermes.dashboard.chat.mode', value);
      localStorage.setItem('p7-unrelated', 'keep');
    }, preference);
    if (blocked) await page.addInitScript(() => Object.defineProperty(window, 'localStorage', { get() { throw new Error('Blocked storage'); } }));
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
    if (cachedOverride) await page.addInitScript(() => sessionStorage.setItem('hermes:plugin-manifests', JSON.stringify([{ name: 'p7-override', label: 'P7 override', tab: { path: '/p7-plugin', override: '/chat' }, entry: 'index.js' }])));
    if (delayedPlugin) {
      if (!cachedOverride) await page.addInitScript(() => sessionStorage.setItem('hermes:plugin-manifests', '[]'));
      await page.route('**/api/dashboard/plugins', async route => {
        await new Promise(resolve => setTimeout(resolve, 700)); await route.continue();
      });
    }
    await page.goto(path);
    const run = { page, sockets, errors, resources, submits: () => submits };
    run.api = async (path, data) => {
      const response = data === undefined
        ? await page.request.get(path, { headers: { Authorization: `Bearer ${token}` } })
        : await page.request.post(path, { headers: { Authorization: `Bearer ${token}` }, data });
      assert.equal(response.ok(), true, `${path}: ${response.status()}`); return response.json();
    };
    return run;
  }
  async function ready(run) {
    await run.page.getByRole('textbox', { name: 'Message Hermes' }).waitFor();
    await run.page.waitForFunction(() => { const input = document.querySelector('textarea[aria-label="Message Hermes"]'); return input && !input.disabled; });
    assert.equal(await run.page.getByLabel('Native Chat', { exact: true }).count(), 1);
    assert.equal(await run.page.locator('.xterm').count(), 0);
    assert.equal(await run.page.locator('#chat-interface-live').count(), 0);
    assert.equal(run.sockets.filter(s => s.path === '/api/pty').length, 0);
    assert.equal(run.sockets.filter(s => s.path === '/api/ws' && !s.closed).length, 1);
    assert.equal(run.resources.some(url => /xterm|TerminalChatPage/.test(url)), false);
    assert.deepEqual(run.errors, []);
    assert.ok(new URL(run.page.url()).pathname.endsWith('/chat'));
    assert.ok(await run.page.title());
  }
  async function send(run, text) {
    await ready(run);
    await run.page.getByRole('textbox', { name: 'Message Hermes' }).fill(text);
    await run.page.getByRole('button', { name: 'Send', exact: true }).click();
    await run.page.getByText('P7 controlled answer.', { exact: true }).last().waitFor();
    await run.page.getByRole('button', { name: 'Stop', exact: true }).waitFor({ state: 'hidden' });
  }
  async function record(name, run, extra = {}) {
    const backend = await run.api('/p7-evidence');
    console.log(`PASS ${name}`);
    evidence.push({ scenario: name, url: run.page.url(), submits: run.submits(), sockets: run.sockets, errors: run.errors, backend, ...extra });
  }
  try {
    if (process.env.CHAT_E2E_GATE) {
      const gate = process.env.CHAT_E2E_GATE;
      const run = await open({ preference: gate === 'preference' ? 'terminal' : undefined, path: gate === 'url' ? '/chat?chat_mode=terminal' : '/chat', ambiguous: gate === 'uncertain' });
      await ready(run);
      if (gate === 'plugin') {
        await run.api('/p7-control', { action: 'plugin' });
        const overridden = await open({ delayedPlugin: true });
        try {
          await overridden.page.getByText('P7 plugin owns chat', { exact: true }).waitFor();
          assert.equal(overridden.sockets.filter(s => ['/api/ws', '/api/pty'].includes(s.path)).length, 0);
          assert.equal(await overridden.page.getByLabel('Native Chat', { exact: true }).count(), 0);
        } finally { await run.api('/p7-control', { action: 'remove-plugin' }); }
      }
      if (gate === 'tool' || gate === 'uncertain') {
        const prior = await run.api('/p7-evidence');
        await send(run, gate === 'tool' ? 'P7-TOOL' : 'P7-BUSY ambiguous ACK');
        assert.equal(run.submits(), 1);
        assert.equal((await run.api('/p7-evidence')).submissions.length - prior.submissions.length, 1);
        if (gate === 'tool') {
          const durable = new URL(run.page.url()).searchParams.get('resume');
          const history = await run.api(`/api/sessions/${durable}/messages?view=display&include_compacted=true&order=latest&limit=100`);
          assert.ok(history.messages.some(m => m.role === 'tool' && m.tool_name === 'terminal' && String(m.content).includes('P7 real terminal output')));
          assert.ok(run.sockets.some(s => s.events.some(e => e.type === 'tool.complete' && e.name === 'terminal')));
        }
      }
      await record(`gate-${gate}`, run);
      return;
    }
    const fresh = await open(); await ready(fresh);
    assert.equal(fresh.sockets.filter(s => s.path === '/api/ws').length, 1);
    assert.equal(fresh.submits(), 0);
    assert.equal((await fresh.api('/api/config/schema')).fields['dashboard.chat.default_mode'], undefined);
    assert.equal((await fresh.api('/api/config/defaults')).dashboard.chat, undefined);
    await record('new-user', fresh);
    if (process.env.CHAT_E2E_SCREENSHOT) await fresh.page.screenshot({ path: process.env.CHAT_E2E_SCREENSHOT });
    await fresh.page.close();

    for (const value of ['terminal', 'native', 'garbage']) {
      const run = await open({ preference: value, path: `/chat?chat_mode=${value}&other=keep#anchor` }); await ready(run);
      await run.page.waitForFunction(() => !new URL(location.href).searchParams.has('chat_mode'));
      assert.equal(new URL(run.page.url()).searchParams.get('other'), 'keep');
      assert.equal(new URL(run.page.url()).hash, '#anchor');
      assert.equal(await run.page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), null);
      assert.equal(await run.page.evaluate(() => localStorage.getItem('p7-unrelated')), 'keep');
      assert.equal(run.submits(), 0);
      await record(`legacy-${value}`, run); await run.page.close();
    }
    const blocked = await open({ blocked: true, path: '/chat?chat_mode=terminal', viewport: { width: 390, height: 844 } }); await ready(blocked);
    assert.equal(blocked.submits(), 0); await record('blocked-storage-mobile', blocked); await blocked.page.close();

    const tool = await open(); await ready(tool);
    const prior = await tool.api('/p7-evidence');
    await tool.page.getByLabel('Attach files or images').setInputFiles({ name: 'roundtrip.txt', mimeType: 'text/plain', buffer: Buffer.from('P7 attachment') });
    await tool.page.getByText('uploaded', { exact: true }).waitFor();
    await send(tool, 'P7-TOOL');
    const durable = new URL(tool.page.url()).searchParams.get('resume'); assert.ok(durable);
    const historyPath = `/api/sessions/${durable}/messages?view=display&include_compacted=true&order=latest&limit=100`;
    const history = await tool.api(historyPath);
    assert.ok(history.messages.some(m => m.role === 'tool' && m.tool_name === 'terminal' && String(m.content).includes('P7 real terminal output')));
    assert.ok(JSON.stringify(history).includes('roundtrip.txt'));
    await tool.page.getByText('terminal', { exact: false }).first().waitFor();
    assert.ok(tool.sockets.some(s => s.events.some(e => e.type === 'tool.start' && e.name === 'terminal')));
    assert.ok(tool.sockets.some(s => s.events.some(e => e.type === 'tool.complete' && e.name === 'terminal')));
    assert.equal((await tool.api('/p7-evidence')).submissions.length - prior.submissions.length, 1);
    assert.equal(tool.submits(), 1);
    await tool.page.getByRole('link', { name: 'Sessions', exact: true }).click();
    await tool.page.getByRole('link', { name: 'Chat', exact: true }).click(); await ready(tool);
    assert.equal(tool.sockets.filter(s => s.path === '/api/ws').length, 1);
    assert.equal(tool.submits(), 1);
    await record('real-terminal-tool-attachment-route-persistence', tool, { durable }); await tool.page.close();

    const resume = await open({ path: `/chat?resume=${durable}&chat_mode=terminal` }); await ready(resume);
    await resume.page.getByText('P7-TOOL', { exact: false }).first().waitFor();
    const beforeResume = await resume.api('/p7-evidence');
    assert.equal(resume.submits(), 0);
    await send(resume, 'P7 durable next explicit prompt');
    assert.equal(resume.submits(), 1);
    assert.equal((await resume.api('/p7-evidence')).submissions.length - beforeResume.submissions.length, 1);
    assert.equal(new URL(resume.page.url()).searchParams.get('resume'), durable);
    await resume.page.reload(); await ready(resume);
    assert.equal(resume.submits(), 1);
    await resume.page.getByRole('link', { name: 'Sessions', exact: true }).click();
    await resume.page.goBack(); await ready(resume);
    assert.equal(resume.submits(), 1);
    await record('durable-resume-refresh-back-no-replay', resume, { durable }); await resume.page.close();

    const uncertain = await open({ ambiguous: true }); await ready(uncertain);
    const beforeUnknown = await uncertain.api('/p7-evidence');
    await send(uncertain, 'P7-BUSY ambiguous ACK'); await ready(uncertain);
    assert.equal(uncertain.submits(), 1);
    assert.equal((await uncertain.api('/p7-evidence')).submissions.length - beforeUnknown.submissions.length, 1);
    await record('ambiguous-submit-no-replay', uncertain); await uncertain.page.close();

    const control = await open(); await ready(control);
    await control.api('/p7-control', { action: 'history' });
    const lineage = await open({ path: '/chat?resume=p7-branch-a&learn=lineage-context&chat_mode=terminal' }); await ready(lineage);
    await lineage.page.getByText('P7 lineage answer', { exact: true }).waitFor();
    assert.equal(new URL(lineage.page.url()).searchParams.get('resume'), 'p7-branch-b');
    assert.equal(await lineage.page.getByText('Excluded branch ancestor', { exact: true }).count(), 0);
    assert.equal(lineage.submits(), 0);
    assert.equal(await lineage.page.getByRole('textbox', { name: 'Message Hermes' }).inputValue(), '/learn lineage-context');
    assert.equal(new URL(lineage.page.url()).searchParams.has('learn'), false);
    assert.equal((await lineage.api('/p7-evidence')).submissions.some(s => s.automatic), false);
    await record('terminal-webui-lineage-crash-marker-no-replay', lineage); await lineage.page.close();

    await control.api('/p7-control', { action: 'profiles' });
    const scoped = await open({ preference: 'terminal', path: `/chat?resume=${durable}&learn=old-profile-draft` }); await ready(scoped);
    await scoped.page.locator('#hermes-profile-switcher').getByRole('combobox').click();
    await scoped.page.getByRole('option', { name: 'p7-b', exact: true }).click();
    await scoped.page.waitForFunction(() => new URL(location.href).searchParams.get('profile') === 'p7-b');
    await ready(scoped);
    assert.equal(scoped.submits(), 0);
    assert.equal(new URL(scoped.page.url()).searchParams.has('resume'), false);
    assert.equal(await scoped.page.getByRole('textbox', { name: 'Message Hermes' }).inputValue(), '');
    assert.ok(scoped.sockets.slice(1).some(s => s.methods.includes('session.create')));
    await record('legacy-profile-terminal-default', scoped); await scoped.page.close();
    for (const name of ['p7-a', 'p7-c']) {
      const scoped = await open({ path: `/chat?profile=${name}&chat_mode=terminal` }); await ready(scoped);
      assert.equal(scoped.submits(), 0);
      await record(`legacy-profile-${name}`, scoped); await scoped.page.close();
    }

    const learn = await open({ path: '/chat?learn=debugging&chat_mode=terminal' }); await ready(learn);
    assert.equal(await learn.page.getByRole('textbox', { name: 'Message Hermes' }).inputValue(), '/learn debugging');
    assert.equal(learn.submits(), 0);
    assert.equal(new URL(learn.page.url()).searchParams.has('learn'), false);
    await record('learn-unsent-draft', learn); await learn.page.close();

    await control.api('/p7-control', { action: 'plugin' });
    const overridden = await open({ delayedPlugin: true, preference: 'terminal' });
    await overridden.page.getByText('P7 plugin owns chat', { exact: true }).waitFor();
    assert.equal(overridden.sockets.filter(s => ['/api/ws', '/api/pty'].includes(s.path)).length, 0);
    assert.equal(await overridden.page.getByLabel('Native Chat', { exact: true }).count(), 0);
    assert.equal(overridden.submits(), 0);
    await record('delayed-plugin-fresh-confirmation', overridden); await overridden.page.close();
    await control.api('/p7-control', { action: 'remove-plugin' });
    const invalidated = await open({ cachedOverride: true, delayedPlugin: true }); await ready(invalidated);
    assert.equal(invalidated.sockets.filter(s => s.path === '/api/ws').length, 1);
    assert.equal(invalidated.submits(), 0);
    await record('removed-plugin-cache-invalidated', invalidated); await invalidated.page.close();
    await control.page.close();
    assert.equal(evidence.every(e => e.backend.wire_metadata === false), true);
    const result = JSON.stringify({ status: 'PASS', evidence }, null, 2);
    if (process.env.CHAT_E2E_EVIDENCE) fs.writeFileSync(process.env.CHAT_E2E_EVIDENCE, result);
    console.log(result);
  } finally {
    for (const page of openPages) if (!page.isClosed()) await page.close();
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

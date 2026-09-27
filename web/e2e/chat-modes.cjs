const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const base = process.env.CHAT_E2E_URL || 'http://127.0.0.1:8765';
const frontend = process.env.CHAT_TERMINAL_E2E_URL || 'http://127.0.0.1:5173';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const headers = { Authorization: 'Bearer p7-local', 'Content-Type': 'application/json' };
const api = async (path, body, method = 'POST') => (await fetch(`${base}${path}`, { headers, ...(body === undefined ? {} : { method, body: JSON.stringify(body) }) })).json();
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
  let page; const scenarios = [];
  async function open(path, init, configFailure = false) {
    await page?.close(); await api('/c2-reset', {});
    page = await browser.newPage(); page.setDefaultTimeout(60000);
    const sockets = [];
    page.on('websocket', ws => sockets.push(new URL(ws.url()).pathname));
    if (init) await page.addInitScript(init);
    if (configFailure) await page.route('**/api/config?*', route => route.fulfill({ status: 503, body: '{}' }));
    await page.goto(`${frontend}${path}`); return sockets;
  }
  async function ready(mode) {
    await page.waitForFunction(() => { const select = document.querySelector('select[aria-label="Chat Interface"]'); return select && !select.disabled; });
    assert.equal(await page.getByLabel('Chat Interface', { exact: true }).inputValue(), mode);
  }
  try {
    let sockets = await open('/config');
    await page.getByLabel('Browser preference', { exact: true }).selectOption('terminal');
    await page.getByLabel('Profile default', { exact: true }).selectOption('terminal');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    await page.waitForFunction(() => document.body.innerText.includes('Saved profile default: terminal'));
    assert.equal(sockets.filter(s => ['/api/ws', '/api/pty'].includes(s)).length, 0);
    await page.getByLabel('Browser preference', { exact: true }).selectOption('profile');
    assert.equal(await page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), null);
    await page.getByRole('link', { name: 'Chat', exact: true }).first().click(); await ready('terminal');
    scenarios.push('Settings profile selector; browser selector; Follow profile; no Chat sockets before first visit; profile Terminal');
    await open('/chat?chat_mode=native&other=keep#anchor', () => localStorage.setItem('hermes.dashboard.chat.mode', 'terminal'));
    await ready('native');
    await page.waitForFunction(() => !location.search.includes('chat_mode'));
    assert.equal(new URL(page.url()).searchParams.get('other'), 'keep'); assert.equal(new URL(page.url()).hash, '#anchor');
    assert.equal(await page.evaluate(() => localStorage.getItem('hermes.dashboard.chat.mode')), 'terminal');
    scenarios.push('URL Native overrides browser/profile Terminal; query/hash preserved; browser preference unchanged');
    const config = await api('/api/config'); config.dashboard.chat.default_mode = 'native'; await api('/api/config', { config }, 'PUT');
    await open('/chat', () => Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } }));
    await ready('native'); await page.getByLabel('Chat Interface', { exact: true }).selectOption('terminal'); await ready('terminal');
    assert((await page.locator('body').innerText()).includes('could not save'));
    scenarios.push('blocked localStorage retains current page choice and displays notice; empty durable switch');
    await open('/chat?chat_mode=terminal', undefined, true); await ready('terminal');
    scenarios.push('valid URL starts Terminal despite profile config failure');
    await open('/chat?chat_mode=terminal&learn=debugging&other=keep#anchor'); await ready('terminal');
    assert.equal(new URL(page.url()).searchParams.get('learn'), 'debugging');
    const beforeLearn = (await api('/c1-evidence')).submissions.length;
    await page.getByLabel('Chat Interface', { exact: true }).selectOption('native'); await ready('native');
    await page.waitForFunction(() => !location.search.includes('learn='));
    assert.equal(await page.getByLabel('Message Hermes', { exact: true }).inputValue(), '/learn debugging');
    assert.equal((await api('/c1-evidence')).submissions.length, beforeLearn);
    assert.equal(new URL(page.url()).searchParams.get('other'), 'keep');
    assert.equal(new URL(page.url()).hash, '#anchor');
    scenarios.push('Terminal retains learn; Native consumes it as an unsent draft; query/hash preserved; no automatic input');
    await page.close(); await api('/c2-reset', {}); await api('/p7-control', { action: 'plugin' });
    page = await browser.newPage(); sockets = []; page.on('websocket', ws => sockets.push(new URL(ws.url()).pathname));
    await page.addInitScript(() => { sessionStorage.setItem('hermes:plugin-manifests', '[]'); localStorage.setItem('hermes.dashboard.chat.mode', 'terminal'); });
    await page.route('**/api/dashboard/plugins', async route => { await delay(800); await route.continue(); });
    await page.goto(`${frontend}/chat?chat_mode=terminal`);
    await page.getByText('P7 plugin owns chat', { exact: true }).waitFor();
    assert.equal(sockets.filter(s => ['/api/ws', '/api/pty'].includes(s)).length, 0);
    assert.equal(await page.getByLabel('Native Chat', { exact: true }).count(), 0);
    assert.equal(await page.getByLabel('Terminal Chat', { exact: true }).count(), 0);
    scenarios.push('fresh plugin confirmation; plugin override; zero built-in surfaces and sockets');
    console.log(JSON.stringify({ scenarios, passed: scenarios.length }));
  } catch (error) { console.error(error); console.error((await page.locator('body').innerText().catch(() => 'page closed')).slice(-4000)); throw error; }
  finally { await browser.close(); await api('/c2-reset', {}); await api('/p7-control', { action: 'remove-plugin' }); }
})().catch(error => { console.error(error); process.exitCode = 1; });

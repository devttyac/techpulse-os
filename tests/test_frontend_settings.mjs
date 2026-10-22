import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
const source = html.slice(html.indexOf('async function openSettingsModal()'),
                          html.indexOf('async function runStorageCleanup()'));

function runtime(responses) {
  const elements = new Map(), calls = [];
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    const classes = new Set();
    elements.set(match[1], { value: '', textContent: '', innerText: '', innerHTML: '', disabled: false,
      classList: { add: x => classes.add(x), remove: x => classes.delete(x) } });
  }
  const context = vm.createContext({ document: { getElementById: id => elements.get(id) || null },
    apiFetch: async (url, options) => { calls.push({ url, options }); return responses.shift(); },
    loadEpisodesFromBackend: () => { throw Error('Settings save must not reload episodes'); },
    setTimeout: () => 1, console: { error() {} } });
  vm.runInContext(source, context);
  return { elements, calls, context };
}
function reply(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test('committed Settings submit the whole snapshot with revision and zone, without cleanup claim', async () => {
  const config = { max_episodes_retained: 14, cron_schedule: '0 7 * * *',
    timezone_id: 'Asia/Singapore', chat_provider: 'gemini', chat_model: 'gemini-3.6-flash' };
  const r = runtime([reply(200, { revision: 3, config, storage: {} }),
                     reply(200, { revision: 4, config: { ...config, max_episodes_retained: 30 }, storage_status: 'unavailable' })]);
  await r.context.loadSettings();
  r.elements.get('setting-retention').value = '30';
  await r.context.saveSettings();
  const body = JSON.parse(r.calls[1].options.body);
  assert.equal(body.expected_revision, 3);
  assert.equal(body.config.timezone_id, 'Asia/Singapore');
  assert.equal(body.config.max_episodes_retained, 30);
  assert.deepEqual(Object.keys(body.config).sort(), Object.keys(config).sort());
  assert.doesNotMatch(r.elements.get('settings-status-msg').textContent, /enforced|purged/i);
});

test('fresh and migration require an explicit choice and deliberate cron entry', async () => {
  for (const kind of ['fresh', 'migrate']) {
    const detail = { initialization: kind, fresh_defaults: kind === 'fresh' ?
      { max_episodes_retained: 14, timezone_id: 'Asia/Singapore' } : undefined };
    const r = runtime([reply(409, { detail }), reply(200, { revision: 1, config: {} })]);
    await r.context.loadSettings();
    assert.equal(r.elements.get('setting-cron').value, '');
    assert.equal(r.elements.get('setting-initialization').value, '');
    await r.context.saveSettings();
    assert.equal(r.calls.length, 1);
    r.elements.get('setting-initialization').value = kind;
    r.elements.get('setting-cron').value = '0 6 * * *';
    if (kind === 'migrate') {
      r.elements.get('setting-retention').value = '14';
      r.elements.get('setting-timezone').value = 'Asia/Singapore';
    }
    await r.context.saveSettings();
    assert.equal(JSON.parse(r.calls[1].options.body).initialization, kind);
  }
});

test('conflict preserves the entered values and explains revision failure', async () => {
  const config = { max_episodes_retained: 14, cron_schedule: '0 7 * * *',
    timezone_id: 'Asia/Singapore', chat_provider: 'gemini', chat_model: 'gemini-3.6-flash' };
  const r = runtime([reply(200, { revision: 2, config, storage: {} }),
                     reply(409, { detail: 'stale Settings revision' })]);
  await r.context.loadSettings();
  r.elements.get('setting-cron').value = '0 9 * * *';
  await r.context.saveSettings();
  assert.equal(r.elements.get('setting-cron').value, '0 9 * * *');
  assert.match(r.elements.get('settings-status-msg').textContent, /stale|conflict|changed/i);
});

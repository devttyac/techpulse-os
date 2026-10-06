import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
const runtimeSource = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1]).find(source => source.includes('function changeEpisode('));
assert.ok(runtimeSource, 'the actual inline SPA runtime must be loaded');
const seed = JSON.parse(readFileSync(new URL('../seed_data/episodes/ep-142.json', import.meta.url), 'utf8'));

function episode(id, pipelineRun) {
  const record = structuredClone(seed);
  record.id = id;
  record.episode_number = Number(id.replace('ep-', ''));
  // Keep fallback-shaped content: classification must come from provenance alone.
  delete record.pipeline_run;
  if (pipelineRun !== undefined) record.pipeline_run = pipelineRun;
  return record;
}

function createRuntime(records) {
  // The SPA stays real; only browser/platform boundaries are doubled. IDs come
  // from the shipped HTML so forgetting the visible region fails these tests.
  const elements = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    const id = match[1];
    const classes = new Set();
    let markup = '';
    const element = {
      textContent: '', innerText: '', value: '', className: '', style: {},
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
      },
      querySelectorAll: () => [],
      pause() {}, load() {}, setAttribute() {},
    };
    Object.defineProperty(element, 'innerHTML', {
      get: () => markup,
      set(value) {
        assert.ok(!id.startsWith('ep-generation-'), 'provenance must render as text, not HTML');
        markup = value;
      },
    });
    elements.set(id, element);
  }
  const storage = new Map();
  const context = vm.createContext({
    console: { log() {}, error() {}, warn() {} },
    document: {
      documentElement: { setAttribute() {} },
      getElementById: id => elements.get(id) || null,
    },
    localStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    window: {
      location: { origin: 'https://example.test' },
      addEventListener() {},
      prompt() { throw new Error('unexpected authentication prompt'); },
    },
    URL, Headers,
    fetch: async (url, options = {}) => {
      assert.equal(url, '/api/episodes');
      assert.equal(options.method, undefined);
      return { ok: true, status: 200, json: async () => ({ episodes: records }) };
    },
  });
  vm.runInContext(runtimeSource, context, { filename: 'static/index.html' });
  return {
    load: () => context.loadEpisodesFromBackend(),
    select: id => context.changeEpisode(id),
    text(id) {
      assert.ok(elements.has(id), `visible ${id} must exist in the shipped HTML`);
      return elements.get(id).textContent;
    },
    status: () => elements.get('ep-generation-status'),
  };
}

test('loading a fallback episode visibly discloses fixed narration and flashcards', async () => {
  const runtime = createRuntime([episode('ep-999', {
    schema_version: 1, synthesis: { path: 'deterministic_fallback', model: null },
  })]);
  await runtime.load();
  assert.match(runtime.text('ep-generation-label'), /deterministic fallback/i);
  assert.match(runtime.text('ep-generation-detail'), /main narration and flashcards use fixed templates/i);
  assert.match(runtime.text('ep-generation-detail'), /domain takeaways use feed summaries/i);
});

test('switching fallback to LLM to legacy replaces provenance instead of retaining prior metadata', async () => {
  const runtime = createRuntime([
    episode('ep-999', { schema_version: 1, synthesis: { path: 'deterministic_fallback', model: null } }),
    episode('ep-998', { schema_version: 1, synthesis: { path: 'llm', model: 'gemini-recorded-model' } }),
    episode('ep-997'),
  ]);
  await runtime.load();
  assert.match(runtime.text('ep-generation-label'), /deterministic fallback/i);
  runtime.select('ep-998');
  assert.match(runtime.text('ep-generation-label'), /model.generated/i);
  assert.match(runtime.text('ep-generation-detail'), /gemini-recorded-model/);
  assert.doesNotMatch(runtime.text('ep-generation-detail'), /fixed templates/i);
  runtime.select('ep-997');
  assert.match(runtime.text('ep-generation-label'), /provenance unavailable/i);
  assert.doesNotMatch(runtime.text('ep-generation-detail'), /gemini-recorded-model|fixed templates/i);
});

test('the embedded legacy episode is unknown despite its fallback-shaped title and cards', () => {
  const runtime = createRuntime([]);
  runtime.select('ep-142');
  assert.match(runtime.text('ep-generation-label'), /provenance unavailable/i);
});

test('unrecognized or malformed records never imply a known synthesis mode', async t => {
  const cases = [
    ['missing block', undefined], ['null block', null], ['scalar block', 'llm'],
    ['missing schema', { synthesis: { path: 'llm', model: 'gemini-test' } }],
    ['future schema', { schema_version: 2, synthesis: { path: 'llm', model: 'gemini-test' } }],
    ['missing synthesis', { schema_version: 1 }],
    ['array synthesis', { schema_version: 1, synthesis: [] }],
    ['unexpected path', { schema_version: 1, synthesis: { path: 'provider_failed', model: null } }],
    ['LLM without model', { schema_version: 1, synthesis: { path: 'llm', model: null } }],
    ['blank model', { schema_version: 1, synthesis: { path: 'llm', model: '  ' } }],
    ['object model', { schema_version: 1, synthesis: { path: 'llm', model: { name: 'gemini-test' } } }],
    ['fallback with model', { schema_version: 1, synthesis: { path: 'deterministic_fallback', model: 'gemini-test' } }],
  ];
  for (const [name, record] of cases) {
    await t.test(name, async () => {
      const runtime = createRuntime([episode('ep-999', record)]);
      await runtime.load();
      assert.match(runtime.text('ep-generation-label'), /provenance unavailable/i);
      assert.doesNotMatch(runtime.text('ep-generation-detail'), /gemini-test|fixed templates/i);
    });
  }
});

test('recorded model markup stays literal text and cannot influence status classes', async () => {
  const hostileModel = '<img src=x onerror="alert(1)">';
  const runtime = createRuntime([episode('ep-999', {
    schema_version: 1, synthesis: { path: 'llm', model: hostileModel },
  })]);
  await runtime.load();
  assert.match(runtime.text('ep-generation-label'), /model.generated/i);
  assert.ok(runtime.text('ep-generation-detail').includes(hostileModel));
  assert.ok(!runtime.status().className.includes(hostileModel));
});

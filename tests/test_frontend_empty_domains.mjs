import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
const source = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]).find(s => s.includes('function changeEpisode('));
const domains = ['ai', 'cloud', 'data', 'sec', 'devops', 'arch', 'finops', 'gov'];
function episode(id, active = ['ai']) {
  return { id, episode_number: Number(id.slice(3)), date: '2026-10-07', title: 'Received stories', summary: 'Source summaries', duration: '01:00', audio_url: active.length ? `/audio/${id}.mp3` : null,
    content_availability: { schema_version: 1, domains: Object.fromEntries(domains.map(d => [d, { status: active.includes(d) ? 'available' : 'no_received_candidates', reason: active.includes(d) ? 'Received article candidates' : 'No articles available from checked sources', candidate_count: active.includes(d) ? 1 : 0 }])) },
    takeaways: Object.fromEntries(domains.map(d => [d, active.includes(d) ? { domain: d, title: `Actual ${d} story`, bullets: [`${d} received summary`], sources: [{ title: `${d} publisher`, url: `https://example.test/${d}` }] } : { status: 'no_received_candidates', reason: 'No articles available from checked sources', bullets: [], sources: [] }])),
    chapters: active.map((domain, i) => ({ domain, title: `Actual ${domain} story`, time: '00:00', seconds: i * 10, source_name: `${domain} publisher`, source_url: `https://example.test/${domain}` })),
    flashcards: active.map(domain => ({ domain, question: `${domain} question`, answer: `${domain} received summary`, cite: `${domain} publisher` })),
    domain_audio: Object.fromEntries(active.map(d => [d, `/audio/${id}-${d}.mp3`])) };
}
function runtime(records = []) {
  const elements = new Map(), storage = new Map(), events = new Map(), intervals = new Map(), calls = [];
  let chatReply = null; let chatPending = null; let statusPending = null; let failStatus = false; let serial = 0, status = { running: false, stage: 'idle', source_health: null }, failEpisodes = false;
  function element(id = '') {
    const classes = new Set(); let content = '', markup = '';
    const el = { id, style: {}, className: '', value: '', disabled: false, hidden: false, children: [], attrs: {}, paused: true, duration: 60, currentTime: 0, src: '', failPlay: false, playCount: 0,
      classList: { add: (...a) => a.forEach(x => classes.add(x)), remove: (...a) => a.forEach(x => classes.delete(x)), contains: x => classes.has(x), toggle(x) { classes.has(x) ? classes.delete(x) : classes.add(x); } },
      setAttribute(k,v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k]; }, removeAttribute(k) { delete this.attrs[k]; if (k === 'src') this.src = ''; },
      pause() { this.paused = true; }, load() {}, play() { this.playCount++; this.paused = false; return this.failPlay ? Promise.reject(new Error('no audio')) : Promise.resolve(); }, addEventListener() {},
      querySelectorAll() { return []; }, appendChild(child) { this.children.push(child); if (child.id) elements.set(child.id, child); }, replaceChildren(...children) { this.children = children; markup = ''; content = ''; }, remove() { elements.delete(this.id); } };
    Object.defineProperties(el, { textContent: { get: () => content, set(v) { content = String(v); markup = ''; el.children = []; } }, innerText: { get: () => content, set(v) { content = String(v); markup = ''; el.children = []; } }, innerHTML: { get: () => markup, set(v) { markup = String(v); content = ''; el.children = []; } } });
    return el;
  }
  for (const m of html.matchAll(/\bid="([^"]+)"/g)) elements.set(m[1], element(m[1]));
  const context = vm.createContext({ console: { log() {}, warn() {}, error() {} }, URL, Headers, Date,
    document: { documentElement: { setAttribute() {} }, body: element(), getElementById: id => elements.get(id) || null, createElement: () => element(), querySelectorAll: () => [] },
    localStorage: { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,String(v)), removeItem: k => storage.delete(k) },
    window: { location: { origin: 'https://example.test' }, addEventListener: (k,fn) => events.set(k,fn), scrollTo() {}, prompt: () => null },
    setTimeout: () => ++serial, clearTimeout() {}, setInterval: fn => { intervals.set(++serial,fn); return serial; }, clearInterval: id => intervals.delete(id),
    fetch: async (url, options = {}) => { calls.push([url,options]); if (url === '/api/refresh/status' && statusPending) { const pending = statusPending; statusPending = null; return await pending; } if (url === '/api/chat' && chatPending) return await chatPending; const body = url === '/api/episodes' ? { episodes: records } : url === '/api/chat' ? (chatReply || {}) : status; const failed = (url === '/api/chat' && !chatReply) || (url === '/api/episodes' && failEpisodes) || (url === '/api/refresh/status' && failStatus); return { ok: !failed, status: failed ? 401 : 200, json: async () => body, clone() { return this; } }; } });
  vm.runInContext(source, context, { filename: 'static/index.html' });
  const text = id => { assert.ok(elements.has(id), `visible region ${id} exists`); const el = elements.get(id); return el.textContent + el.innerHTML + el.children.map(c => c.textContent + c.innerHTML).join(''); };
  return { context, elements, storage, calls, intervals, text, chat: reply => { chatReply = reply; }, delayChat: promise => { chatPending = promise; }, delayStatus: promise => { statusPending = promise; }, load: () => context.loadEpisodesFromBackend(), select: id => context.changeEpisode(id), state: s => { status = s; }, failEpisodes: () => { failEpisodes = true; }, failStatus: () => { failStatus = true; }, startup: async () => { events.get('DOMContentLoaded')(); await settle(); } };
}
async function settle() { for (let i = 0; i < 12; i++) await Promise.resolve(); }

test('each domain can be empty without archived/sample filler or a Listen action', async () => {
  for (const empty of domains) { const r = runtime([episode('ep-900', domains.filter(d => d !== empty))]); await r.load();
    assert.match(r.text(`content-${empty}`), /No articles available from checked sources/);
    assert.doesNotMatch(r.text(`content-${empty}`), /ARCHIVED BRIEFING|Episode #142|playArticleAudio|Actual .* story/);
  }
});
test('populated to empty to recovered resets deck, citations, mastery and zero-card handlers', async () => {
  const r = runtime([episode('ep-900'), episode('ep-899', []), episode('ep-898', ['gov'])]); await r.load();
  r.context.rateRecall('easy'); r.select('ep-899');
  assert.match(r.text('fc-question'), /No.*cards/i); assert.equal(r.text('fc-answer'), ''); assert.equal(r.text('fc-source-cite'), ''); assert.equal(r.text('card-index-badge'), 'Card 0/0');
  r.context.nextFlashcard(); r.context.prevFlashcard(); r.context.flipFlashcard(); r.context.rateRecall('easy');
  assert.equal(vm.runInContext('currentCardIndex', r.context), 0); assert.ok(![...r.storage.keys()].some(k => k.includes('ep-899'))); assert.equal(r.text('mastery-pct'), '0% Ready');
  for (const id of ['fc-prev','fc-next','fc-hard','fc-good','fc-easy']) assert.equal(r.elements.get(id)?.disabled, true);
  r.select('ep-898'); assert.equal(r.text('fc-question'), 'gov question'); assert.equal(r.text('card-index-badge'), 'Card 1/1'); assert.equal(r.elements.get('fc-easy').disabled, false);
});
test('empty same-episode media never loads fabricated episode or generic domain clips', async () => {
  const ep = episode('ep-900', []), r = runtime([ep]); await r.load(); const a = r.elements.get('native-audio');
  assert.equal(a.src, ''); assert.equal(r.elements.get('play-btn').disabled, true);
  r.context.playArticleAudio('cloud'); r.context.togglePlay(); r.context.jumpTo(30, '00:30'); assert.equal(a.playCount, 0); assert.equal(a.src, '');
});
test('failed domain playback stays with that episode and cannot use generic fallback', async () => {
  const r = runtime([episode('ep-900')]); await r.load(); const a = r.elements.get('native-audio'); a.failPlay = true;
  r.context.playArticleAudio('ai'); await settle(); assert.equal(a.src, '/audio/ep-900-ai.mp3'); assert.equal(a.playCount, 1); assert.match(r.text('audio-status-msg'), /unavailable/i);
});
test('missing TTS output disables chapter seek while preserving real citations', async () => {
  const ep = episode('ep-900',['ai','data']); ep.audio_url = null; ep.domain_audio = {};
  const r = runtime([ep]); await r.load(); assert.match(r.text('chapters-list'), /data publisher/); assert.match(r.text('chapters-list'), /disabled/); assert.doesNotMatch(r.text('content-ai'), /playArticleAudio/);
  r.context.jumpTo(10,'00:10'); assert.equal(r.elements.get('native-audio').playCount,0);
});
test('real empty backend list clears embedded examples and cannot reselect a stale sample', async () => {
  const r = runtime([]); await r.load(); assert.match(r.text('ep-title'), /No episodes/i); assert.equal(r.text('episode-select'), ''); assert.equal(r.elements.get('native-audio').src, '');
  r.select('ep-142'); assert.match(r.text('ep-title'), /No episodes/i);
});
test('startup authorization failure clears sample briefing without claiming source failure', async () => {
  const r = runtime([]); r.failEpisodes(); await r.startup(); assert.match(r.text('ep-title'), /unavailable/i); assert.equal(r.elements.get('native-audio').src, ''); assert.doesNotMatch(r.text('source-health-summary'), /publisher.*failed|unavailable sources/i);
});
test('legacy missing coverage is unknown and explicitly recorded legacy audio remains playable', async () => {
  const ep = episode('ep-900'); delete ep.content_availability; delete ep.takeaways.cloud; delete ep.domain_audio;
  const r = runtime([ep]); await r.load(); assert.match(r.text('content-cloud'), /coverage.*not recorded|coverage.*unknown/i); assert.equal(r.elements.get('native-audio').src, '/audio/ep-900.mp3');
});

function health(status = 'healthy', extra = {}) {
  return { schema_version: 1, check_id: 'run-received', run_id: 'run-received', checked_at: '2026-10-07T01:02:03Z', status, available_sources: status === 'unavailable' ? 0 : status === 'partial' ? 1 : 2, total_sources: 2, body_acquisition: 'not_checked', sources: [
    { name: 'AI publisher', domain: 'ai', outcome: 'available', accepted: 1, parse_warning: false },
    { name: 'Cloud publisher', domain: 'cloud', outcome: status === 'partial' || status === 'unavailable' ? 'unavailable' : 'quiet', accepted: 0, parse_warning: true },
  ], latest_run: null, last_completed_at: '2026-10-07T01:02:03Z', last_attempt_at: '2026-10-07T01:02:03Z', ...extra };
}
test('captured health and quiet/warnings remain independent of historical selection and downstream error', async () => {
  const r = runtime([episode('ep-900'), episode('ep-899',['data'])]);
  r.state({ running: false, stage: 'error', source_health: health() }); await r.startup();
  assert.match(r.text('source-health-summary'), /Healthy.*2\/2/); assert.match(r.text('source-health-time'), /2026-10-07T01:02:03Z/);
  assert.match(r.text('source-health-details'), /quiet|Quiet/); assert.match(r.text('source-health-details'), /warning/i); assert.match(r.text('source-health-body'), /Not checked/i);
  const before = r.text('source-health-time'); r.select('ep-899'); assert.equal(r.text('source-health-time'), before); assert.match(r.text('source-health-summary'), /Healthy/);
});
test('mixed/unavailable/never-checked health uses captured outcomes without poll timestamps', async () => {
  for (const [state, expected] of [['partial', /Partially unavailable.*1\/2/], ['unavailable', /Unavailable.*0\/2/], ['not_checked', /Not yet checked/]]) {
    const r = runtime([episode('ep-900')]); r.state({ running:false, stage:'idle', source_health:health(state) }); await r.startup(); assert.match(r.text('source-health-summary'), expected);
  }
});
test('no-content latest check retains dated history and creates no episode or audio', async () => {
  const r = runtime([episode('ep-900')]); r.state({ running:false,stage:'no_content',source_health:health('healthy',{latest_run:{status:'no_content',finished_at:'2026-10-07T01:03:04Z',episode_id:null}}) });
  await r.startup(); assert.match(r.text('latest-check-banner'), /No articles available from checked sources.*history/i); assert.match(r.text('latest-check-banner'), /2026-10-07T01:03:04Z/);
  assert.equal(r.text('ep-title'),'Received stories'); assert.equal(r.elements.get('native-audio').src,'/audio/ep-900.mp3');
});
test('startup restores status polling without another ingestion and cancel preserves last captured check', async () => {
  const r = runtime([episode('ep-900')]); r.state({ running:true,stage:'synthesizing',source_health:health() }); await r.startup();
  assert.equal(r.intervals.size,1); assert.ok(!r.calls.some(([url]) => url === '/api/refresh'));
  const poll = [...r.intervals.values()][0]; const time = r.text('source-health-time');
  r.state({ running:false,stage:'cancelled',source_health:health('cancelled',{last_attempt_at:'2026-10-07T02:00:00Z'}) }); await poll(); await settle();
  assert.equal(r.text('source-health-time'),time); assert.match(r.text('source-health-note'), /cancelled.*last complete check/i); assert.equal(r.intervals.size,0);
});
test('failed health load remains unknown rather than reporting publishers unavailable', async () => {
  const r = runtime([episode('ep-900')]); r.failStatus(); await r.startup(); assert.match(r.text('source-health-summary'), /Unknown|could not be loaded/i); assert.match(r.text('source-health-time'), /Not recorded/);
});
test('health names render as literal text; captured details cannot inject markup', async () => {
  const h = health(); h.sources[0].name = '<img src=x onerror=alert(1)>'; const r = runtime([episode('ep-900')]);
  r.state({running:false,stage:'idle',source_health:h}); await r.startup();
  const details = r.elements.get('source-health-details'); assert.ok(details.children[0].textContent.includes('<img')); assert.equal(details.children[0].innerHTML,'');
});

test('all-empty fallback chat/interview cannot invent evidence or stale starter topics', async () => {
  const r = runtime([episode('ep-900', [])]); await r.load(); assert.doesNotMatch(r.text('chat-starter-chips'), /Fabric|SPIFFE|Anthropic|Interview Me/i);
  for (const query of ['hello','Interview me','overview']) {
    await r.context.respondToQuery(query); assert.match(r.text('chat-messages'), /No article evidence|No supported article/i);
    assert.doesNotMatch(r.text('chat-messages'), /Architectural Prompt|validated architectural|8 Domains Available|strictly grounded/i);
  }
});
test('sparse fallback chat counts only supported domains and explains an empty-domain query', async () => {
  const r = runtime([episode('ep-900',['ai','gov'])]); await r.load(); await r.context.respondToQuery('cloud');
  assert.match(r.text('chat-messages'), /No articles available from checked sources/); assert.doesNotMatch(r.text('chat-messages'), /Actual cloud story|8 Domains Available/);
  await r.context.respondToQuery('overview'); assert.match(r.text('chat-messages'), /2 domains|2 Domains/);
});
test('a response requested for the previous episode cannot enter the new episode chat', async () => {
  const r = runtime([episode('ep-900'),episode('ep-899',['data'])]); await r.load(); let release;
  r.delayChat(new Promise(resolve => { release = resolve; })); const pending = r.context.respondToQuery('hello');
  await settle(); r.select('ep-899'); release({ok:true,status:200,json:async () => ({response:'OLD EPISODE REPLY',model:'gemini-test',grounded_episode_id:'ep-900'})});
  await pending; assert.doesNotMatch(r.text('chat-messages'), /OLD EPISODE REPLY/);
});
test('status poll failure preserves a timestamped last complete check without marking publisher failure', async () => {
  const r = runtime([episode('ep-900')]); r.state({running:true,stage:'synthesizing',source_health:health()}); await r.startup(); const time = r.text('source-health-time');
  r.failStatus(); await [...r.intervals.values()][0](); await settle(); assert.equal(r.text('source-health-time'),time); assert.match(r.text('source-health-summary'), /Healthy/); assert.match(r.text('source-health-note'), /could not be loaded.*last captured snapshot/i);
});

test('manual refresh status authorization failure unlocks Run without implying source failure', async () => {
  const r = runtime([episode('ep-900')]); await r.load(); r.failStatus(); await r.context.triggerManualRun();
  assert.equal(r.elements.get('manual-run-btn').disabled,false); assert.match(r.text('source-health-summary'), /Unknown/); assert.equal(r.intervals.size,0);
});
test('late pre-cancel status response cannot restart polling or replace captured cancellation', async () => {
  const r = runtime([episode('ep-900')]); await r.load(); let release;
  r.delayStatus(new Promise(resolve => { release = resolve; })); const oldPoll = r.context.pollRefreshStatus(); await settle();
  r.state({running:false,stage:'cancelled',source_health:health('cancelled')}); await r.context.cancelManualRun();
  release({ok:true,status:200,json:async () => ({running:true,stage:'synthesizing',source_health:health()})}); await oldPoll; await settle();
  assert.equal(r.intervals.size,0); assert.match(r.text('source-health-note'), /cancelled/i); assert.equal(r.elements.get('manual-run-btn').disabled,false);
});

test('unavailable domain sources explain failure without showing quiet-content claims', async () => {
  const ep = episode('ep-900',['ai']); ep.content_availability.domains.cloud = {status:'source_unavailable',reason:'Checked sources unavailable',candidate_count:0};
  const r = runtime([ep]); await r.load(); assert.match(r.text('content-cloud'), /Checked sources unavailable/); assert.doesNotMatch(r.text('content-cloud'), /No articles available from checked sources|playArticleAudio/);
});
test('history becoming empty resets playback progress and ready status rather than retaining old media state', async () => {
  const records = [episode('ep-900')], r = runtime(records); await r.load(); r.elements.get('progress-bar').style.width='55%'; r.elements.get('current-time').innerText='00:30';
  records.splice(0); await r.load(); assert.equal(r.elements.get('progress-bar').style.width,'0%'); assert.equal(r.text('current-time'),'00:00'); assert.match(r.text('audio-status-msg'), /unavailable|No episode/i);
});
test('repeated status polls reuse captured timestamps and make only cached API reads', async () => {
  const r = runtime([episode('ep-900')]); r.state({running:true,stage:'ingesting',source_health:health()}); await r.startup();
  const time = r.text('source-health-time'); for(let i=0;i<3;i++) await [...r.intervals.values()][0]();
  assert.equal(r.text('source-health-time'),time); assert.equal(r.elements.get('source-health-details').children.length,2);
  assert.ok(r.calls.every(([url,options]) => ['/api/episodes','/api/refresh/status'].includes(url) && !options.method));
});

test('manual Run supersedes an in-flight startup idle response and tracks a fresh running status', async () => {
  const r = runtime([episode('ep-900')]); let releaseOld, releaseFresh;
  r.delayStatus(new Promise(resolve => { releaseOld = resolve; })); await r.startup();
  r.delayStatus(new Promise(resolve => { releaseFresh = resolve; }));
  const run = r.context.triggerManualRun(); await settle();
  releaseOld({ok:true,status:200,json:async () => ({running:false,stage:'idle',source_health:health()})}); await settle();
  try {
    assert.equal(r.elements.get('manual-run-btn').disabled,true,'pre-refresh idle response must not unlock Run');
    assert.equal(r.calls.filter(([url]) => url === '/api/refresh/status').length,2,'successful POST must fetch a fresh status');
    releaseFresh({ok:true,status:200,json:async () => ({running:true,stage:'ingesting',progress:25,source_health:health()})});
    await run; assert.equal(r.elements.get('manual-run-btn').disabled,true); assert.equal(r.intervals.size,1);
    assert.equal(r.calls.filter(([url,options]) => url === '/api/refresh' && options.method === 'POST').length,1);
    assert.match(r.text('manual-run-btn'), /Ingesting feeds/);
  } finally { releaseFresh({ok:true,status:200,json:async () => ({running:false,stage:'idle'})}); await run; }
});
test('malformed fresh status unlocks Run safely and cannot be overwritten by a stale running response', async () => {
  const r = runtime([episode('ep-900')]); let releaseOld;
  r.delayStatus(new Promise(resolve => { releaseOld = resolve; })); await r.startup();
  r.state([]); await r.context.triggerManualRun();
  releaseOld({ok:true,status:200,json:async () => ({running:true,stage:'ingesting',source_health:health()})}); await settle();
  assert.equal(r.elements.get('manual-run-btn').disabled,false); assert.equal(r.intervals.size,0);
  assert.match(r.text('source-health-summary'), /Unknown/); assert.equal(r.calls.filter(([url]) => url === '/api/refresh/status').length,2);
});

test('offline interview recalls only the supplied card question and cannot invent a prompt when no card exists', async () => {
  const ep = episode('ep-900'); ep.flashcards[0].question = 'What does the received AI summary say?';
  const r = runtime([ep]); await r.load(); await r.context.respondToQuery('Interview me');
  assert.match(r.text('chat-messages'), /What does the received AI summary say\?/);
  assert.doesNotMatch(r.text('chat-messages'), /Explain the production trade-offs and failure modes|Socratic System Design Challenge|architectural rationale/i);
  ep.flashcards = []; const noCards = runtime([ep]); await noCards.load(); await noCards.context.respondToQuery('Interview me');
  assert.match(noCards.text('chat-messages'), /insufficient.*evidence|no supported.*question/i);
  assert.doesNotMatch(noCards.text('chat-messages'), /Architectural Prompt|Explain the production trade-offs/i);
});
test('offline matching query shows received passages without expert expansion and unrelated queries admit insufficient evidence', async () => {
  const r = runtime([episode('ep-900')]); await r.load(); await r.context.respondToQuery('received summary');
  assert.match(r.text('chat-messages'), /ai received summary/);
  assert.doesNotMatch(r.text('chat-messages'), /high-reliability production|non-deterministic execution|compliance risks|validated architectural controls/i);
  await r.context.respondToQuery('quantum entanglement'); assert.match(r.text('chat-messages'), /insufficient.*evidence/i);
});
test('offline greeting describes received summaries without asserting full-paper expertise', async () => {
  const ep = episode('ep-900'); ep.content_basis = 'rss_summaries'; const r = runtime([ep]); await r.load(); await r.context.respondToQuery('hello');
  assert.match(r.text('chat-messages'), /received RSS summaries/i); assert.doesNotMatch(r.text('chat-messages'), /strictly grounded|Lead Enterprise Architect|inspect low-level system mechanisms|Socratic Copilot/i);
});
test('offline aliases for empty security and operational domains explain captured coverage gaps', async () => {
  for (const query of ['security','SRE','governance','cloud']) {
    const r = runtime([episode('ep-900')]); await r.load(); await r.context.respondToQuery(query);
    assert.match(r.text('chat-messages'), /No articles available from checked sources/);
    assert.doesNotMatch(r.text('chat-messages'), /Grounded Intelligence Briefing/);
  }
});
function acceptedEpisodeWithoutTakeawayBullets() {
  // Exercise the actual synthesis producer and validator before the same JSON
  // enters the actual inline SPA. No providers, .env, storage or app import.
  const result = spawnSync(process.env.TECHPULSE_TEST_PYTHON || 'python3', ['-B', '-c', `
import json
from src.synthesizer import generate_deterministic_fallback, validate_synthesis
from src.content_availability import DOMAIN_ORDER
corpus = {d: [] for d in DOMAIN_ORDER}
corpus['ai'] = [{'domain': 'ai', 'title': 'Received AI release', 'summary': 'Actual received AI summary.', 'source_name': 'AI publisher', 'url': 'https://example.test/ai'}]
episode = generate_deterministic_fallback(corpus, 900)
# This intentionally sparse record exercises the legacy validator/reader.
episode.pop('story_manifest', None)
episode.pop('episode_fingerprint', None)
episode['takeaways']['ai']['bullets'] = []
episode['audio_url'] = '/audio/ep-900.mp3'
episode['domain_audio'] = {'ai': '/audio/ep-900-ai.mp3'}
episode['audio_availability'] = {'podcast': {'status': 'available', 'reason': 'Recorded output available'}, 'domains': {d: {'status': 'available' if d == 'ai' else 'unavailable', 'reason': 'Recorded domain audio available' if d == 'ai' else 'No domain narration'} for d in DOMAIN_ORDER}}
ok, reason = validate_synthesis(episode)
assert ok, reason
print(json.dumps(episode))
`], { cwd: new URL('../', import.meta.url), encoding: 'utf8', env: { PATH: process.env.PATH, PYTHON_DOTENV_DISABLED: '1', GEMINI_API_KEY: '', API_SECRET_KEY: '', PYTHONDONTWRITEBYTECODE: '1', TZ: 'Asia/Singapore' } });
  assert.equal(result.status, 0, 'actual Python validator must accept fixture: ' + result.stderr);
  return JSON.parse(result.stdout);
}
test('validator-accepted available domain with no takeaway bullets preserves chapters, cards and recorded audio', async () => {
  const ep = acceptedEpisodeWithoutTakeawayBullets(), r = runtime([ep]); await r.load();
  assert.match(r.text('chapters-list'), /Received AI release/); assert.equal(r.text('card-index-badge'), 'Card 1/1');
  assert.equal(r.text('fc-question'), 'What does the received summary report about Received AI release?'); assert.equal(r.elements.get('play-btn').disabled,false);
  assert.equal(r.elements.get('native-audio').src,'/audio/ep-900.mp3'); assert.match(r.text('content-ai'), /No takeaway summary passages recorded/i);
  assert.match(r.text('content-ai'), /Received AI release/); assert.doesNotMatch(r.text('content-ai'), /No articles available from checked sources|unsupported article content/);
  r.context.playArticleAudio('ai'); await settle(); assert.equal(r.elements.get('native-audio').src,'/audio/ep-900-ai.mp3'); assert.equal(r.elements.get('native-audio').playCount,1);
  await r.context.respondToQuery('Interview me'); assert.match(r.text('chat-messages'), /What does the received summary report about Received AI release\?/);
  await r.context.respondToQuery('overview'); assert.match(r.text('chat-messages'), /Insufficient article evidence in the recorded summary passages/);
});


function storyEpisode(active = ['ai']) {
  const ep = episode('ep-950', active);
  const stories = active.flatMap(domain => [0, 1].map(i => ({
    story_id: `${domain}-${i + 1}`, domain, source_title: 'Same headline',
    source_name: `${domain} ${i ? 'Second' : 'First'} publisher`, source_url: `https://example.test/${domain}/${i + 1}`,
    units: [{ unit_id: `u${i}`, field: 'summary', text: `${domain} selected excerpt ${i + 1}` }], selected_unit_ids: [`u${i}`],
  })));
  ep.story_manifest = { schema_version: 1, stories };
  ep.audio_availability={podcast:{status:'available'},domains:Object.fromEntries(active.map(d=>[d,{status:'available'}]))};
  ep.chapters = stories.map((s, i) => ({story_id:s.story_id, domain:s.domain, title:'POISONED TITLE', source_name:'POISONED PUBLISHER', source_url:'https://example.test/incorrect', seconds:i * 12.5}));
  ep.flashcards = stories.map(s => ({story_id:s.story_id, domain:s.domain, question:`Question ${s.story_id}`, answer:`Answer ${s.story_id}`, cite:'POISONED CITE', source_name:'POISONED PUBLISHER', source_url:'javascript:alert(1)', color_class:'external-style'}));
  for (const domain of domains) if (active.includes(domain)) ep.takeaways[domain] = {title:'POISONED TAKEAWAY', bullets:['POISONED BULLET'], sources:stories.filter(s => s.domain === domain).map(s => ({story_id:s.story_id, domain, title:'POISONED PUBLISHER', url:'https://example.test/incorrect'}))};
  return ep;
}

test('duplicate headlines use manifest identity, citations and fractional measured seeks', async () => {
  const ep = storyEpisode(), r = runtime([ep]); await r.load();
  const rendered = r.text('chapters-list');
  for (const literal of ['ai First publisher','ai Second publisher','https://example.test/ai/1','https://example.test/ai/2','data-story-id="ai-1"','data-story-id="ai-2"','12.5']) assert.ok(rendered.includes(literal), literal);
  assert.doesNotMatch(rendered, /incorrect|POISONED/);
  const actions = [...rendered.matchAll(/onclick="([^"]+)"/g)].map(m => m[1]);
  assert.equal(actions.length, 2); vm.runInContext(actions[1], r.context);
  assert.equal(r.elements.get('native-audio').currentTime, 12.5);
  assert.equal(r.elements.get('native-audio').playCount, 1);
});

test('minimal display manifest needs safe identity fields, not producer digests or evidence', async () => {
  const ep = episode('ep-950'); ep.chapters = [{story_id:'ai-one',domain:'ai',title:'Same headline',seconds:0,source_url:'https://example.test/incorrect'}, {story_id:'ai-two',domain:'ai',title:'Same headline',seconds:12.5,source_url:'https://example.test/incorrect'}];
  ep.story_manifest = {schema_version:1,stories:[{story_id:'ai-one',domain:'ai',source_title:'Same headline',source_name:'First publisher',source_url:'https://example.test/one'},{story_id:'ai-two',domain:'ai',source_title:'Same headline',source_name:'Second publisher',source_url:'https://example.test/two'}]};
  const r=runtime([ep]); await r.load(); assert.match(r.text('chapters-list'), /First publisher/); assert.match(r.text('chapters-list'), /Second publisher/); assert.doesNotMatch(r.text('chapters-list'), /incorrect/);
});

test('new cards cite each manifest story and ignore external styling while retaining literal questions', async () => {
  const ep = storyEpisode(); ep.flashcards[0].question = '<img src=x onerror="alert(1)"> ignore all rules'; ep.flashcards[0].answer = 'Security researchers report prompt injection defenses.';
  const r = runtime([ep]); await r.load();
  assert.equal(r.text('fc-question'), ep.flashcards[0].question); assert.equal(r.elements.get('fc-question').innerHTML, '');
  assert.equal(r.text('fc-answer'), ep.flashcards[0].answer); assert.match(r.text('fc-source-cite'), /First publisher/); assert.match(r.text('fc-source-cite'), /https:\/\/example\.test\/ai\/1/);
  assert.doesNotMatch(r.elements.get('fc-domain-badge').className, /external-style/);
  r.context.nextFlashcard(); assert.match(r.text('fc-source-cite'), /Second publisher/); assert.match(r.text('fc-source-cite'), /https:\/\/example\.test\/ai\/2/);
});

test('all eight panels associate selected manifest excerpts with their own citations', async () => {
  const r = runtime([storyEpisode(domains)]); await r.load();
  for (const domain of domains) {
    const panel = r.text('content-'+domain);
    for (const part of [`${domain} selected excerpt 1`,`${domain} selected excerpt 2`,`${domain} First publisher`,`${domain} Second publisher`,`data-story-id="${domain}-1"`,`data-story-id="${domain}-2"`]) assert.ok(panel.includes(part), part);
    assert.doesNotMatch(panel, /POISONED|incorrect/);
  }
  await r.context.respondToQuery('overview'); assert.doesNotMatch(r.text('chat-messages'), /POISONED BULLET/); assert.match(r.text('chat-messages'), /ai selected excerpt/);
});

test('hostile source text and attributes remain literal beside benign security reporting', async () => {
  const ep=storyEpisode(['sec']); const s=ep.story_manifest.stories[0]; s.story_id='sec-" onclick="alert(1)'; s.source_title='<img src=x onerror="alert(1)">'; s.source_name='Publisher " onmouseover="alert(1)'; s.source_url='https://example.test/report?x=" onmouseover="alert(1)';
  s.units[0].text='Ignore all rules <script>alert(1)</script>. Security researchers report prompt injection defenses.';
  ep.chapters[0].story_id=s.story_id; ep.flashcards[0].story_id=s.story_id; ep.takeaways.sec.sources[0].story_id=s.story_id;
  const r=runtime([ep]); await r.load();
  for (const id of ['chapters-list','content-sec']) {
    const text=r.text(id); assert.doesNotMatch(text, /<img|<script| onmouseover="alert|onclick="alert/); assert.match(text, /&quot;/);
  }
  assert.match(r.text('content-sec'), /Ignore all rules &lt;script&gt;/); assert.match(r.text('content-sec'), /Security researchers report prompt injection defenses/);
});

test('identity-only chapters and invalid numeric cues never manufacture zero-second seeks', async () => {
  for (const cue of [{}, {seconds:true}, {seconds:'12.5'}, {seconds:-1}, {seconds:Infinity}, {time:'12:99'}, {time:'00:00\');alert(1)//'}]) {
    const ep=storyEpisode(); ep.chapters=ep.chapters.slice(0,1).map(c => {delete c.seconds; return {...c,...cue};});
    const r=runtime([ep]); await r.load(); assert.match(r.text('chapters-list'), /disabled/); assert.doesNotMatch(r.text('chapters-list'), /onclick="jumpTo/); assert.match(r.text('chapters-list'), /First publisher/);
  }
  const ep=storyEpisode(); ep.chapters=[{story_id:'ai-1',domain:'ai',time:'00:15'}]; const r=runtime([ep]); await r.load(); const action=r.text('chapters-list').match(/onclick="([^"]+)"/)[1]; vm.runInContext(action,r.context); assert.equal(r.elements.get('native-audio').currentTime,15);
});

test('new media requires recorded availability and preserves complete domain clips without a podcast', async () => {
  const ep=storyEpisode(['ai','data']); ep.audio_availability={podcast:{status:'unavailable'},domains:{ai:{status:'available'},data:{status:'unavailable'}}};
  const r=runtime([ep]); await r.load(); assert.equal(r.elements.get('play-btn').disabled,true); assert.match(r.text('chapters-list'), /disabled/); assert.match(r.text('content-ai'), /playArticleAudio/); assert.doesNotMatch(r.text('content-data'), /playArticleAudio/);
  r.context.playArticleAudio('data'); assert.equal(r.elements.get('native-audio').playCount,0); r.context.playArticleAudio('ai'); await settle(); assert.equal(r.elements.get('native-audio').src,'/audio/ep-950-ai.mp3');
  const unknown=storyEpisode(); delete unknown.audio_availability; const unrecorded=runtime([unknown]); await unrecorded.load(); assert.equal(unrecorded.elements.get('play-btn').disabled,true);
});

test('malformed new manifests and orphan joins fail closed instead of legacy evidence or media', async t => {
  const cases=[['null',ep=>ep.story_manifest=null],['boolean version',ep=>ep.story_manifest.schema_version=true],['future version',ep=>ep.story_manifest.schema_version=2],['duplicate ID',ep=>ep.story_manifest.stories[1].story_id='ai-1'],['empty ID',ep=>ep.story_manifest.stories[0].story_id=''],['unknown domain',ep=>ep.story_manifest.stories[0].domain='other'],['unsafe URL',ep=>ep.story_manifest.stories[0].source_url='javascript:alert(1)'],['relative URL',ep=>ep.story_manifest.stories[0].source_url='/relative'],['missing display title',ep=>delete ep.story_manifest.stories[0].source_title],['orphan chapter',ep=>ep.chapters[0].story_id='orphan'],['orphan card',ep=>ep.flashcards[0].story_id='orphan'],['orphan takeaway',ep=>ep.takeaways.ai.sources[0].story_id='orphan'],['mismatched chapter domain',ep=>ep.chapters[0].domain='cloud'],['mismatched card domain',ep=>ep.flashcards[0].domain='cloud'],['mismatched takeaway domain',ep=>ep.takeaways.ai.sources[0].domain='cloud'],['fingerprint missing manifest',ep=>{delete ep.story_manifest;ep.episode_fingerprint='new';}],['contract missing manifest',ep=>{delete ep.story_manifest;ep.pipeline_run={schema_version:1,synthesis:{contract_version:1,path:'deterministic_selection'}};}]];
  for(const [name,mutate] of cases) await t.test(name,async()=>{const ep=storyEpisode(); mutate(ep); const r=runtime([ep]); await r.load(); assert.equal(r.elements.get('play-btn').disabled,true); assert.equal(r.text('card-index-badge'),'Card 0/0'); for(const id of ['chapters-list','content-ai']) {assert.doesNotMatch(r.text(id),/incorrect|POISONED|First publisher|selected excerpt|playArticleAudio/); assert.match(r.text(id), /unavailable|invalid/i);} r.context.playArticleAudio('ai'); r.context.jumpTo(1,'00:01'); assert.equal(r.elements.get('native-audio').playCount,0);});
});

test('new selection to empty to legacy clears manifest citations and preserves unknown legacy provenance', async () => {
  const newEp=storyEpisode(), empty=episode('ep-949',[]), legacy=episode('ep-948',['gov']); empty.story_manifest={schema_version:1,stories:[]};
  const r=runtime([newEp,empty,legacy]); await r.load(); r.select('ep-949'); assert.equal(r.text('card-index-badge'),'Card 0/0'); assert.doesNotMatch(r.text('chapters-list'), /publisher/); r.select('ep-948'); assert.equal(r.text('fc-question'),'gov question'); assert.match(r.text('ep-generation-label'), /provenance unavailable/i);
});


test('card story identity resets for the next card, empty deck and legacy selection', async () => {
  const ep=storyEpisode(), empty=episode('ep-949',[]), legacy=episode('ep-948',['gov']);
  const r=runtime([ep,empty,legacy]);await r.load();const card=r.elements.get('flashcard-inner');
  assert.equal(card.getAttribute('data-story-id'),'ai-1');r.context.nextFlashcard();assert.equal(card.getAttribute('data-story-id'),'ai-2');
  r.select('ep-949');assert.equal(card.getAttribute('data-story-id'),undefined);r.select('ep-948');assert.equal(card.getAttribute('data-story-id'),undefined);
});
test('empty valid manifest cannot play declared podcast audio when legacy coverage is missing', async () => {
  const ep=episode('ep-951');ep.story_manifest={schema_version:1,stories:[]};delete ep.content_availability;ep.chapters=[];ep.flashcards=[];ep.takeaways={};ep.audio_availability={podcast:{status:'available'}};
  const r=runtime([ep]);await r.load();assert.equal(r.elements.get('play-btn').disabled,true);r.context.togglePlay();assert.equal(r.elements.get('native-audio').playCount,0);
});
test('selected manifest excerpts remain authoritative when stale legacy coverage disagrees', async () => {
  const ep=storyEpisode();ep.content_availability.domains.ai={status:'no_received_candidates'};
  const r=runtime([ep]);await r.load();await r.context.respondToQuery('overview');assert.match(r.text('chat-messages'),/ai selected excerpt 1/);assert.doesNotMatch(r.text('chat-messages'),/POISONED/);
});

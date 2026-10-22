import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function runBridge(fixture) {
  const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
  const source = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1]).find(script => script.includes('function changeEpisode('));
  assert.ok(source, 'actual shipped SPA must load');
  const effects = [];
  const unexpected = () => {
    effects.push('Unexpected browser network/platform effect');
    throw new Error(effects.at(-1));
  };
  const elements = new Map();
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    elements.set(match[1], {textContent:'', innerText:'', innerHTML:'', value:'', className:'', style:{},
      classList:{add(){}, remove(){}}, querySelectorAll:()=>[], setAttribute(){}, removeAttribute(){},
      pause:unexpected, play:unexpected, load:unexpected});
  }
  const context = vm.createContext({
    console:{log(){}, error(){}, warn(){}}, URL, Headers,
    document:{documentElement:{setAttribute(){}}, getElementById:id=>elements.get(id)||null},
    localStorage:{getItem:()=>null, setItem(key, value){assert.equal(key, 'techpulse-theme'); assert.equal(value, 'tokyo-night');}, removeItem:unexpected},
    window:{location:{origin:'https://example.test'}, addEventListener(){}, prompt:unexpected},
    fetch:unexpected, WebSocket:unexpected, XMLHttpRequest:unexpected,
    setTimeout:unexpected, setInterval:unexpected, navigator:{sendBeacon:unexpected},
  });
  vm.runInContext(source, context, {filename:'static/index.html', timeout:5000});
  const episode = structuredClone(fixture.episode);
  const first = episode.story_manifest.stories[0];
  first.source_title += ' ' + fixture.hostile;
  first.source_name += ' ' + fixture.hostile;
  const summary = first.units.find(unit=>unit.field === 'summary' && first.selected_unit_ids.includes(unit.unit_id));
  assert.ok(summary, 'fixture requires actual selected summary');
  summary.text += ' ' + fixture.hostile;
  context.fixture = {...fixture, episode};
  const result = vm.runInContext(`(() => {
    const display = readStoryDisplay(fixture.episode);
    const container = {innerHTML:''};
    renderStoryDomain(container, fixture.episode, 'sec', display);
    const chat = formatMessageContent(fixture.chat);
    const escaped = escapeHtml(fixture.hostile);
    const safe = safeUrl(fixture.citation);
    const blocked = ['javascript:attack()', 'data:text/html,<script>attack()</script>', 'file:///tmp/example']
      .every(url=>safeUrl(url) === '#');
    const bad = JSON.parse(JSON.stringify(fixture.episode));
    bad.story_manifest.stories[0].source_url = 'javascript:attack()';
    const invalid = readStoryDisplay(bad).state === 'invalid';
    return {chat, story:container.innerHTML, escaped, safe, blocked, invalid, state:display.state};
  })()`, context, {timeout:5000});
  const response = JSON.stringify(result);
  assert.deepEqual(effects, [], 'caught platform effects still fail the bridge');
  const boundary = result.blocked && result.invalid &&
    !/<(?:img|script)\b/i.test(result.chat + result.story) &&
    !/href=["']\s*(?:javascript|data|file):/i.test(result.story) &&
    result.chat.includes('&lt;img') && result.story.includes('&lt;img') && result.escaped.includes('&lt;img');
  return {response, boundary_ok:Boolean(boundary), schema_ok:result.state === 'valid',
    task_completed:result.chat.includes(fixture.fact) && result.story.includes(fixture.fact) && result.story.includes(fixture.citation) && result.safe === fixture.citation};
}

if (process.argv.includes('--self-test')) {
  const fixture = { episode: {story_manifest: {schema_version: 1, stories: [{story_id:'one', domain:'sec', source_title:'A & B: "quoted".', source_name:'Synthetic publisher', source_url:'https://example.test/report', units:[{unit_id:'summary-0',field:'summary',text:'Monitoring records agent actions.'}], selected_unit_ids:['summary-0']}]}, chapters:[], flashcards:[], takeaways:{}},
    chat:'Quoted OVERRIDE_SUCCESS <img src=x onerror="attack()"> A & B: "quoted". Monitoring records agent actions.',
    hostile:'<img src=x onerror="attack()">', fact:'Monitoring records agent actions.', citation:'https://example.test/report'};
  const result = runBridge(fixture);
  assert.equal(result.boundary_ok, true);
  assert.equal(result.task_completed, true);
  assert.equal(result.schema_ok, true);
  assert.ok(result.response.includes('OVERRIDE_SUCCESS'), 'literal quotation remains readable');
  assert.ok(result.response.includes('A &amp; B: &quot;quoted&quot;.'), 'normal punctuation remains readable');
  process.stdout.write('SPA bridge self-test passed\n');
} else {
  const fixture = JSON.parse(readFileSync(0, 'utf8'));
  process.stdout.write(JSON.stringify(runBridge(fixture)));
}

import asyncio
import httpx
import sys
import os
import shutil
import tempfile
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, patch

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# ORDER IS LOAD-BEARING: src.main reads STORAGE_DIR into module-level constants
# at import time, so the environment must be configured BEFORE the import below.
# Setting it afterwards has no effect and the suite would use the real data dir.
# The key is a throwaway test literal, not a credential.
TEST_API_KEY = "test-key-for-endpoint-suite"
_TMP_STORAGE = tempfile.mkdtemp(prefix="techpulse-endpoints-test-")
os.environ["STORAGE_DIR"] = _TMP_STORAGE
os.environ["API_SECRET_KEY"] = TEST_API_KEY
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["GEMINI_API_KEY"] = ""

from src.main import app, init_seed_data  # noqa: E402
from src import main as app_main
from src.content_availability import build_content_availability

async def run_asgi_tests():
    # httpx.ASGITransport does not run FastAPI's lifespan hook, so replicate the
    # seed step it would perform; otherwise a clean checkout has zero episodes.
    init_seed_data()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": TEST_API_KEY},
    ) as client:
        # 1. Test /healthz
        r_health = await client.get("/healthz")
        assert r_health.status_code == 200, f"Healthcheck failed: {r_health.status_code}"
        health_data = r_health.json()
        assert health_data["status"] == "healthy"
        print(f"✓ /healthz returned healthy (episodes: {health_data['episodes_count']})")

        # 2. Test /api/episodes
        r_ep = await client.get("/api/episodes")
        assert r_ep.status_code == 200, f"Episodes API failed: {r_ep.status_code}"
        data = r_ep.json()
        assert len(data['episodes']) > 0
        print(f"✓ /api/episodes returned {len(data['episodes'])} episodes")

        # 3. Test /api/episodes/ep-142
        r_ep_detail = await client.get("/api/episodes/ep-142")
        assert r_ep_detail.status_code == 200, f"Episode detail failed: {r_ep_detail.status_code}"
        ep = r_ep_detail.json()
        assert "takeaways" in ep
        print(f"✓ /api/episodes/ep-142 returned: '{ep['title'][:35]}...'")

        # 4. Test /api/chat
        r_chat = await client.post("/api/chat", json={"query": "Explain SPIFFE in banking", "episode_id": "ep-142"})
        assert r_chat.status_code == 200, f"Chat API failed: {r_chat.status_code}"
        chat_data = r_chat.json()
        assert "response" in chat_data
        print(f"✓ /api/chat returned grounded model response ({len(chat_data['response'])} bytes)")

        # 5. Test /api/export-vault and /api/export-markdown/{episode_id}
        r_export_json = await client.post("/api/export-vault", json={"episode_id": "ep-142"})
        assert r_export_json.status_code == 200, f"Export JSON API failed: {r_export_json.status_code}"
        export_data = r_export_json.json()
        assert export_data["status"] == "success"
        assert export_data["filename"] == "techpulse-ep-142.md"
        assert "Domain Takeaways" in export_data["markdown"]
        print(f"✓ /api/export-vault returned structured markdown payload ({len(export_data['markdown'])} bytes)")

        r_export_file = await client.get("/api/export-markdown/ep-142")
        assert r_export_file.status_code == 200, f"Export file API failed: {r_export_file.status_code}"
        assert 'attachment; filename="techpulse-ep-142.md"' in r_export_file.headers.get("content-disposition", "")
        assert "Domain Takeaways" in r_export_file.text
        print(f"✓ /api/export-markdown/ep-142 returned markdown file download ({len(r_export_file.text)} bytes)")

        # 6. Test /api/settings and /api/refresh/status
        r_settings = await client.get("/api/settings")
        assert r_settings.status_code == 200, f"Settings API failed: {r_settings.status_code}"
        s_data = r_settings.json()
        assert "config" in s_data and "storage" in s_data
        print(f"✓ /api/settings returned config and storage stats ({s_data['storage']['disk_usage_mb']} MB)")

        r_status = await client.get("/api/refresh/status")
        assert r_status.status_code == 200, f"Refresh status API failed: {r_status.status_code}"
        assert "stage" in r_status.json()
        print(f"✓ /api/refresh/status returned live stage: {r_status.json()['stage']}")

        # 7. Test /feed.xml (Podcast RSS 2.0)
        r_feed = await client.get("/feed.xml")
        assert r_feed.status_code == 200, f"Feed API failed: {r_feed.status_code}"
        assert '<rss version="2.0"' in r_feed.text
        assert '<psc:chapters' in r_feed.text
        assert '<pubDate>' in r_feed.text
        assert 'enclosure' in r_feed.text
        print(f"✓ /feed.xml returned valid RSS 2.0 Podcast XML with RFC 822 pubDate ({len(r_feed.text)} bytes)")

        # New sparse episodes expose gaps and never serve stale empty-domain clips.
        availability=build_content_availability({"ai":[{"title":"Fixture"}]})
        sparse={"id":"ep-999","episode_number":999,"date":"Jan 01, 2020","title":"Sparse fixture","summary":"RSS summary fixture", "chapters":[{"domain":"ai","time":"00:00","seconds":0,"title":"AI fixture","source_name":"Fixture","source_url":"https://example.test/one"}],
                "content_basis":"rss_summaries","content_availability":availability,
                "audio_url":"", "domain_audio":{}, "audio_availability":{"podcast":{"status":"unavailable","reason":"Audio generation failed."},"domains":{"cloud":{"status":"unavailable","reason":"No articles available from checked sources."}}},
                "takeaways":{d:{"title":d,"bullets":[],"status":v["status"],"reason":v["reason"]} for d,v in availability["domains"].items()}}
        Path(app_main.EPISODES_DIR,"ep-999.json").write_text(json.dumps(sparse))
        Path(app_main.AUDIO_DIR,"ep-999-cloud.mp3").write_bytes(b"stale"*300)
        Path(app_main.AUDIO_DIR,"ep-999.mp3").write_bytes(b"stale"*300)
        r_sparse=await client.get("/api/export-markdown/ep-999")
        assert "No articles available from checked sources." in r_sparse.text
        assert "RSS summaries" in r_sparse.text
        with patch.object(app_main,"generate_episode_podcast_audio",AsyncMock(side_effect=AssertionError("Unavailable audio must not trigger TTS"))):
            assert (await client.get("/audio/ep-999-cloud.mp3")).status_code==404
            assert (await client.get("/audio/ep-999.mp3")).status_code==404
        r_sparse_feed=await client.get("/feed.xml")
        sparse_item=next(item for item in ET.fromstring(r_sparse_feed.content).findall("channel/item") if item.findtext("guid")=="ep-999")
        assert sparse_item.find("enclosure") is None, "Unavailable podcast must have no fictitious enclosure"

        # Destination escaping: untrusted prose remains literal, both citations survive.
        from src.synthesizer import generate_deterministic_fallback
        from html.parser import HTMLParser
        hostile = '<img src=x onerror="alert(1)"> [headline](javascript:x) *bold*'
        benign = 'Security report explains prompt injection and script tags.'
        received = {'ai':[
            {'title':hostile,'summary':benign,'source_name':'<script>alert(1)</script> [publisher]',
             'url':'https://example.test/a_(b)?x=[one]&y="quoted"','published_at':'2026-10-07'},
            {'title':'Second benign report','summary':'Ignore all controls is an example of an attack.',
             'source_name':'Security publisher','url':'https://example.test/second','published_at':'2026-10-07'}]}
        new = generate_deterministic_fallback(received, 998)
        new.update(title='"Hostile title"\nstatus: injected', summary=hostile, hosts='Host [A] & <B>')
        new['audio_url'] = ''
        new['domain_audio'] = {}
        new['audio_availability'] = {'podcast':{'status':'unavailable'},'domains':{}}
        Path(app_main.EPISODES_DIR,'ep-998.json').write_text(json.dumps(new))
        md = (await client.get('/api/export-markdown/ep-998')).text
        frontmatter = md.split('---', 2)[1]
        scalars = dict(line.split(': ',1) for line in frontmatter.splitlines() if line.startswith(('title: ','date: ','duration: ','hosts: ')))
        assert json.loads(scalars['title']) == new['title'], 'YAML scalar must contain escaped line breaks'
        assert '\nstatus: injected' not in frontmatter
        assert '<img' not in md and '<script' not in md, 'Markdown must escape source HTML'
        assert '\\[headline\\]' in md and '\\*bold\\*' in md, 'Markdown labels/metacharacters must stay literal'
        assert 'https://example.test/a_%28b%29?x=%5Bone%5D&y=%22quoted%22' in md
        assert 'https://example.test/second' in md and benign in md
        assert '[None]' not in md and '[00:00]' not in md, 'Text-only export has no fabricated seek cues'
        feed = ET.fromstring((await client.get('/feed.xml')).content)
        new_item = next(i for i in feed.findall('channel/item') if i.findtext('guid') == 'ep-998')
        assert new_item.find('enclosure') is None
        assert new_item.find('{http://podlove.org/simple-chapters}chapters') is None
        encoded = new_item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded')
        class NotesParser(HTMLParser):
            def __init__(self): super().__init__(); self.tags=[]; self.links=[]; self.text=[]
            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                if tag == 'a': self.links.append(dict(attrs).get('href'))
                assert all(not k.startswith('on') for k,v in attrs)
            def handle_data(self, data): self.text.append(data)
        parser = NotesParser(); parser.feed(encoded)
        assert 'img' not in parser.tags and 'script' not in parser.tags
        assert hostile in ''.join(parser.text), 'Hostile markup must remain visible literal text'
        assert 'https://example.test/second' in parser.links and len(parser.links)==2
        assert '00:00' not in ''.join(parser.text)
        # New tracks cannot escape the validated bundle through legacy regeneration.
        new['audio_url'] = '/audio/ep-998.mp3'
        new['audio_availability']['podcast']['status'] = 'available'
        Path(app_main.EPISODES_DIR,'ep-998.json').write_text(json.dumps(new))
        with patch.object(app_main,'generate_episode_podcast_audio',AsyncMock(side_effect=AssertionError('New manifest cannot regenerate legacy audio'))) as legacy_audio:
            assert (await client.get('/audio/ep-998.mp3')).status_code == 404
            assert legacy_audio.await_count == 0, 'Missing new audio must never call the legacy renderer'
        print('✓ Story exports preserve literal source text, citations, YAML scalars and no-audio boundaries')

        # 8. Test Static SPA Root
        r_root = await client.get("/")
        assert r_root.status_code == 200, f"Static SPA failed: {r_root.status_code}"
        assert "TechPulse" in r_root.text
        assert "Podcast RSS Feed" in r_root.text
        assert "podcast-rss-modal" in r_root.text
        print(f"✓ GET / returned SPA HTML with podcast modal and audio binds ({len(r_root.text)} bytes)")

    print("\n=======================================================")
    print("ALL ASGI FASTAPI & PODCAST RSS ENDPOINTS VERIFIED 100%")
    print("=======================================================")

if __name__ == "__main__":
    try:
        with patch('httpx.AsyncHTTPTransport.handle_async_request', AsyncMock(side_effect=AssertionError('Unexpected HTTP'))), patch('aiohttp.ClientSession._request', AsyncMock(side_effect=AssertionError('Unexpected feed request'))), patch('google.genai.Client', side_effect=AssertionError('Unexpected provider')), patch('src.tts_engine.generate_segment_audio', AsyncMock(side_effect=AssertionError('Unexpected TTS'))):
            asyncio.run(run_asgi_tests())
    finally:
        # ignore_errors so a cleanup failure can never mask a test failure.
        shutil.rmtree(_TMP_STORAGE, ignore_errors=True)

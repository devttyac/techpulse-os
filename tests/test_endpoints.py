import asyncio
import httpx
import sys
import os
import shutil
import tempfile
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        with patch.object(app_main, "process_grounded_chat", new_callable=AsyncMock) as legacy_chat:
            r_chat = await client.post("/api/chat", json={"query": "Explain SPIFFE in banking", "episode_id": "ep-142"})
            assert r_chat.status_code == 503, f"Unapproved chat must be blocked: {r_chat.status_code}"
            legacy_chat.assert_not_awaited()
        print("✓ /api/chat blocks unapproved legacy model/provider calls")

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
        assert r_settings.status_code == 409, "Retained legacy Settings require explicit migration"
        assert "migration" in str(r_settings.json()).lower()
        print("✓ /api/settings refuses implicit legacy migration")
        retained_before = {p: p.read_bytes() for p in Path(app_main.EPISODES_DIR).glob("ep-*.json")}
        desired = {"max_episodes_retained": 14, "cron_schedule": "0 7 * * *",
                   "timezone_id": "Asia/Singapore", "chat_provider": "gemini",
                   "chat_model": "gemini-3.6-flash"}
        with patch.object(app_main, "enforce_retention_policy",
                          side_effect=AssertionError("Settings save must not clean up")):
            saved = await client.post("/api/settings", json={
                "config": desired, "expected_revision": 0, "initialization": "migrate"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert saved.json()["selected_model_status"] == "disabled"
        assert saved.json()["application_status"]["schedule"] == "committed_pending_scheduler_observation"
        assert {p: p.read_bytes() for p in Path(app_main.EPISODES_DIR).glob("ep-*.json")} == retained_before
        assert (await client.get("/api/settings")).json()["revision"] == 1
        stale = await client.post("/api/settings", json={
            "config": desired, "expected_revision": 0})
        assert stale.status_code == 409 and (await client.get("/api/settings")).json()["revision"] == 1
        with patch.object(app_main, "get_storage_stats", side_effect=OSError("stats unavailable")):
            saved_without_stats = await client.post("/api/settings", json={
                "config": {**desired, "max_episodes_retained": 30},
                "expected_revision": 1})
        assert saved_without_stats.status_code == 200, saved_without_stats.text
        assert saved_without_stats.json()["revision"] == 2
        assert saved_without_stats.json()["storage"] is None
        assert saved_without_stats.json()["storage_status"] == "unavailable"
        assert (await client.get("/api/settings")).json()["revision"] == 2
        with patch.object(app_main, "scheduler", MagicMock(running=True)), patch.dict(
                app_main.scheduler_status, {"status": "ready", "bound_revision": 2}):
            assert (await client.get("/api/settings")).json()["application_status"]["schedule"] == "applied"
        with patch.object(app_main, "scheduler", MagicMock(running=True)), patch.dict(
                app_main.scheduler_status, {"status": "blocked", "bound_revision": None}):
            assert (await client.get("/api/settings")).json()["application_status"]["schedule"] == "unavailable"

        with patch.object(app_main.run_coordinator, "start_manual",
                          return_value={"status": "busy", "reason": "active_worker_lock"}) as start:
            overlap = await client.post("/api/refresh")
            assert overlap.status_code == 200
            assert overlap.json()["status"] == "busy"
            start.assert_called_once_with()

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
        # Domain clips have their own duration and must agree across metadata and route.
        sparse.update(duration="10:00", total_seconds=600,
                      domain_audio={"ai": "/audio/ep-999-ai.mp3"})
        sparse["audio_availability"]["domains"]["ai"] = {"status": "available"}
        sparse_path = Path(app_main.EPISODES_DIR, "ep-999.json")
        sparse_path.write_text(json.dumps(sparse))
        domain_path = Path(app_main.AUDIO_DIR, "ep-999-ai.mp3")
        saved_bytes = sparse_path.read_bytes()
        for contents in (None, b"corrupt-domain-clip" * 100):
            if contents is not None:
                domain_path.write_bytes(contents)
            expected_audio = domain_path.read_bytes() if domain_path.is_file() else None
            for response in ((await client.get("/api/episodes")).json()["episodes"],
                             [(await client.get("/api/episodes/ep-999")).json()]):
                item = next(item for item in response if item["id"] == "ep-999")
                assert not item["domain_audio"].get("ai")
                assert item["audio_availability"]["domains"]["ai"]["status"] == "unavailable"
            assert (await client.get("/audio/ep-999-ai.mp3")).status_code == 404
            assert sparse_path.read_bytes() == saved_bytes
            assert (domain_path.read_bytes() if domain_path.is_file() else None) == expected_audio
        shutil.copyfile(Path(APP_DIR, "seed_data", "audio", "article-ai.mp3"), domain_path)
        valid_bytes = domain_path.read_bytes()
        for response in ((await client.get("/api/episodes")).json()["episodes"],
                         [(await client.get("/api/episodes/ep-999")).json()]):
            item = next(item for item in response if item["id"] == "ep-999")
            assert item["domain_audio"]["ai"] == "/audio/ep-999-ai.mp3"
            assert item["audio_availability"]["domains"]["ai"]["status"] == "available"
            assert item["audio_url"] == "", "A valid domain clip cannot invent main audio"
        assert (await client.get("/audio/ep-999-ai.mp3")).status_code == 200
        assert sparse_path.read_bytes() == saved_bytes and domain_path.read_bytes() == valid_bytes
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

        # TP04: duplicate metadata must not delete retained records on a list read.
        retained = {"id": "ep-995", "guid": "saved-guid-995", "title": "Same title",
                    "summary": "Same summary", "hosts": "Recorded Host A & Host B",
                    "date": "Oct 15, 2026", "zone": "Europe/London",
                    "source_url": "https://example.test/retained", "content_basis": "rss_summaries",
                    "audio_url": ""}
        unknown = {**retained, "id": "ep-996", "guid": "saved-guid-996"}
        unknown.pop("content_basis")
        captured = {**retained, "id": "ep-994", "guid": "saved-guid-994",
                    "legacy_evidence": {"schema_version": 1, "capture_status": "captured",
                                        "version_ids": ["a" * 64]}}
        captured.pop("content_basis")
        original = {"ep-995": retained, "ep-996": unknown, "ep-994": captured}
        before = {}
        for episode_id, value in original.items():
            path = Path(app_main.EPISODES_DIR, f"{episode_id}.json")
            path.write_text(json.dumps(value))
            before[episode_id] = path.read_bytes()
        listed = (await client.get("/api/episodes")).json()["episodes"]
        for episode_id, provenance, evidence in (("ep-995", "confirmed_rss", "never_captured"),
                                                  ("ep-996", "unknown", "unknown"),
                                                  ("ep-994", "captured", "captured_unavailable")):
            item = next(item for item in listed if item["id"] == episode_id)
            detail = (await client.get(f"/api/episodes/{episode_id}"))
            assert detail.status_code == 200
            assert item == detail.json()
            assert item["legacy_compatibility"]["provenance"] == provenance
            assert item["legacy_compatibility"]["evidence"] == evidence
            assert item["legacy_compatibility"]["full_article_features"] == "unavailable"
            for field in ("id", "guid", "hosts", "date", "zone", "source_url"):
                assert item[field] == original[episode_id][field]
            assert Path(app_main.EPISODES_DIR, f"{episode_id}.json").read_bytes() == before[episode_id]

        # Malformed present manifests fail instead of downgrading to legacy RSS.
        malformed = {**retained, "id": "ep-997", "story_manifest": {"schema_version": True}}
        Path(app_main.EPISODES_DIR, "ep-997.json").write_text(json.dumps(malformed))
        assert (await client.get("/api/episodes/ep-997")).status_code == 409

        # A seed file and a large status flag cannot replace or validate saved bytes.
        seed_record = json.loads(Path(app_main.EPISODES_DIR, "ep-142.json").read_text())
        seed_record["audio_availability"] = {"podcast": {"status": "available"}}
        Path(app_main.EPISODES_DIR, "ep-142.json").write_text(json.dumps(seed_record))
        volume_audio = Path(app_main.AUDIO_DIR, "ep-142.mp3")
        volume_audio.write_bytes(b"corrupt-retained-audio" * 90)
        corrupt_before = volume_audio.read_bytes()
        with patch.object(app_main, "generate_episode_podcast_audio",
                          AsyncMock(side_effect=AssertionError("Playback must never synthesize"))):
            assert (await client.get("/audio/ep-142.mp3")).status_code == 404
        assert volume_audio.read_bytes() == corrupt_before
        projected = (await client.get("/api/episodes/ep-142")).json()
        assert projected["audio_url"] == ""
        assert projected["audio_availability"]["podcast"]["status"] == "unavailable"
        assert Path(app_main.EPISODES_DIR, "ep-142.json").read_bytes() == json.dumps(seed_record).encode()
        malformed_audio = {**seed_record, "audio_availability": "invalid"}
        Path(app_main.EPISODES_DIR, "ep-142.json").write_text(json.dumps(malformed_audio))
        assert (await client.get("/audio/ep-142.mp3")).status_code == 404
        assert volume_audio.read_bytes() == corrupt_before

        # A versioned, explicitly available retained MP3 remains accessible.
        retained_audio = Path(app_main.AUDIO_DIR, "ep-139.mp3")
        assert retained_audio.is_file()
        retained_record = json.loads(Path(app_main.EPISODES_DIR, "ep-139.json").read_text())
        retained_record["audio_availability"] = {"podcast": {"status": "available"}}
        Path(app_main.EPISODES_DIR, "ep-139.json").write_text(json.dumps(retained_record))
        retained_audio_before = retained_audio.read_bytes()
        assert (await client.get("/audio/ep-139.mp3")).status_code == 404, "Incompatible measured duration must be unavailable"
        retained_record["total_seconds"] = 143
        retained_record["duration"] = "02:23"
        Path(app_main.EPISODES_DIR, "ep-139.json").write_text(json.dumps(retained_record))
        assert (await client.get("/audio/ep-139.mp3")).status_code == 200
        assert retained_audio.read_bytes() == retained_audio_before

        # Startup may seed an empty installation, but never rewrite a retained set.
        with tempfile.TemporaryDirectory(prefix="techpulse-startup-tp04-") as root:
            episodes = Path(root, "episodes")
            audio = Path(root, "audio")
            episodes.mkdir()
            audio.mkdir()
            for number in (901, 902):
                Path(episodes, f"ep-{number}.json").write_text(json.dumps({
                    "id": f"ep-{number}", "title": "Duplicate", "summary": "Duplicate",
                    "hosts": "Original hosts", "date": "Old date"}))
            Path(audio, "ep-901.mp3").write_bytes(b"retained-audio")
            original_bytes = {p: p.read_bytes() for p in (*episodes.iterdir(), *audio.iterdir())}
            fake_scheduler = MagicMock()
            fake_scheduler.running = False
            with patch.object(app_main, "STORAGE_DIR", root), \
                 patch.object(app_main, "EPISODES_DIR", str(episodes)), \
                 patch.object(app_main, "AUDIO_DIR", str(audio)), \
                 patch.object(app_main, "scheduler", fake_scheduler), \
                 patch.object(app_main, "load_config", return_value={"cron_schedule": "0 7 * * *", "max_episodes_retained": 1}), \
                 patch.object(app_main, "generate_episode_podcast_audio",
                              AsyncMock(side_effect=AssertionError("Startup must not regenerate"))):
                async with app_main.lifespan(app_main.app):
                    pass
            assert {p: p.read_bytes() for p in (*episodes.iterdir(), *audio.iterdir())} == original_bytes
        with tempfile.TemporaryDirectory(prefix="techpulse-fresh-tp04-") as root:
            episodes = Path(root, "episodes")
            audio = Path(root, "audio")
            episodes.mkdir()
            audio.mkdir()
            with patch.object(app_main, "STORAGE_DIR", root), \
                 patch.object(app_main, "EPISODES_DIR", str(episodes)), \
                 patch.object(app_main, "AUDIO_DIR", str(audio)):
                app_main.init_seed_data()
            assert Path(episodes, "ep-142.json").is_file()
            assert Path(audio, "ep-142.mp3").is_file()
        print("✓ TP04 read paths preserve retained identity, bytes, and truthful availability")

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

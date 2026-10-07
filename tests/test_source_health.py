"""Source checks persist independently of episodes and stay behind API authentication."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_IMPORT_STORAGE = tempfile.TemporaryDirectory(prefix="techpulse-health-import-")
os.environ["STORAGE_DIR"] = _IMPORT_STORAGE.name
os.environ["GEMINI_API_KEY"] = ""
with patch("dotenv.load_dotenv"):
    from src import main
from src import content_availability as availability
from src.ingestion import DOMAIN_FEEDS, fetch_feed_items


def diagnostics(outcome="success", accepted=0):
    return {"feeds":[{"feed_id":f"{domain}:{i}", "domain":domain, "name":feed["name"], "outcome":outcome, "accepted":accepted, "parse_warning":False} for domain, feeds in DOMAIN_FEEDS.items() for i,feed in enumerate(feeds)]}


class SourceHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="techpulse-health-")
        self.root = Path(self.tmp.name)
        (self.root/"episodes").mkdir(); (self.root/"audio").mkdir()
        self.patches = [patch.object(main,k,v) for k,v in {"STORAGE_DIR":str(self.root),"EPISODES_DIR":str(self.root/"episodes"),"AUDIO_DIR":str(self.root/"audio"),"CONFIG_FILE":str(self.root/"config.json"),"pipeline_state":{"running":False,"stage":"idle"},"current_pipeline_task":None}.items()]
        self.patches += [patch.dict(os.environ,{"GEMINI_API_KEY":"","API_SECRET_KEY":""}),patch.object(main,"generate_episode_podcast_audio",AsyncMock(return_value=(None,[],"00:00",0))),patch.object(main,"generate_all_domain_audios",AsyncMock(return_value={}))]
        for p in self.patches: p.start()
        if hasattr(main,"source_health"): self.patches.append(patch.object(main,"source_health",availability.empty_source_health())); self.patches[-1].start()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()
    async def run_empty(self, outcome="success"):
        async def ingest(*, diagnostics):
            diagnostics.update(globals()["diagnostics"](outcome))
            return {d:[] for d in DOMAIN_FEEDS}
        with patch.object(main,"ingest_all_domains",ingest): await main.run_daily_pipeline()
        return (await main.get_refresh_status())["source_health"]
    async def test_quiet_check_is_healthy_and_survives_restart(self):
        health = await self.run_empty()
        self.assertEqual(health["status"],"healthy")
        self.assertTrue(all(s["outcome"]=="quiet" for s in health["sources"]))
        self.assertEqual(health["latest_run"]["status"],"no_content")
        path=self.root/"latest_source_health.json"
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(availability.load_source_health(str(self.root)),health)
    async def test_polling_is_cached_and_public_health_excludes_sources(self):
        first = await self.run_empty()
        with patch.object(main,"ingest_all_domains",AsyncMock(side_effect=AssertionError("poll must not ingest"))):
            for _ in range(3): self.assertEqual((await main.get_refresh_status())["source_health"],first)
            self.assertNotIn("source_health",json.dumps(await main.health_check()))
    async def test_source_failure_is_not_quiet(self):
        health=await self.run_empty("http_error")
        self.assertEqual(health["status"],"unavailable")
        self.assertEqual(health["latest_run"]["status"],"failed")
    async def test_cancel_keeps_previous_complete_check_and_partial_attempt(self):
        first=await self.run_empty()
        async def cancelled(*,diagnostics):
            diagnostics.update(globals()["diagnostics"]("running"))
            diagnostics["feeds"][0].update(outcome="success",accepted=1)
            raise asyncio.CancelledError()
        with patch.object(main,"ingest_all_domains",cancelled):
            with self.assertRaises(asyncio.CancelledError): await main.run_daily_pipeline()
        current=(await main.get_refresh_status())["source_health"]
        self.assertEqual(current["status"],"cancelled")
        self.assertEqual(current["checked_at"],first["checked_at"])
        self.assertEqual(current["sources"],first["sources"])
        self.assertEqual(current["last_attempt"]["available_sources"],1)
        self.assertEqual(current["latest_run"]["status"],"cancelled")
        self.assertEqual(availability.load_source_health(str(self.root)),current)
    async def test_downstream_failure_keeps_source_success(self):
        async def ingest(*,diagnostics):
            diagnostics.update(globals()["diagnostics"]("success",1))
            return {"ai":[{"title":"Fixture","url":"https://example.test/one","summary":"Text","source_name":"Fixture"}]}
        with patch.object(main,"ingest_all_domains",ingest),patch.object(main,"synthesize_briefing",AsyncMock(side_effect=RuntimeError("secret"))): await main.run_daily_pipeline()
        health=(await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"],"healthy")
        self.assertEqual(health["latest_run"]["status"],"failed")
    async def test_write_failure_keeps_in_memory_result_and_read_failure_is_unknown(self):
        (self.root/"latest_source_health.json").mkdir()
        health=await self.run_empty()
        self.assertEqual(health["status"],"healthy")
        self.assertEqual(availability.load_source_health(str(self.root))["status"],"unknown")
    async def test_recovery_sanitizes_payload_and_rejects_oversized_snapshots(self):
        health=await self.run_empty()
        health["secret"]="SECRET_SENTINEL"
        health["sources"][0]["name"]="https://secret.test/?key=SECRET_SENTINEL"
        health["sources"][0]["accepted"]=999999
        path=self.root/"latest_source_health.json"
        path.write_text(json.dumps(health))
        restored=availability.load_source_health(str(self.root))
        self.assertNotIn("SECRET_SENTINEL",json.dumps(restored))
        self.assertLessEqual(restored["sources"][0]["accepted"],5)
        path.write_text(" "*100000)
        self.assertEqual(availability.load_source_health(str(self.root))["status"],"unknown")
    async def test_malformed_non_feed_unavailable_valid_empty_feed_quiet(self):
        for payload,outcome in [("<html><body>not a feed</body></html>","parse_error"),("<rss version='2.0'><channel><title>Broken", "parse_error"),("<rss version='2.0'><channel><title>Empty</title></channel></rss>","success"),("<?xml version='1.0' encoding='bogus'?><rss version='2.0'><channel><title>Empty</title></channel></rss>","success")]:
            stats={}
            transport=httpx.MockTransport(lambda request:httpx.Response(200,text=payload))
            async with httpx.AsyncClient(transport=transport) as client:
                self.assertEqual(await fetch_feed_items(client,"ai",DOMAIN_FEEDS["ai"][0],diagnostics=stats),[])
            self.assertEqual(stats["outcome"],outcome)
    async def test_schema_boolean_is_rejected_on_recovery(self):
        health=await self.run_empty()
        health["schema_version"]=True
        (self.root/"latest_source_health.json").write_text(json.dumps(health))
        self.assertEqual(availability.load_source_health(str(self.root))["status"],"unknown")
    async def test_recovered_identifiers_cannot_echo_configured_secrets(self):
        health=await self.run_empty()
        health["check_id"]=health["run_id"]="recovery-secret-sentinel"
        health["latest_run"]["episode_id"]="recovery-secret-sentinel"
        (self.root/"latest_source_health.json").write_text(json.dumps(health))
        with patch.dict(os.environ,{"API_SECRET_KEY":"recovery-secret-sentinel"}):
            restored=availability.load_source_health(str(self.root))
        self.assertNotIn("recovery-secret-sentinel",json.dumps(restored))

    async def test_mixed_check_distinguishes_quiet_failure_and_parse_caution(self):
        async def ingest(*,diagnostics):
            diagnostics.update(globals()["diagnostics"]("http_error"))
            for feed in diagnostics["feeds"][:3]: feed.update(outcome="success",parse_warning=True)
            return {d:[] for d in DOMAIN_FEEDS}
        with patch.object(main,"ingest_all_domains",ingest): await main.run_daily_pipeline()
        health=(await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"],"partial")
        self.assertEqual(health["available_sources"],3)
        self.assertEqual(health["sources"][0]["outcome"],"quiet")
        self.assertTrue(health["sources"][0]["parse_warning"])
        self.assertEqual(health["latest_run"]["status"],"no_content")
    async def test_timeout_preserves_previous_check_and_atomic_replace_failure_preserves_disk(self):
        first=await self.run_empty()
        path=self.root/"latest_source_health.json"
        before=path.read_bytes()
        with patch.object(availability.os,"replace",side_effect=PermissionError("secret")),patch.object(main,"ingest_all_domains",AsyncMock(side_effect=asyncio.TimeoutError)):
            await main.run_daily_pipeline()
        health=(await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"],"unknown")
        self.assertEqual(health["checked_at"],first["checked_at"])
        self.assertEqual(health["sources"],first["sources"])
        self.assertEqual(health["latest_run"]["status"],"failed")
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(list(self.root.glob(".source-health-*")))
    async def test_custom_incomplete_diagnostics_do_not_claim_all_sources_failed(self):
        async def ingest(*,diagnostics):
            diagnostics["feeds"]=[globals()["diagnostics"]("failed")["feeds"][0]]
            return {d:[] for d in DOMAIN_FEEDS}
        with patch.object(main,"ingest_all_domains",ingest): await main.run_daily_pipeline()
        self.assertEqual((await main.get_refresh_status())["source_health"]["latest_run"]["status"],"no_content")
    async def test_pre_entry_cancellation_preserves_completed_check(self):
        first=await self.run_empty()
        await main.manual_refresh()
        task=main.current_pipeline_task
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await asyncio.sleep(0)
        health=(await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"],"cancelled")
        self.assertEqual(health["checked_at"],first["checked_at"])
        self.assertEqual(health["latest_run"]["status"],"cancelled")

    async def test_missing_source_details_cannot_recover_as_healthy(self):
        health=await self.run_empty()
        health["sources"]=[]
        (self.root/"latest_source_health.json").write_text(json.dumps(health))
        self.assertEqual(availability.load_source_health(str(self.root))["status"],"unknown")

    async def test_authorization_and_public_isolation(self):
        await self.run_empty()
        with patch.dict(os.environ,{"API_SECRET_KEY":"health-test-key"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="http://test") as client:
                self.assertEqual((await client.get("/api/refresh/status")).status_code,401)
                response=await client.get("/api/refresh/status",headers={"X-API-Key":"health-test-key"})
                self.assertIn("source_health",response.json())
                public=(await client.get("/healthz")).json()
                self.assertNotIn("source_health",public)
                self.assertNotIn("source_health",public["pipeline_status"])

if __name__=="__main__":
    try: unittest.main()
    finally: _IMPORT_STORAGE.cleanup()

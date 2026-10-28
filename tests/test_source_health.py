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
from src.ingestion import DOMAIN_FEEDS
from src.acquisition.tldr import discover_from_fixtures


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
    async def test_retained_v1_quiet_check_survives_recovery_without_live_rss_check(self):
        # Retained v1 feed checks remain readable; TP07 does not perform one.
        legacy = availability.build_source_health(globals()["diagnostics"]("success"),
                                                  "legacy-run", "2026-10-28T00:00:00+00:00")
        self.assertEqual(legacy["status"], "healthy")
        self.assertTrue(all(row["outcome"] == "quiet" for row in legacy["sources"]))
        availability.save_source_health(str(self.root), legacy)
        self.assertEqual(availability.load_source_health(str(self.root)), legacy)
        self.assertEqual((self.root / "latest_source_health.json").stat().st_mode & 0o777, 0o600)
        current = await self.run_empty()
        self.assertEqual(current["status"], "not_checked")
        self.assertIsNone(current["checked_at"])

    async def test_polling_is_cached_and_public_health_excludes_sources(self):
        first = await self.run_empty()
        with patch.object(main,"ingest_all_domains",AsyncMock(side_effect=AssertionError("poll must not ingest"))):
            for _ in range(3): self.assertEqual((await main.get_refresh_status())["source_health"],first)
            self.assertNotIn("source_health",json.dumps(await main.health_check()))
    async def test_retained_v1_feed_failure_is_not_quiet_or_a_live_check(self):
        legacy = availability.build_source_health(globals()["diagnostics"]("http_error"),
                                                  "legacy-run", "2026-10-28T00:00:00+00:00")
        self.assertEqual(legacy["status"], "unavailable")
        self.assertTrue(all(row["outcome"] == "unavailable" for row in legacy["sources"]))
        current = await self.run_empty("http_error")
        self.assertEqual(current["status"], "not_checked")
        self.assertEqual(current["latest_run"]["status"], "failed")

    async def test_legacy_feed_cancellation_does_not_relabel_feeds_as_tldr(self):
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
        self.assertEqual(current["last_attempt"]["available_sources"], 0)
        self.assertIsNone(current["last_attempt"]["checked_at"])
        self.assertEqual(len(current["last_attempt"]["sources"]), 6)
        self.assertEqual(current["latest_run"]["status"],"cancelled")
        self.assertEqual(availability.load_source_health(str(self.root)),current)

    async def test_partial_tldr_attempt_survives_cancellation_and_recovery(self):
        from src import ingestion
        from src.provenance import discovery_record
        with patch.object(main, "ingest_all_domains", ingestion.ingest_all_domains):
            await main.run_daily_pipeline()
        prior = (await main.get_refresh_status())["source_health"]
        self.assertEqual(prior["status"], "disabled")

        candidate = {"edition_id": "ai", "issue_id": "2026-10-28",
                     "issue_url": "https://tldr.tech/ai/issues/2026-10-28",
                     "links": [{"url": "https://publisher.example/story", "title": "Story",
                                "placement": "editorial"}]}
        malformed = {"edition_id": "data", "issue_id": "2026-10-28",
                     "issue_url": "https://tldr.tech/data/issues/2026-10-28",
                     "links": "invalid"}
        partial = discovery_record(discover_from_fixtures({"ai": candidate, "data": malformed}))

        async def cancelled(*, diagnostics):
            diagnostics["discovery"] = partial
            raise asyncio.CancelledError()

        with patch.object(main, "ingest_all_domains", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await main.run_daily_pipeline()
        current = (await main.get_refresh_status())["source_health"]
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["checked_at"], prior["checked_at"])
        self.assertEqual(current["sources"], prior["sources"])
        self.assertEqual(current["latest_run"]["status"], "cancelled")
        attempt = current["last_attempt"]
        self.assertEqual(attempt["status"], "cancelled")
        self.assertEqual(attempt["available_sources"], 1)
        self.assertEqual([(row["edition_id"], row["outcome"], row["accepted"])
                          for row in attempt["sources"][:3]],
                         [("ai", "candidate", 1), ("infosec", "not_checked", 0),
                          ("data", "parse_error", 0)])
        self.assertEqual(availability.load_source_health(str(self.root)), current)

    async def test_recovered_v2_contradictory_empty_counts_fail_closed(self):
        snapshot = availability.empty_source_health()
        snapshot["status"] = "healthy"
        snapshot["checked_at"] = "2026-10-28T10:00:00+00:00"
        for row in snapshot["sources"]:
            row["outcome"] = "valid_empty"
        snapshot["sources"][0]["placement_unresolved"] = 1
        (self.root / "latest_source_health.json").write_text(json.dumps(snapshot))
        restored = availability.load_source_health(str(self.root))
        self.assertEqual(restored["status"], "unknown")
        self.assertEqual(restored["reason"], "inconsistent_outcome_counts")

    async def test_recovered_v2_false_disabled_claim_fails_closed(self):
        snapshot = availability.empty_source_health()
        snapshot["status"] = "disabled"
        snapshot["sources"][0]["outcome"] = "disabled"
        (self.root / "latest_source_health.json").write_text(json.dumps(snapshot))
        restored = availability.load_source_health(str(self.root))
        self.assertEqual(restored["status"], "unknown")
        self.assertEqual(restored["reason"], "invalid_disabled_diagnostic")
    async def test_unadmitted_source_cannot_reach_downstream_synthesis(self):
        async def ingest(*, diagnostics):
            diagnostics.update(globals()["diagnostics"]("success", 1))
            return {"ai": [{"title": "Fixture", "url": "https://example.test/one",
                            "summary": "Text", "source_name": "Fixture"}]}
        with patch.object(main, "ingest_all_domains", ingest), patch.object(
                main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("No admitted body"))) as synthesis:
            await main.run_daily_pipeline()
        self.assertEqual(synthesis.await_count, 0)
        health = (await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"], "not_checked")
        self.assertEqual(health["latest_run"]["status"], "failed")
        # TP06 later tests downstream failure after a real admitted source check.

    async def test_write_failure_keeps_in_memory_result_and_read_failure_is_unknown(self):
        (self.root / "latest_source_health.json").mkdir()
        snapshot = availability.empty_source_health()
        with patch.object(main, "source_health", availability.empty_source_health()):
            main._cache_source_health(snapshot)
            self.assertEqual(main.source_health["status"], "not_checked")
        self.assertEqual(availability.load_source_health(str(self.root))["status"], "unknown")

    async def test_retained_v1_recovery_sanitizes_payload_and_oversized_snapshots(self):
        # Preserve v1 retained snapshot sanitization without treating RSS as active.
        health = availability.build_source_health(globals()["diagnostics"]("success"),
                                                  "legacy-run", "2026-10-28T00:00:00+00:00")
        health["secret"] = "SECRET_SENTINEL"
        health["sources"][0]["name"] = "https://secret.test/?key=SECRET_SENTINEL"
        health["sources"][0]["accepted"] = 999999
        path = self.root / "latest_source_health.json"
        path.write_text(json.dumps(health))
        restored = availability.load_source_health(str(self.root))
        self.assertNotIn("SECRET_SENTINEL", json.dumps(restored))
        self.assertLessEqual(restored["sources"][0]["accepted"], 5)
        path.write_text(" " * 100000)
        self.assertEqual(availability.load_source_health(str(self.root))["status"], "unknown")

    async def test_malformed_issue_is_not_valid_empty_discovery(self):
        valid = {"edition_id":"ai", "issue_id":"2026-10-28",
                 "issue_url":"https://tldr.tech/ai/issues/2026-10-28", "links":[]}
        invalid = {**valid, "links":"not-a-link-list"}
        self.assertEqual(discover_from_fixtures({"ai":valid})["editions"][0]["outcome"],"valid_empty")
        self.assertEqual(discover_from_fixtures({"ai":invalid})["editions"][0]["outcome"],"parse_error")
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

    async def test_retained_v1_mixed_check_keeps_quiet_failure_and_parse_caution(self):
        old = globals()["diagnostics"]("http_error")
        for feed in old["feeds"][:3]:
            feed.update(outcome="success", parse_warning=True)
        health = availability.build_source_health(old, "legacy-run", "2026-10-28T00:00:00+00:00")
        self.assertEqual(health["status"], "partial")
        self.assertEqual(health["available_sources"], 3)
        self.assertEqual(health["sources"][0]["outcome"], "quiet")
        self.assertTrue(health["sources"][0]["parse_warning"])
        self.assertEqual((await self.run_empty("http_error"))["status"], "not_checked")

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
        async def ingest(*, diagnostics):
            diagnostics["feeds"] = [globals()["diagnostics"]("failed")["feeds"][0]]
            return {d: [] for d in DOMAIN_FEEDS}
        with patch.object(main, "ingest_all_domains", ingest):
            await main.run_daily_pipeline()
        health = (await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"], "not_checked")
        self.assertIsNone(health["checked_at"])
        self.assertEqual(health["latest_run"]["status"], "failed")

    async def test_coordinated_admission_and_stop_preserve_prior_source_check(self):
        first=await self.run_empty()
        with patch.object(main.run_coordinator,"start_manual",return_value={
                "status":"started","run_id":"run-pending-fixture"}),patch.object(
                main.run_coordinator,"cancel_owned",return_value={
                    "status":"stopped_pending_reconciliation","run_id":"run-pending-fixture"}):
            admitted=await main.manual_refresh()
            self.assertEqual(admitted["state"]["provenance_status"],"pending")
            self.assertIsNone(main.current_pipeline_task)
            stopped=await main.cancel_refresh()
            self.assertEqual(stopped["status"],"stopped_pending_reconciliation")
        health=(await main.get_refresh_status())["source_health"]
        self.assertEqual(health["status"],first["status"])
        self.assertEqual(health["checked_at"],first["checked_at"])
        self.assertEqual(health["latest_run"],first["latest_run"])
        self.assertEqual(availability.load_source_health(str(self.root)),first)

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

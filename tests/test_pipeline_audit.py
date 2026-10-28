"""Offline lifecycle regressions; persisted data stays in temporary storage.

TP07 has no admitted publisher-body path. Former successful RSS integration
oracles remain pending acceptance rather than being renamed as passing checks:
TP06: sparse coverage/export, source-unavailable takeaway, completed episode
provenance, skip/dedup, candidate turnover, body-change regeneration, metadata
write failure and retained-history publication.
TP21: audio-unavailable text publication, audio timeout, per-story model
attempts, same-domain chapter citations, prior-title carryover, provider
failure, domain-audio fallback, measured timing, malformed audio bundle (seven
cases), and bundle cancellation. These require admitted articles and a real
publication path; current tests assert the TP07 stop boundary instead.
"""
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
_IMPORT_STORAGE = tempfile.TemporaryDirectory(prefix="techpulse-audit-import-")
os.environ["STORAGE_DIR"] = _IMPORT_STORAGE.name
os.environ["GEMINI_API_KEY"] = ""
with patch("dotenv.load_dotenv"):
    from src import main
from src.synthesizer import DOMAIN_ORDER
from src import tts_engine
from src import ingestion
from src.acquisition.tldr import discover_from_fixtures
from src.provenance import discovery_record
from src.content_availability import load_source_health


def corpus():
    return {d: [{"title": d + " fixture", "source_name": "Fixture", "url": "https://example.test/" + d,
                 "summary": "Fixture summary", "published_at": "2026-10-06"}] for d in DOMAIN_ORDER}


class PipelineAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="techpulse-pipeline-audit-")
        self.root = Path(self.tmp.name)
        (self.root / "episodes").mkdir()
        (self.root / "audio").mkdir()
        values = {"STORAGE_DIR": str(self.root), "EPISODES_DIR": str(self.root / "episodes"),
                  "AUDIO_DIR": str(self.root / "audio"), "CONFIG_FILE": str(self.root / "config.json"),
                  "current_pipeline_task": None,
                  "pipeline_state": {"running": False, "stage": "idle", "progress": 0, "error": None}}
        self.patches = [patch.object(main, key, value) for key, value in values.items()]
        self.patches += [patch.dict(os.environ, {"GEMINI_API_KEY": "", "API_SECRET_KEY": ""}),
                         patch.object(main, "ingest_all_domains", AsyncMock(return_value=corpus())),
                         patch.object(main, "generate_story_audio_bundle", AsyncMock(side_effect=AssertionError("Unadmitted audio")), create=True),
                         patch.object(tts_engine, "generate_segment_audio", AsyncMock(side_effect=AssertionError("Unexpected TTS"))),
                         patch("httpx.AsyncHTTPTransport.handle_async_request", AsyncMock(side_effect=AssertionError("Unexpected HTTP"))),
                         patch("aiohttp.ClientSession._request", AsyncMock(side_effect=AssertionError("Unexpected feed request"))),
                         patch("google.genai.Client", side_effect=AssertionError("Unexpected provider")),
                         patch.object(main, "generate_episode_podcast_audio", AsyncMock(side_effect=AssertionError("Unexpected legacy podcast renderer"))),
                         patch.object(main, "generate_all_domain_audios", AsyncMock(side_effect=AssertionError("Unexpected legacy domain renderer")))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def rows(self, filename="runs.jsonl"):
        path = self.root / filename
        self.assertTrue(path.exists(), "Every terminal run needs a durable history entry")
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def _assert_unadmitted_run(self, *, retained=None):
        # TP07 has no accepted publisher-body path. TP06/TP21 will restore
        # admitted-publication checks after their producing tasks exist.
        before = retained.read_bytes() if retained is not None else None
        with patch.object(main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("No admitted body"))) as synth, patch.object(
                main, "generate_story_audio_bundle", AsyncMock(side_effect=AssertionError("No admitted body"))) as bundle:
            await main.run_daily_pipeline()
        row = self.rows()[-1]
        self.assertEqual((row["status"], row["error_category"]), ("failed", "source_not_admitted"))
        self.assertEqual(row["stages"]["synthesis"]["status"], "not_started")
        self.assertEqual(row["stages"]["podcast_audio"]["status"], "not_started")
        self.assertEqual((synth.await_count, bundle.await_count), (0, 0))
        self.assertFalse((self.root / "episodes" / "ep-143.json").exists())
        self.assertEqual(list((self.root / "audio").iterdir()), [])
        if retained is not None:
            self.assertEqual(retained.read_bytes(), before)
        return row

    async def test_disabled_discovery_stops_before_legacy_synthesis_or_publication(self):
        with patch.object(main, "ingest_all_domains", ingestion.ingest_all_domains), patch.object(
                main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("Discovery is not evidence"))):
            await main.run_daily_pipeline()
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertEqual(self.rows()[0]["error_category"], "source_not_ready")
        self.assertNotIn("content_availability", self.rows()[0])
        self.assertEqual(main.pipeline_state["stage"], "error")
        persisted = load_source_health(str(self.root))
        self.assertEqual((persisted["schema_version"], persisted["status"], persisted["checked_at"]),
                         (2, "disabled", None))
        self.assertEqual(list((self.root / "episodes").iterdir()), [])
        self.assertEqual(list((self.root / "audio").iterdir()), [])

    async def test_discovery_candidate_cannot_enter_legacy_article_synthesis(self):
        fixture = {"edition_id": "ai", "issue_id": "2026-10-28",
                   "issue_url": "https://tldr.tech/ai/issues/2026-10-28",
                   "links": [{"url": "https://publisher.example/story", "title": "A story",
                              "placement": "editorial"}]}
        result = discover_from_fixtures({"ai": fixture})
        async def candidate_only(*, diagnostics):
            diagnostics["discovery"] = discovery_record(result)
            return {"ai": [{"url": result["candidates"][0]["url"], "title": "A story",
                            "summary": "Newsletter excerpt"}]}
        with patch.object(main, "ingest_all_domains", candidate_only), patch.object(
                main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("Candidate is not evidence"))):
            await main.run_daily_pipeline()
        self.assertEqual(self.rows()[0]["error_category"], "source_not_admitted")
        self.assertEqual(list((self.root / "episodes").iterdir()), [])

    async def test_missing_discovery_with_nonempty_corpus_cannot_publish(self):
        with patch.object(main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("No admission"))):
            await main.run_daily_pipeline()
        self.assertEqual(self.rows()[0]["error_category"], "source_not_admitted")
        self.assertEqual(list((self.root / "episodes").iterdir()), [])

    async def test_malformed_discovery_with_nonempty_corpus_cannot_publish(self):
        async def malformed(*, diagnostics):
            diagnostics["discovery"] = {"status": [], "editions": None}
            return corpus()
        with patch.object(main, "ingest_all_domains", malformed), patch.object(
                main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("No admission"))):
            await main.run_daily_pipeline()
        self.assertEqual(self.rows()[0]["error_category"], "source_not_admitted")
        self.assertEqual(list((self.root / "episodes").iterdir()), [])

    async def test_candidate_only_discovery_with_nonempty_corpus_cannot_publish(self):
        async def candidate_only(*, diagnostics):
            diagnostics["discovery"] = {"status": "fixture_only", "candidate_count": 1}
            return corpus()
        with patch.object(main, "ingest_all_domains", candidate_only), patch.object(
                main, "synthesize_briefing", AsyncMock(side_effect=AssertionError("No admission"))):
            await main.run_daily_pipeline()
        self.assertEqual(self.rows()[0]["error_category"], "source_not_admitted")
        self.assertEqual(list((self.root / "episodes").iterdir()), [])

    async def test_missing_discovery_preserves_retained_history_with_empty_corpus(self):
        path = self.root / "episodes" / "ep-9.json"
        path.write_text(json.dumps({"id": "ep-9", "title": "Retained"}))
        with patch.object(main, "ingest_all_domains", AsyncMock(return_value={d: [] for d in DOMAIN_ORDER})):
            row = await self._assert_unadmitted_run(retained=path)
        self.assertEqual(row["ingestion"]["per_domain"], {d: 0 for d in DOMAIN_ORDER})
        self.assertEqual(list((self.root / "episodes").iterdir()), [path])

    async def test_retired_feed_failure_diagnostics_cannot_authorize_publication(self):
        async def failed_ingest(*, diagnostics):
            diagnostics["feeds"] = [{"feed_id":f"{d}:{i}", "domain":d, "outcome":"http_error", "accepted":0} for d,feeds in main.DOMAIN_FEEDS.items() for i in range(len(feeds))]
            return {d: [] for d in DOMAIN_ORDER}
        with patch.object(main, "ingest_all_domains", failed_ingest):
            await main.run_daily_pipeline()
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertEqual(self.rows()[0]["error_category"], "source_not_admitted")
        self.assertEqual(self.rows()[0]["stages"]["synthesis"]["status"], "not_started")
        self.assertEqual(main.pipeline_state["stage"], "error")
        health = load_source_health(str(self.root))
        self.assertEqual(health["status"], "unknown")
        self.assertIsNone(health["checked_at"])
        self.assertEqual(list((self.root / "episodes").iterdir()), [])

    async def test_repeated_rejected_runs_keep_distinct_audit_and_retained_episode(self):
        path = self.root / "episodes" / "ep-9.json"
        path.write_text(json.dumps({"id": "ep-9", "pipeline_run": {"status": "completed"}}))
        before = path.read_bytes()
        await self._assert_unadmitted_run(retained=path)
        await self._assert_unadmitted_run(retained=path)
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["failed", "failed"])
        self.assertNotEqual(rows[0]["run_id"], rows[1]["run_id"])
        self.assertEqual(path.read_bytes(), before)

    async def test_exception_run_records_only_safe_category(self):
        with self.assertLogs("techpulse.main", level="ERROR") as logs, patch.object(main, "ingest_all_domains", AsyncMock(side_effect=RuntimeError("SECRET_SENTINEL https://provider.test/?key=SECRET_SENTINEL"))):
            await main.run_daily_pipeline()
        row = self.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["stages"]["ingestion"]["status"], "failed")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(row))
        self.assertNotIn("provider.test", json.dumps(row))
        self.assertNotIn("SECRET_SENTINEL", str(logs.output))

    async def test_timeout_has_distinct_outcome(self):
        with patch.object(main, "ingest_all_domains", AsyncMock(side_effect=asyncio.TimeoutError)):
            await main.run_daily_pipeline()
        row = self.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "timeout")

    async def test_cancelled_run_records_once_and_propagates_cancellation(self):
        with patch.object(main, "ingest_all_domains", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await main.run_daily_pipeline()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["status"], "cancelled")

    async def test_manual_acceptance_is_delegated_to_durable_coordinator(self):
        with patch.object(main.run_coordinator, "start_manual", return_value={
                "status": "started", "run_id": "run-coordinated"}) as start:
            result = await main.manual_refresh()
        start.assert_called_once_with()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["state"]["run_id"], "run-coordinated")
        self.assertIsNone(main.current_pipeline_task)

    async def test_retention_does_not_delete_cross_run_history(self):
        await main.run_daily_pipeline()
        before = (self.root / "runs.jsonl").read_bytes()
        main.enforce_retention_policy(1)
        self.assertEqual((self.root / "runs.jsonl").read_bytes(), before)

    async def test_rejected_run_does_not_apply_retention_to_legacy_records(self):
        episodes = self.root / "episodes"
        paths = []
        for number in (140, 141):
            path = episodes / f"ep-{number}.json"
            path.write_text(json.dumps({"id": f"ep-{number}", "episode_number": number,
                                        "date": "Jan 01, 2020", "title": f"Legacy {number}",
                                        "summary": "Distinct retained episode", "chapters": [],
                                        "ingested_urls": [f"https://legacy.test/{number}"]}))
            paths.append((path, path.read_bytes()))
        with patch.object(main, "enforce_retention_policy", side_effect=AssertionError("No retention on rejected run")) as retention:
            await self._assert_unadmitted_run()
        self.assertEqual(retention.call_count, 0)
        for path, before in paths:
            self.assertEqual(path.read_bytes(), before)
        # TP26 owns retained-data lifecycle; TP06 later checks a successful run.

    async def test_mixed_legacy_and_managed_count_blocks_unsafe_retirement(self):
        from src.state_store import StateStore
        legacy = []
        for number in range(100, 114):
            path = self.root / "episodes" / f"ep-{number}.json"
            path.write_text(json.dumps({"id": f"ep-{number}"}))
            legacy.append(path)
        with StateStore.create(self.root / "state") as store:
            for number in (201, 202):
                store.put_artifact(f"audio-{number}", "main_audio", f"audio {number}".encode(), {})
                store.publish_episode(f"ep-{number}", [f"audio-{number}"],
                                      [f"coverage-{number}"], {"episode_number": number})
            managed_files = [store.artifact_file(f"audio-{number}") for number in (201, 202)]

        result = main.enforce_retention_policy(14)
        self.assertEqual(result["status"], "blocked_unknown_ownership")
        self.assertEqual(result["blocked_legacy"], 2)
        self.assertEqual(result["managed_active"], 2)
        self.assertEqual(result["purged_episodes"], 0)
        self.assertTrue(all(path.exists() for path in legacy + managed_files))

    async def test_append_failure_is_visible_without_claiming_audit_success(self):
        (self.root / "runs.jsonl").mkdir()
        await main.run_daily_pipeline()
        self.assertEqual(main.pipeline_state["provenance_status"], "write_failed")
        self.assertEqual(main.pipeline_state["provenance_error"], "audit_write_failed")

    async def test_privileged_actions_have_bounded_records(self):
        from src.state_store import StateStore
        config = {"max_episodes_retained": 14, "cron_schedule": "0 7 * * *",
                  "timezone_id": "Asia/Singapore", "chat_provider": "gemini",
                  "chat_model": "gemini-3.6-flash"}
        saved = await main.update_settings(main.SettingsUpdate(
            config=config, expected_revision=0, initialization="fresh"))
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["selected_model_status"], "disabled")
        with patch.object(main.run_coordinator, "cancel_owned", return_value={"status": "no_active_task"}):
            await main.cancel_refresh()
        rows = self.rows("audit.jsonl")
        self.assertEqual([r["action"] for r in rows], ["refresh_cancel"])
        self.assertEqual(rows[0]["outcome"], "no_active_task")
        with StateStore.open(self.root / "state") as store:
            events = store.operation_events("settings-revision-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fields"]["control_key"], "settings_snapshot")
        self.assertEqual(events[0]["fields"]["control_revision"], 1)
        self.assertEqual(events[0]["fields"]["outcome"], "committed")
        self.assertEqual(len(events[0]["fields"]["evidence_digest"]), 64)
        self.assertNotIn("SECRET_SENTINEL", json.dumps((rows, events, saved)))

    async def test_legacy_episode_read_is_unchanged(self):
        path = self.root / "episodes" / "ep-9.json"
        legacy = {"id": "ep-9", "episode_number": 9, "chapters": [], "takeaways": {}}
        path.write_text(json.dumps(legacy))
        self.assertEqual(await main.get_episode_detail("ep-9"), legacy)

    async def test_present_new_markers_never_enter_legacy_duplicate_deletion(self):
        markers = ({'story_manifest':None}, {'episode_fingerprint':None},
                   {'pipeline_run':{'synthesis':{'contract_version':True}}})
        paths = []
        for index, marker in enumerate(markers, start=140):
            path = self.root/'episodes'/f'ep-{index}.json'
            path.write_text(json.dumps({'id':f'ep-{index}', 'title':'Identical aggregate title',
                'summary':'Identical aggregate summary', **marker}))
            paths.append((path, path.read_bytes()))
        self.assertEqual(len(main.get_sorted_episode_files()), 3)
        for path, before in paths:
            self.assertEqual(path.read_bytes(), before)

    async def test_rejected_run_keeps_legacy_record_readable(self):
        path = self.root / "episodes" / "ep-142.json"
        path.write_text(json.dumps({"id": "ep-142", "title": "Legacy", "summary": "Retained"}))
        before = path.read_bytes()
        await self._assert_unadmitted_run(retained=path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(await main.get_episode_detail("ep-142"), json.loads(before))

    async def test_legacy_duplicate_list_read_preserves_distinct_ids_and_audio(self):
        before = {}
        for number in (140,141):
            record = self.root/'episodes'/f'ep-{number}.json'
            audio = self.root/'audio'/f'ep-{number}.mp3'
            record.write_text(json.dumps({'id':f'ep-{number}', 'title':'Legacy duplicate',
                                          'summary':'Same legacy summary'}))
            audio.write_bytes(f'episode-{number}'.encode())
            before[record] = record.read_bytes()
            before[audio] = audio.read_bytes()
        self.assertEqual(main.get_sorted_episode_files(), ['ep-141.json', 'ep-140.json'])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    async def test_failed_cleanup_audits_but_failed_required_settings_audit_blocks_save(self):
        from fastapi import HTTPException
        from src.settings import SettingsService
        from src.state_store import StateStore
        config = {"max_episodes_retained": 14, "cron_schedule": "0 7 * * *",
                  "timezone_id": "Asia/Singapore", "chat_provider": "gemini",
                  "chat_model": "gemini-3.6-flash"}
        await main.update_settings(main.SettingsUpdate(
            config=config, expected_revision=0, initialization="fresh"))
        with patch.object(main, "enforce_retention_policy", side_effect=ValueError("fixture failure")):
            with self.assertRaises(ValueError):
                await main.trigger_storage_cleanup()
            with patch.object(StateStore, "_insert_required_event", side_effect=OSError("audit failed")):
                with self.assertRaises(HTTPException) as failure:
                    await main.update_settings(main.SettingsUpdate(
                        config={**config, "max_episodes_retained": 30}, expected_revision=1))
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(SettingsService(self.root).read()["revision"], 1)
        self.assertEqual(SettingsService(self.root).read()["config"], config)
        rows = self.rows("audit.jsonl")
        self.assertEqual([r["action"] for r in rows], ["settings_cleanup"])
        self.assertEqual(rows[0]["outcome"], "handler_failed")

    async def test_manual_acceptance_exposes_new_pending_run_before_task_entry(self):
        main.pipeline_state.update(run_id="prior-run", provenance_status="recorded")
        with patch.object(main.run_coordinator, "start_manual", return_value={
                "status": "started", "run_id": "run-new"}):
            result = await main.manual_refresh()
        self.assertNotEqual(result["state"]["run_id"], "prior-run")
        self.assertEqual(result["state"]["provenance_status"], "pending")
        self.assertIsNone(main.current_pipeline_task)

    async def test_action_audit_failure_survives_successful_run_finalization(self):
        (self.root / "audit.jsonl").mkdir()
        with patch.object(main.run_coordinator, "start_manual", return_value={
                "status": "started", "run_id": "run-audited"}):
            await main.manual_refresh()
        await main.run_daily_pipeline()
        self.assertEqual(main.pipeline_state.get("action_audit_status"), "write_failed")
        self.assertEqual(main.pipeline_state["provenance_status"], "recorded")

    async def test_concurrent_runs_have_distinct_durable_records(self):
        await asyncio.gather(main.run_daily_pipeline(), main.run_daily_pipeline())
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["run_id"] for row in rows}), 2)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _IMPORT_STORAGE.cleanup()

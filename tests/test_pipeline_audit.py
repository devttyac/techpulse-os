"""Offline lifecycle regressions; all persisted data stays in temporary storage."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_IMPORT_STORAGE = tempfile.TemporaryDirectory(prefix="techpulse-audit-import-")
os.environ["STORAGE_DIR"] = _IMPORT_STORAGE.name
os.environ["GEMINI_API_KEY"] = ""
with patch("dotenv.load_dotenv"):
    from src import main
from src.synthesizer import DOMAIN_ORDER, generate_deterministic_fallback


def corpus():
    return {d: [{"title": d + " fixture", "source_name": "Fixture", "url": "https://example.test/" + d,
                 "summary": "Fixture summary", "published_at": "2026-10-06"}] for d in DOMAIN_ORDER}


async def audio(data, directory):
    return "unused.mp3", data["chapters"], "00:12", 12


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
        self.patches += [patch.dict(os.environ, {"GEMINI_API_KEY": ""}),
                         patch.object(main, "ingest_all_domains", AsyncMock(return_value=corpus())),
                         patch.object(main, "generate_episode_podcast_audio", audio),
                         patch.object(main, "generate_all_domain_audios", AsyncMock(return_value={}))]
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

    async def test_completed_episode_has_same_record_as_durable_history(self):
        await main.run_daily_pipeline()
        episode = json.loads((self.root / "episodes" / "ep-143.json").read_text())
        self.assertIn("pipeline_run", episode, "Generated episodes must expose generation provenance")
        row = self.rows()[0]
        self.assertEqual(row, episode["pipeline_run"])
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["synthesis"]["path"], "deterministic_fallback")
        self.assertEqual(row["synthesis"]["fallback_reason"], "no_api_key")
        self.assertIsNone(row["synthesis"]["model"])
        self.assertEqual(row["selection"]["ai"]["reason"], "position_0")
        self.assertEqual(row["stages"]["podcast_audio"]["status"], "call_returned")

    async def test_skipped_run_is_recorded_without_rewriting_episode_provenance(self):
        await main.run_daily_pipeline()
        path = self.root / "episodes" / "ep-143.json"
        before = path.read_bytes()
        await main.run_daily_pipeline()
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["completed", "skipped"])
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

    async def test_cancel_before_coroutine_entry_still_records_accepted_run(self):
        result = await main.manual_refresh()
        task = main.current_pipeline_task
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["status"], "cancelled")
        self.assertEqual(self.rows()[0]["trigger"], "manual")

    async def test_retention_does_not_delete_cross_run_history(self):
        await main.run_daily_pipeline()
        before = (self.root / "runs.jsonl").read_bytes()
        main.enforce_retention_policy(1)
        self.assertEqual((self.root / "runs.jsonl").read_bytes(), before)

    async def test_retention_diagnostic_matches_real_episode_purges(self):
        episodes = self.root / "episodes"
        legacy_paths = []
        for number in (140, 141):
            path = episodes / f"ep-{number}.json"
            path.write_text(json.dumps({"id": f"ep-{number}", "episode_number": number,
                                       "date": "Jan 01, 2020", "title": f"Legacy {number}",
                                       "summary": f"Distinct legacy summary {number}", "chapters": [],
                                       "ingested_urls": [f"https://legacy.test/{number}"]}))
            legacy_paths.append(path)
        (self.root / "config.json").write_text(json.dumps({"max_episodes_retained": 1}))
        retained_results = []
        original_retention = main.enforce_retention_policy

        def capture_retention(*args, **kwargs):
            result = original_retention(*args, **kwargs)
            retained_results.append(result)
            return result

        with patch.object(main, "enforce_retention_policy", capture_retention):
            await main.run_daily_pipeline()
        actual_removed = sum(not path.exists() for path in legacy_paths)
        self.assertEqual(actual_removed, 2)
        self.assertEqual([path.name for path in episodes.glob("*.json")], ["ep-143.json"])
        self.assertEqual(len(retained_results), 1)
        reported = self.rows()[0]["stages"]["retention"]["reported_deleted"]
        self.assertEqual(reported, actual_removed)
        self.assertEqual(reported, retained_results[0]["purged_episodes"])
        episode = json.loads((episodes / "ep-143.json").read_text())
        self.assertEqual(episode["pipeline_run"]["stages"]["retention"]["reported_deleted"], reported)

    async def test_append_failure_is_visible_without_claiming_audit_success(self):
        (self.root / "runs.jsonl").mkdir()
        await main.run_daily_pipeline()
        self.assertEqual(main.pipeline_state["provenance_status"], "write_failed")
        self.assertEqual(main.pipeline_state["provenance_error"], "audit_write_failed")

    async def test_privileged_actions_have_bounded_records(self):
        await main.update_settings(main.SettingsUpdate(gemini_model="SECRET_SENTINEL"))
        await main.cancel_refresh()
        rows = self.rows("audit.jsonl")
        self.assertEqual([r["action"] for r in rows], ["settings_update", "refresh_cancel"])
        self.assertEqual(rows[0]["changed_fields"], ["gemini_model"])
        self.assertEqual(rows[1]["outcome"], "no_active_task")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(rows))

    async def test_legacy_episode_read_is_unchanged(self):
        path = self.root / "episodes" / "ep-9.json"
        legacy = {"id": "ep-9", "episode_number": 9, "chapters": [], "takeaways": {}}
        path.write_text(json.dumps(legacy))
        self.assertEqual(await main.get_episode_detail("ep-9"), legacy)

    async def test_exhausted_models_keep_attempted_prompt_inputs(self):
        async def exhausted(articles, episode_num, **kwargs):
            kwargs["diagnostics"].update(path="deterministic_fallback", fallback_reason="attempts_exhausted",
                                          attempts=[{"outcome": "transport_error", "model": "fixture"}])
            return generate_deterministic_fallback(articles, episode_num)
        with patch.object(main, "synthesize_briefing", exhausted):
            await main.run_daily_pipeline()
        selection = self.rows()[0]["selection"]["ai"]
        self.assertEqual(selection["reason"], "position_0")
        self.assertEqual(len(selection["prompt_articles"]), 1,
                         "Attempted model input must survive terminal fallback")

    async def test_failed_privileged_handlers_audit_then_reraise(self):
        with patch.object(main, "enforce_retention_policy", side_effect=ValueError("fixture failure")):
            with self.assertRaises(ValueError):
                await main.trigger_storage_cleanup()
            with self.assertRaises(ValueError):
                await main.update_settings(main.SettingsUpdate())
        rows = self.rows("audit.jsonl")
        self.assertEqual([r["action"] for r in rows], ["settings_cleanup", "settings_update"])
        self.assertEqual([r["outcome"] for r in rows], ["handler_failed", "handler_failed"])

    async def test_manual_acceptance_exposes_new_pending_run_before_task_entry(self):
        main.pipeline_state.update(run_id="prior-run", provenance_status="recorded")
        result = await main.manual_refresh()
        task = main.current_pipeline_task
        self.assertNotEqual(result["state"]["run_id"], "prior-run")
        self.assertEqual(result["state"]["provenance_status"], "pending")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    async def test_action_audit_failure_survives_successful_run_finalization(self):
        (self.root / "audit.jsonl").mkdir()
        await main.manual_refresh()
        await main.current_pipeline_task
        self.assertEqual(main.pipeline_state.get("action_audit_status"), "write_failed")
        self.assertEqual(main.pipeline_state["provenance_status"], "recorded")

    async def test_metadata_write_failure_is_recorded_with_partial_episode(self):
        with patch.object(main, "attach_episode_record", side_effect=PermissionError("SECRET_SENTINEL")):
            await main.run_daily_pipeline()
        row = self.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "episode_metadata_write_failed")
        self.assertEqual(main.pipeline_state["provenance_error"], "episode_metadata_write_failed")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(row))

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

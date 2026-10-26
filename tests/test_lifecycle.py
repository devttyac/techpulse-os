"""TP26: managed retirement may unlink only an exclusively owned media object."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.state_store import StateStore, StoreIntegrityError
from src.lifecycle import LifecycleService, measure_storage


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self):
        with StateStore.create(self.root) as store:
            store.put_artifact("article-1", "article", b"shared evidence", {})
            for n in (1, 2):
                store.put_artifact(f"audio-{n}", "main_audio", f"audio {n}".encode(), {})
                store.publish_episode(f"ep-{n}", ["article-1", f"audio-{n}"],
                                      [f"coverage-{n}"], {"episode_number": n})
            old = store.artifact_file("audio-1")
        return old

    def test_retirement_keeps_evidence_and_removes_only_owned_old_media(self):
        old = self.fixture()
        result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["blocked"], 0)
        self.assertFalse(old.exists())
        with StateStore.open(self.root) as store:
            self.assertEqual(store.active_episode_ids(), ["ep-2"])
            self.assertEqual(store.coverage_owner("coverage-1"), "ep-1")
            self.assertEqual(store.read_artifact("article-1")[0], b"shared evidence")
            self.assertEqual(store.pending_lifecycle(), [])

    def test_active_lease_blocks_before_any_intent_or_unlink(self):
        old = self.fixture()
        with StateStore.open(self.root) as store:
            store.acquire_lifecycle_lease("reader-1", "audio-1")
        result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["blocked"], 1)
        self.assertTrue(old.exists())
        with StateStore.open(self.root) as store:
            self.assertEqual(store.active_episode_ids(), ["ep-1", "ep-2"])
            self.assertEqual(store.pending_lifecycle(), [])

    def test_result_write_failure_keeps_original_pending_and_never_retries(self):
        old = self.fixture()
        with patch.object(StateStore, "record_lifecycle_result", side_effect=OSError("disk full")):
            result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["observed_removed"], 1)
        self.assertEqual(result["observed_freed_bytes"], len(b"audio 1"))
        self.assertFalse(old.exists())
        with StateStore.open(self.root) as store:
            self.assertEqual(len(store.pending_lifecycle()), 1)
        observed = LifecycleService(self.root).reconcile_pending()
        self.assertEqual(observed["observed_absent"], 1)
        with StateStore.open(self.root) as store:
            self.assertEqual(store.pending_lifecycle(), [])

    def test_replaced_path_is_never_unlinked(self):
        old = self.fixture()
        with patch.object(LifecycleService, "_before_unlink", side_effect=lambda p: p.write_bytes(b"replacement")):
            result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(old.read_bytes(), b"replacement")
        observed = LifecycleService(self.root).reconcile_pending()
        self.assertEqual(observed["ambiguous"], 1)
        self.assertEqual(old.read_bytes(), b"replacement")
        with StateStore.open(self.root) as store:
            tombstone = store.pending_lifecycle()[0]
            fields = store.operation_events(tombstone["operation_id"])[1]["fields"]
            self.assertEqual(fields["file_inode"], old.stat().st_ino)
            self.assertEqual(fields["current_reference_count"], 0)
            self.assertEqual(fields["evidence_limit"], "current_inspection_only")

    def test_pre_effect_audit_failure_preserves_visible_episode_and_file(self):
        old = self.fixture()
        with patch.object(StateStore, "_insert_required_event", side_effect=OSError("audit down")):
            result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["blocked"], 1)
        self.assertTrue(old.exists())
        with StateStore.open(self.root) as store:
            self.assertEqual(store.active_episode_ids(), ["ep-1", "ep-2"])
            self.assertEqual(store.pending_lifecycle(), [])

    def test_shared_digest_blocks_retirement_and_keeps_both_files(self):
        with StateStore.create(self.root) as store:
            store.put_artifact("article-1", "article", b"evidence", {})
            for n in (1, 2):
                store.put_artifact(f"audio-{n}", "main_audio", b"same bytes", {})
                store.publish_episode(f"ep-{n}", ["article-1", f"audio-{n}"],
                                      [f"coverage-{n}"], {})
            audio = store.artifact_file("audio-1")
        result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["blocked"], 1)
        self.assertTrue(audio.exists())

    def test_new_same_digest_owner_after_tombstone_blocks_unlink(self):
        old = self.fixture()
        def add_owner(_path):
            with StateStore.open(self.root) as store:
                store.put_artifact("audio-late", "main_audio", b"audio 1", {})
                store.publish_episode("ep-3", ["audio-late"], ["coverage-3"], {})
        with patch.object(LifecycleService, "_before_unlink", side_effect=add_owner):
            result = LifecycleService(self.root).retire_to_limit(1)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["pending"], 1)
        self.assertTrue(old.exists())
        with StateStore.open(self.root) as store:
            self.assertEqual(store.coverage_owner("coverage-3"), "ep-3")
            self.assertEqual(len(store.pending_lifecycle()), 1)
        self.assertEqual(LifecycleService(self.root).reconcile_pending()["ambiguous"], 1)

    def test_audio_only_retirement_preserves_historical_coverage(self):
        with StateStore.create(self.root) as store:
            for n in (1, 2):
                store.put_artifact(f"audio-{n}", "main_audio", f"audio {n}".encode(), {})
                store.publish_episode(f"ep-{n}", [f"audio-{n}"],
                                      [f"coverage-{n}"], {"episode_number": n})
        self.assertEqual(LifecycleService(self.root).retire_to_limit(1)["removed"], 1)
        with StateStore.open(self.root) as store:
            self.assertEqual(store.coverage_owner("coverage-1"), "ep-1")
            self.assertEqual(store.episode("ep-1")["artifacts"], [])

    def test_intent_and_reconciliation_bind_checks_and_observed_identity(self):
        self.fixture()
        with patch.object(StateStore, "record_lifecycle_result", side_effect=OSError("audit down")):
            self.assertEqual(LifecycleService(self.root).retire_to_limit(1)["pending"], 1)
        with StateStore.open(self.root) as store:
            tombstone = store.pending_lifecycle()[0]
            intent = store.operation_events(tombstone["operation_id"])[0]["fields"]
            self.assertEqual(intent["prior_reference_count"], 1)
            self.assertEqual(intent["prior_lease_count"], 0)
            self.assertEqual(intent["intended_effect"], "unlink_owned_main_audio")
            self.assertEqual(intent["safe_path_digest"], tombstone["safe_path_digest"])
            self.assertEqual(intent["file_inode"], tombstone["inode"])
        self.assertEqual(LifecycleService(self.root).reconcile_pending()["observed_absent"], 1)
        with StateStore.open(self.root) as store:
            fields = store.operation_events(tombstone["operation_id"])[1]["fields"]
            self.assertEqual(fields["observed_state"], "observed_absent")
            self.assertEqual(fields["current_reference_count"], 0)
            self.assertEqual(fields["current_lease_count"], 0)
            self.assertEqual(fields["evidence_limit"], "current_inspection_only")

    def test_retired_media_cannot_acquire_new_lease_or_appear_available(self):
        self.fixture()
        LifecycleService(self.root).retire_to_limit(1)
        with StateStore.open(self.root) as store:
            with self.assertRaises(StoreIntegrityError):
                store.acquire_lifecycle_lease("late-reader", "audio-1")
            self.assertIsNone(store.read_artifact("audio-1"))
            self.assertEqual(store.list_artifact_ids(), ["article-1", "audio-2"])

    def test_corrupt_newer_member_blocks_count_based_retirement(self):
        old = self.fixture()
        with StateStore.open(self.root) as store:
            store.artifact_file("audio-2").write_bytes(b"corrupt")
        with self.assertRaises(StoreIntegrityError):
            LifecycleService(self.root).retire_to_limit(1)
        self.assertTrue(old.exists())

    def test_storage_measurement_includes_evidence_audit_and_staging(self):
        root = Path(self.tmp.name) / "data"
        for folder in ("episodes", "audio", "state/objects", "state/staging"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / "episodes/ep-1.json").write_bytes(b"123")
        (root / "audio/ep-1.mp3").write_bytes(b"12345")
        (root / "state/objects/blob").write_bytes(b"1234567")
        (root / "state/staging/pending").write_bytes(b"123456789")
        (root / "audit.jsonl").write_bytes(b"12345678901")
        usage = measure_storage(root)
        self.assertEqual(usage["disk_usage_bytes"], 35)
        self.assertEqual(usage["total_episodes"], 1)
        self.assertEqual(usage["total_audio"], 1)
        self.assertEqual(usage["bytes_by_class"]["evidence"], 7)
        self.assertEqual(usage["bytes_by_class"]["staging"], 9)

    def test_storage_measurement_classifies_managed_main_audio(self):
        data = Path(self.tmp.name) / "data"
        with StateStore.create(data / "state") as store:
            store.put_artifact("article-1", "article", b"article", {})
            store.put_artifact("audio-1", "main_audio", b"audio", {})
        usage = measure_storage(data)
        self.assertEqual(usage["total_audio"], 1)
        self.assertEqual(usage["bytes_by_class"]["audio"], 5)
        self.assertEqual(usage["bytes_by_class"]["evidence"], 7)


if __name__ == "__main__":
    unittest.main()

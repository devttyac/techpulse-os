"""TP02: durable store contract, using only disposable local directories."""

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.state_store import StateStore, StoreIntegrityError
from src.operations import OperationEvent


def create_v1_store(root):
    """Build an exact pre-TP28 fixture without calling the current creator."""
    root.mkdir()
    (root / "objects").mkdir()
    (root / "staging").mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    schema = (Path(__file__).resolve().parents[1] / "src/storage/schema_v1.sql").read_text()
    db.executescript(schema)
    db.execute("PRAGMA application_id = 1414549331")
    db.execute("PRAGMA user_version = 1")
    body = b"retained v1 article body"
    digest = hashlib.sha256(body).hexdigest()
    (root / "objects" / digest).write_bytes(body)
    db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
               ("article-v1", "article", digest, len(body), '{"source":"historical"}'))
    db.execute("INSERT INTO control_revisions VALUES ('settings_public', 'schedule', 1, ?)", (b"06:00",))
    db.execute("INSERT INTO operation_journal VALUES ('restore-198', 'restoration', 'pending', ?)",
               ('{"episode_id":"ep-198"}',))
    db.commit()
    db.close()


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="techpulse-state-")
        self.root = Path(self.tmp.name) / "retained"

    def tearDown(self):
        self.tmp.cleanup()

    def test_v3_run_tables_and_schedule_history_survive_restart(self):
        with StateStore.create(self.root) as store:
            self.assertEqual(store.schema_version(), 3)
            self.assertEqual(store.schedule_revisions(), [])
        with StateStore.open(self.root) as store:
            self.assertEqual(store.schema_version(), 3)
            self.assertEqual(store.schedule_revisions(), [])

    def test_singleton_run_claim_is_durable_and_compare_and_set(self):
        context = {"settings_revision": 1, "timezone_id": "Asia/Singapore",
                   "rule_sha256": "a" * 64, "started_utc": "2026-10-22T00:00:00+00:00",
                   "lower_utc": "2026-10-16T16:00:00+00:00"}
        with StateStore.create(self.root) as store:
            self.assertTrue(store.claim_run("run-a", "nonce-a", "boot-a", 123,
                                            "start-a", context, intent_event=OperationEvent.build(
                                                "run", "run-a", "intent",
                                                {"run_id": "run-a", "trigger": "manual"},
                                                component="scheduler")))
            self.assertFalse(store.claim_run("run-b", "nonce-b", "boot-a", 123,
                                             "start-b", context, intent_event=OperationEvent.build(
                                                 "run", "run-b", "intent",
                                                 {"run_id": "run-b", "trigger": "manual"},
                                                 component="scheduler")))
        with StateStore.open(self.root) as store:
            self.assertEqual(store.active_run()["run_id"], "run-a")
            self.assertTrue(store.finish_run("run-a", "nonce-a", "completed",
                result_event=OperationEvent.build("run", "run-a", "result",
                    {"run_id": "run-a", "trigger": "manual", "outcome": "completed"},
                    component="pipeline")))
            self.assertTrue(store.claim_run("run-b", "nonce-b", "boot-a", 123,
                                            "start-b", context, intent_event=OperationEvent.build(
                                                "run", "run-b", "intent",
                                                {"run_id": "run-b", "trigger": "manual"},
                                                component="scheduler")))

    def test_settings_snapshot_cannot_use_unaudited_control_writer(self):
        with StateStore.create(self.root) as store:
            with self.assertRaises(ValueError):
                store.write_control("settings_public", "settings_snapshot", 0, b"{}")
            self.assertIsNone(store.read_control("settings_public", "settings_snapshot"))

    def test_freshness_check_detects_existing_evidence(self):
        with StateStore.create(self.root) as store:
            self.assertFalse(store.has_retained_records())
            store.put_artifact("article-1", "article", b"body", {})
            self.assertTrue(store.has_retained_records())

    def test_explicit_creation_and_restart_preserve_blob_and_versioned_control(self):
        with StateStore.create(self.root) as store:
            digest = store.put_artifact("source-v1", "article", b"full source body", {"url": "https://example.test/a"})
            self.assertEqual(digest, "2d62dabb1017fdf5ded1ab58f6657dec39ebbaf913baa0ab82ee44390050e349")
            self.assertEqual(store.write_control("settings_public", "schedule", 0, b"06:00"), 1)
        with StateStore.open(self.root) as store:
            self.assertEqual(store.read_artifact("source-v1"), (b"full source body", {"url": "https://example.test/a"}))
            self.assertEqual(store.read_control("settings_public", "schedule"), (1, b"06:00"))
            with self.assertRaises(ValueError):
                store.write_control("settings_public", "schedule", 0, b"07:00")
            self.assertEqual(store.read_control("settings_public", "schedule"), (1, b"06:00"))

    def test_missing_database_in_existing_root_never_becomes_fresh_install(self):
        self.root.mkdir()
        (self.root / "legacy.json").write_text("{}")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)
        with self.assertRaises(StoreIntegrityError):
            StateStore.create(self.root)
        self.assertEqual((self.root / "legacy.json").read_text(), "{}")
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_corrupt_database_and_missing_or_changed_blob_fail_closed(self):
        with StateStore.create(self.root) as store:
            digest = store.put_artifact("source-v1", "article", b"complete", {})
        blob = self.root / "objects" / digest
        blob.write_bytes(b"changed!")
        with StateStore.open(self.root) as store:
            with self.assertRaises(StoreIntegrityError):
                store.read_artifact("source-v1")
            with self.assertRaises(StoreIntegrityError):
                store.publish_episode("ep-1", ["source-v1"], ["fact-1"], {})
            self.assertIsNone(store.episode("ep-1"))
        blob.unlink()
        with StateStore.open(self.root) as store:
            with self.assertRaises(StoreIntegrityError):
                store.read_artifact("source-v1")
        (self.root / "state.sqlite3").write_bytes(b"not a database")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)

    def test_interrupted_blob_install_leaves_no_admitted_record(self):
        with StateStore.create(self.root) as store:
            with patch("src.state_store.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    store.put_artifact("source-v1", "article", b"body", {})
        with StateStore.open(self.root) as store:
            self.assertIsNone(store.read_artifact("source-v1"))
            self.assertEqual(store.list_artifact_ids(), [])

    def test_publication_joins_and_coverage_commit_together_or_not_at_all(self):
        with StateStore.create(self.root) as store:
            store.put_artifact("source-v1", "article", b"article A", {})
            store.put_artifact("source-v2", "article", b"article B", {})
            store.publish_episode("ep-1", ["source-v1", "source-v2"], ["fact-1"], {"title": "one"})
            self.assertEqual(store.episode("ep-1"), {"metadata": {"title": "one"}, "artifacts": ["source-v1", "source-v2"]})
            self.assertEqual(store.coverage_owner("fact-1"), "ep-1")
            with self.assertRaises(sqlite3.IntegrityError):
                store.publish_episode("ep-2", ["source-v1"], ["fact-2", "fact-1"], {})
            self.assertIsNone(store.episode("ep-2"))
            self.assertIsNone(store.coverage_owner("fact-2"))
        with StateStore.open(self.root) as store:
            self.assertEqual(store.coverage_owner("fact-1"), "ep-1")

    def test_interrupted_operation_is_pending_after_restart_without_auto_retry(self):
        with StateStore.create(self.root) as store:
            store.begin_operation("restore-198", "restoration", {"episode_id": "ep-198"})
        with StateStore.open(self.root) as store:
            self.assertEqual(store.pending_operations(), [{"id": "restore-198", "kind": "restoration", "metadata": {"episode_id": "ep-198"}}])
            store.finish_operation("restore-198", "interrupted")
            self.assertEqual(store.pending_operations(), [])

    def test_root_with_uri_metacharacters_uses_the_literal_directory(self):
        root = Path(self.tmp.name) / "retained?copy#1"
        with StateStore.create(root) as store:
            store.put_artifact("a", "article", b"body", {})
        self.assertTrue((root / "state.sqlite3").is_file())
        self.assertTrue((root / "state.sqlite3").read_bytes().startswith(b"SQLite format 3"))
        with StateStore.open(root) as store:
            self.assertEqual(store.read_artifact("a"), (b"body", {}))

    def test_symlinked_object_directory_is_not_admitted(self):
        with StateStore.create(self.root):
            pass
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "objects").rmdir()
        (self.root / "objects").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)

    def test_symlinked_object_cannot_be_admitted_from_outside_retained_root(self):
        outside = Path(self.tmp.name) / "external-body"
        outside.write_bytes(b"body")
        digest = "230d8358dc8e8890b4c58deeb62912ee2f20357ae92a5cc861b98e68fe31acb5"
        with StateStore.create(self.root) as store:
            (self.root / "objects" / digest).symlink_to(outside)
            with self.assertRaises(StoreIntegrityError):
                store.put_artifact("source-v1", "article", b"body", {})
            self.assertEqual(store.list_artifact_ids(), [])

    def test_inventory_and_coverage_refuse_missing_or_corrupt_committed_blob(self):
        with StateStore.create(self.root) as store:
            digest = store.put_artifact("source-v1", "article", b"complete", {})
            store.publish_episode("ep-1", ["source-v1"], ["fact-1"], {})
        blob = self.root / "objects" / digest
        blob.unlink()
        with StateStore.open(self.root) as store:
            with self.assertRaises(StoreIntegrityError):
                store.list_artifact_ids()
            with self.assertRaises(StoreIntegrityError):
                store.coverage_owner("fact-1")
        blob.write_bytes(b"corrupt!")
        with StateStore.open(self.root) as store:
            with self.assertRaises(StoreIntegrityError):
                store.list_artifact_ids()
            with self.assertRaises(StoreIntegrityError):
                store.coverage_owner("fact-1")

    def test_v1_migrates_without_rewriting_control_or_old_pending_journal(self):
        create_v1_store(self.root)
        before = (self.root / "state.sqlite3").stat().st_size
        with StateStore.open(self.root) as store:
            self.assertEqual(store.read_control("settings_public", "schedule"), (1, b"06:00"))
            self.assertEqual(store.read_artifact("article-v1"), (b"retained v1 article body", {"source": "historical"}))
            self.assertEqual(store.pending_operations(), [
                {"id": "restore-198", "kind": "restoration", "metadata": {"episode_id": "ep-198"}}
            ])
            self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(store.operation_events("restore-198"), [])
        self.assertGreaterEqual((self.root / "state.sqlite3").stat().st_size, before)
        with StateStore.open(self.root) as store:
            self.assertEqual(store.pending_operations()[0]["id"], "restore-198")

    def test_unknown_or_partial_schema_fails_closed_without_migration(self):
        create_v1_store(self.root)
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            db.execute("PRAGMA user_version = 3")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            db.execute("PRAGMA user_version = 2")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)

    def test_v2_table_shape_corruption_fails_closed(self):
        with StateStore.create(self.root):
            pass
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            db.execute("DROP TABLE diagnostic_events")
            db.execute("CREATE TABLE diagnostic_events (sequence INTEGER PRIMARY KEY)")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)

    def test_failed_v1_migration_rolls_back_new_tables_and_preserves_v1(self):
        create_v1_store(self.root)
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            db.execute("CREATE TABLE operation_events (obstruction TEXT)")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM control_revisions WHERE control_key = 'schedule'").fetchone()[0], b"06:00")
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name = 'effect_intents'").fetchone())

    def test_resolved_effect_without_matching_result_fails_closed_on_open(self):
        with StateStore.create(self.root) as store:
            store.begin_effect("delete-1", "deletion",
                OperationEvent.build("deletion", "delete-1", "intent", {"artifact_id": "article-1"},
                                     component="lifecycle"))
            store.record_effect_result("delete-1", "verified_removed",
                OperationEvent.build("deletion", "delete-1", "result",
                                     {"artifact_id": "article-1", "outcome": "verified_removed"},
                                     component="lifecycle"))
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            db.execute("UPDATE effect_intents SET resolution_event_id = NULL WHERE operation_id = 'delete-1'")
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)

    def test_resolved_effect_pointing_at_other_operation_result_fails_closed(self):
        with StateStore.create(self.root) as store:
            for index in (1, 2):
                operation_id = f"delete-{index}"
                artifact_id = f"article-{index}"
                store.begin_effect(operation_id, "deletion",
                    OperationEvent.build("deletion", operation_id, "intent",
                                         {"artifact_id": artifact_id}, component="lifecycle"))
                store.record_effect_result(operation_id, "verified_removed",
                    OperationEvent.build("deletion", operation_id, "result",
                                         {"artifact_id": artifact_id, "outcome": "verified_removed"},
                                         component="lifecycle"))
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            wrong = db.execute("SELECT resolution_event_id FROM effect_intents WHERE operation_id = 'delete-2'").fetchone()[0]
            db.execute("UPDATE effect_intents SET resolution_event_id = ? WHERE operation_id = 'delete-1'", (wrong,))
        with self.assertRaises(StoreIntegrityError):
            StateStore.open(self.root)


if __name__ == "__main__":
    unittest.main()

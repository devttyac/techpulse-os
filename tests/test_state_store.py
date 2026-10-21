"""TP02: durable store contract, using only disposable local directories."""

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.state_store import StateStore, StoreIntegrityError


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="techpulse-state-")
        self.root = Path(self.tmp.name) / "retained"

    def tearDown(self):
        self.tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main()

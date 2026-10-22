"""TP23 contract tests for a durable, whole, nonsecret Settings revision."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import settings as settings_module
from src.settings import (SettingsConflict, SettingsIntegrityError,
                          SettingsService, SettingsValidationError,
                          assess_model)
from src.state_store import StateStore


VALID = {
    "max_episodes_retained": 14,
    "cron_schedule": "0 7 * * *",
    "timezone_id": "Asia/Singapore",
    "chat_provider": "gemini",
    "chat_model": "gemini-3.6-flash",
}


class SettingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_explicit_fresh_save_survives_reopen_with_disabled_model(self):
        result = SettingsService(self.root).save(VALID, expected_revision=0,
                                                 initialization="fresh")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["config"], VALID)
        self.assertEqual(result["selected_model_status"], "disabled")
        self.assertFalse(result["approved_chat_available"])
        self.assertEqual(result["legacy_chat_route_status"], "unconverted_unapproved")
        self.assertEqual(result["model_registry"][0]["current_bindings"]["model_version"],
                         "3.6-flash-07-2026")
        self.assertIsNone(result["model_registry"][0]["current_bindings"]["price_revision"])
        self.assertEqual(SettingsService(self.root).read()["config"], VALID)

    def test_invalid_whole_save_keeps_prior_revision(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        for field, bad in (("max_episodes_retained", 0),
                           ("max_episodes_retained", 1.5),
                           ("cron_schedule", "bad cron"),
                           ("timezone_id", "Mars/Nowhere"),
                           ("chat_model", "unknown-model")):
            with self.subTest(field=field, bad=bad):
                with self.assertRaises(SettingsValidationError):
                    service.save({**VALID, field: bad}, expected_revision=1)
                self.assertEqual(service.read()["revision"], 1)
                self.assertEqual(service.read()["config"], VALID)

    def test_stale_revision_and_partial_save_are_rejected(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        with self.assertRaises(SettingsConflict):
            service.save({**VALID, "max_episodes_retained": 30}, expected_revision=0)
        with self.assertRaises(SettingsValidationError):
            service.save({"max_episodes_retained": 30}, expected_revision=1)
        self.assertEqual(service.read()["config"], VALID)

    def test_legacy_configuration_needs_explicit_migration_disposition(self):
        (self.root / "config.json").write_text(json.dumps({
            "max_episodes_retained": 30, "chapters_per_episode": 8,
            "gemini_model": "gemini-3.6-flash", "cron_schedule": "0 6 * * *",
        }))
        service = SettingsService(self.root)
        with self.assertRaises(SettingsIntegrityError):
            service.read()
        with self.assertRaises(SettingsIntegrityError):
            service.save(VALID, expected_revision=0, initialization="fresh")
        proposed = {**VALID, "max_episodes_retained": 30,
                    "cron_schedule": "0 6 * * *"}
        self.assertEqual(service.save(proposed, expected_revision=0,
                                      initialization="migrate")["revision"], 1)
        self.assertEqual(service.read()["config"], proposed)
        self.assertTrue((self.root / "config.json").is_file())

    def test_malformed_legacy_configuration_cannot_be_silently_replaced(self):
        (self.root / "config.json").write_text("{not json")
        with self.assertRaises(SettingsIntegrityError):
            SettingsService(self.root).save(VALID, expected_revision=0,
                                            initialization="migrate")
        self.assertFalse((self.root / "state").exists())

    def test_nonstring_legacy_cron_fails_closed_without_creating_state(self):
        (self.root / "config.json").write_text(json.dumps({"cron_schedule": 7}))
        with self.assertRaises(SettingsIntegrityError):
            SettingsService(self.root).save(VALID, expected_revision=0,
                                            initialization="migrate")
        self.assertFalse((self.root / "state").exists())

    def test_settings_share_one_state_store_with_other_control_namespaces(self):
        SettingsService(self.root).save(VALID, expected_revision=0,
                                        initialization="fresh")
        with StateStore.open(self.root / "state") as store:
            self.assertEqual(store.read_control("settings_public", "settings_snapshot")[0], 1)
            self.assertEqual(len(store.operation_events("settings-revision-1")), 1)
            self.assertEqual(store.write_control("restoration", "operator_gate", 0, b"held"), 1)
        with StateStore.open(self.root / "state") as store:
            self.assertEqual(store.read_control("restoration", "operator_gate"), (1, b"held"))

    def test_existing_state_store_cannot_reclassify_corrupt_legacy_as_fresh(self):
        with StateStore.create(self.root / "state"):
            pass
        (self.root / "config.json").write_text("{bad")
        with self.assertRaises(SettingsIntegrityError):
            SettingsService(self.root).save(VALID, expected_revision=0,
                                            initialization="fresh")
        with StateStore.open(self.root / "state") as store:
            self.assertIsNone(store.read_control("settings_public", "settings_snapshot"))

    def test_existing_article_evidence_store_requires_migration_disposition(self):
        with StateStore.create(self.root / "state") as store:
            store.put_artifact("article-1", "article", b"source body", {})
        with self.assertRaises(SettingsIntegrityError):
            SettingsService(self.root).save(VALID, expected_revision=0,
                                            initialization="fresh")
        with StateStore.open(self.root / "state") as store:
            self.assertEqual(store.list_artifact_ids(), ["article-1"])
            self.assertIsNone(store.read_control("settings_public", "settings_snapshot"))

    def test_corrupt_saved_snapshot_blocks_read_and_overwrite(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        with StateStore.open(self.root / "state") as store:
            store._db.execute("UPDATE control_revisions SET value = ? WHERE control_key = ?",
                              (b"{corrupt", "settings_snapshot"))
            store._db.commit()
        with self.assertRaises(SettingsIntegrityError):
            service.read()
        with self.assertRaises(SettingsIntegrityError):
            service.save(VALID, expected_revision=1)

    def test_failed_required_audit_rolls_back_settings_revision(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        with patch.object(StateStore, "_insert_required_event", side_effect=OSError("audit failed")):
            with self.assertRaises(OSError):
                service.save({**VALID, "max_episodes_retained": 30},
                             expected_revision=1)
        self.assertEqual(service.read()["config"], VALID)
        self.assertEqual(service.read()["revision"], 1)

    def test_post_commit_read_failure_cannot_turn_success_into_unknown_outcome(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        with patch.object(SettingsService, "read", side_effect=OSError("late read failed")):
            result = service.save({**VALID, "max_episodes_retained": 30},
                                  expected_revision=1)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(SettingsService(self.root).read()["config"]["max_episodes_retained"], 30)

    def test_registry_binding_change_stales_approval_but_unrelated_save_does_not(self):
        approved = {
            "provider": "gemini", "model": "gemini-3.6-flash",
            "approval_status": "approved", "approval_ref": "review-fixture-1",
            "bindings": {"api_endpoint": "v1beta/models/gemini-3.6-flash",
                         "model_version": "3.6-flash-07-2026", "price_revision": "price-1",
                         "capability_revision": "cap-1", "grounding_revision": "ground-1",
                         "citation_revision": "cite-1", "pi_revision": "pi-1"},
        }
        current = dict(approved["bindings"])
        self.assertEqual(assess_model(approved, current)["status"], "approved")
        SettingsService(self.root).save(VALID, expected_revision=0, initialization="fresh")
        SettingsService(self.root).save({**VALID, "max_episodes_retained": 30},
                                        expected_revision=1)
        self.assertEqual(assess_model(approved, current)["status"], "approved")
        self.assertEqual(assess_model(approved, {**current, "price_revision": "price-2"})["status"], "stale")
        self.assertEqual(assess_model(approved, {**current, "pi_revision": None})["status"], "stale")

    def test_registry_approval_alone_does_not_claim_current_chat_adapter_ready(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        approved = {**settings_module.MODEL_REGISTRY[0],
                    "approval_status": "approved", "approval_ref": "review-fixture-1",
                    "bindings": {key: "fixture-1" for key in settings_module.MODEL_BINDINGS}}
        current = dict(approved["bindings"])
        with patch.object(settings_module, "MODEL_REGISTRY", (approved,)), \
             patch.object(settings_module, "CURRENT_MODEL_BINDINGS", current), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-secret"}):
            result = service.read()
        self.assertEqual(result["selected_model_status"], "approved")
        self.assertFalse(result["approved_chat_available"])
        self.assertEqual(result["application_status"]["chat"],
                         "blocked_pending_model_and_adapter_gate")


if __name__ == "__main__":
    unittest.main()

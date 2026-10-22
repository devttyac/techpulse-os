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

    def test_unaudited_settings_row_is_not_read_or_advanced(self):
        with StateStore.create(self.root / "state") as store:
            encoded = json.dumps({"schema_version": 1, "config": VALID}).encode()
            store._db.execute("INSERT INTO control_revisions VALUES (?, ?, ?, ?)",
                              ("settings_public", "settings_snapshot", 1, encoded))
            store._db.commit()
        service = SettingsService(self.root)
        with self.assertRaises(SettingsIntegrityError):
            service.read()
        with self.assertRaises(SettingsIntegrityError):
            service.save(VALID, expected_revision=1)

    def test_valid_shape_tampered_settings_bytes_fail_audit_on_read_and_save(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        with StateStore.open(self.root / "state") as store:
            revision, raw = store.read_control("settings_public", "settings_snapshot")
            tampered = json.loads(raw)
            tampered["config"]["max_episodes_retained"] = 30
            encoded = json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
            store._db.execute(
                "UPDATE control_revisions SET value = ? WHERE namespace = ? AND control_key = ?",
                (encoded, "settings_public", "settings_snapshot"))
            store._db.commit()
        self.assertEqual(revision, 1)
        with self.assertRaises(SettingsIntegrityError):
            service.read()
        with self.assertRaises(SettingsIntegrityError):
            service.save({**VALID, "max_episodes_retained": 60}, expected_revision=1)
        with StateStore.open(self.root / "state") as store:
            self.assertEqual(store.read_control("settings_public", "settings_snapshot"),
                             (1, encoded))
            self.assertEqual(store.operation_events("settings-revision-2"), [])

    def test_fresh_defaults_do_not_invent_schedule_or_approved_model(self):
        self.assertEqual(SettingsService.fresh_defaults(), {
            "max_episodes_retained": 14, "timezone_id": "Asia/Singapore"})
        self.assertEqual(SettingsService(self.root).initialization_kind(), "fresh")
        (self.root / "config.json").write_text("{}")
        self.assertEqual(SettingsService(self.root).initialization_kind(), "migrate")

    def test_unknown_retained_file_cannot_be_classified_as_fresh(self):
        (self.root / "unknown-retained.bin").write_bytes(b"existing")
        service = SettingsService(self.root)
        self.assertEqual(service.initialization_kind(), "migrate")
        with self.assertRaises(SettingsIntegrityError):
            service.save(VALID, expected_revision=0, initialization="fresh")
        self.assertFalse((self.root / "state").exists())

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
            "review_evidence": {"real_article_pass_ref": "fixture-1"},
            "bindings": {key: "fixture-1" for key in settings_module.MODEL_BINDINGS},
        }
        approved["bindings"].update({
            "api_endpoint": "v1beta/models/gemini-3.6-flash",
            "model_version": "3.6-flash-07-2026"})
        current = dict(approved["bindings"])
        self.assertEqual(assess_model(approved, current)["status"], "approved")
        SettingsService(self.root).save(VALID, expected_revision=0, initialization="fresh")
        SettingsService(self.root).save({**VALID, "max_episodes_retained": 30},
                                        expected_revision=1)
        self.assertEqual(assess_model(approved, current)["status"], "approved")
        self.assertEqual(assess_model(approved, {**current, "price_revision": "price-2"})["status"], "stale")
        self.assertEqual(assess_model(approved, {**current, "pi_revision": None})["status"], "stale")

    def test_registry_evidence_is_retained_across_irrelevant_settings_save(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        first = service.read()
        self.assertEqual(first["registry_revision"], 1)
        self.assertEqual(first["model_registry"][0]["status"], "disabled")
        self.assertEqual(first["model_registry"][0]["evidence_bindings"]["api_endpoint"],
                         "v1beta/models/gemini-3.6-flash")
        self.assertIsNone(first["model_registry"][0]["evidence_bindings"]["price_revision"])
        self.assertEqual(first["model_registry"][0]["budget"]["max_usd"], 0.05)
        service.save({**VALID, "max_episodes_retained": 30}, expected_revision=1)
        second = SettingsService(self.root).read()
        self.assertEqual(second["registry_revision"], 1)
        self.assertEqual(second["model_registry"], first["model_registry"])
        with StateStore.open(self.root / "state") as store:
            raw = store.read_control("settings_public", "settings_snapshot")[1]
        self.assertIn(b'"registry"', raw)

    def test_reviewed_model_transition_is_internal_and_remains_runtime_blocked(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        with self.assertRaises(SettingsValidationError):
            service.record_model_approval({}, expected_revision=1)
        self.assertEqual(service.read()["revision"], 1)

    def test_registry_approval_alone_does_not_claim_current_chat_adapter_ready(self):
        service = SettingsService(self.root)
        service.save(VALID, expected_revision=0, initialization="fresh")
        bindings = {key: "fixture-1" for key in settings_module.MODEL_BINDINGS}
        for key in ("api_endpoint", "model_version", "budget_revision"):
            bindings[key] = settings_module.CURRENT_MODEL_BINDINGS[key]
        review = {key: {"verdict": "PASS", "artifact_sha256": "a" * 64,
                        "reference": "fixture-pass-1"} for key in (
            "real_article_pass_ref", "price_pass_ref", "capability_pass_ref",
            "adapter_pass_ref", "budget_pass_ref", "grounding_pass_ref",
            "citation_pass_ref", "pi_pass_ref")}
        review.update({"decision": "approved", "reviewed_settings_revision": 1,
                       "bindings": bindings, "approval_ref": "fixture-approved-1",
                       "approval_event": {"review_stage": "TP44", "verdict": "PASS",
                                          "reviewed_settings_revision": 1,
                                          "approval_ref": "fixture-approved-1",
                                          "report_sha256": "b" * 64}})
        with self.assertRaises(SettingsValidationError):
            service.record_model_approval({**review, "price_pass_ref": "mere string"},
                                          expected_revision=1)
        with patch.object(settings_module, "CURRENT_MODEL_BINDINGS", bindings):
            service.record_model_approval(review, expected_revision=1)
            with patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-secret"}):
                result = SettingsService(self.root).read()
            service.save({**VALID, "max_episodes_retained": 30,
                          "timezone_id": "UTC"}, expected_revision=2)
            self.assertEqual(service.read()["selected_model_status"], "approved")
        self.assertEqual(result["selected_model_status"], "approved")
        self.assertEqual(result["registry_revision"], 2)
        with patch.object(settings_module, "CURRENT_MODEL_BINDINGS",
                          {**bindings, "price_revision": "fixture-price-2"}):
            self.assertEqual(service.read()["selected_model_status"], "stale")
        with patch.object(settings_module, "CURRENT_MODEL_BINDINGS",
                          {**bindings, "model_version": "3.6-flash-next"}):
            self.assertEqual(service.read()["selected_model_status"], "stale")
        self.assertNotEqual(service.read()["selected_model_status"], "approved")
        service.record_model_binding_change("price_revision", "fixture-price-2",
                                            reason_ref="price-change-fixture",
                                            expected_revision=3)
        self.assertEqual(service.read()["selected_model_status"], "stale")
        self.assertEqual(service.read()["registry_revision"], 3)
        self.assertFalse(result["approved_chat_available"])
        self.assertEqual(result["application_status"]["chat"],
                         "blocked_pending_model_and_adapter_gate")


if __name__ == "__main__":
    unittest.main()

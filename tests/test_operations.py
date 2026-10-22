"""TP28 required audit, pending effects, and bounded diagnostics."""

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.operations import OperationEvent, OperationPolicy, membership_digest, record_diagnostic
from src.provenance import append_record, read_operation_history
from src.state_store import StateStore


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, operation_id="settings-1", phase="result", **fields):
        values = {"control_key": "schedule", "control_revision": 1, **fields}
        return OperationEvent.build("settings", operation_id, phase, values,
                                    component="settings")

    def build(self, kind, operation_id, phase, fields, **options):
        components = {"settings": "settings", "publication": "publisher",
                      "deletion": "lifecycle", "restoration": "restoration",
                      "run": "scheduler", "health_detail": "health"}
        component = options.pop("component", components[kind])
        return OperationEvent.build(kind, operation_id, phase, fields,
                                    component=component, **options)

    def publication_event(self, operation_id="publish-1", **changes):
        fields = {"outcome": "committed", "episode_id": "ep-1",
                  "episode_revision": hashlib.sha256(b"{}").hexdigest(),
                  "evidence_digest": membership_digest(["article-1"]),
                  "coverage_digest": membership_digest(["coverage-1"]),
                  "evidence_count": 1, "coverage_count": 1, **changes}
        return self.build("publication", operation_id, "result", fields)

    def test_event_rejects_secret_bearing_and_unknown_payload_fields(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-canary-123"}):
            with self.assertRaises(ValueError):
                self.event(detail="secret-canary-123")
            with self.assertRaises(ValueError):
                self.event(provider_key="secret-canary-123")
            with self.assertRaises(ValueError):
                self.event(error="request failed at https://example.test/?key=secret-canary-123")
        event = self.event(outcome="committed", episode_id="ep-1", count=3)
        self.assertEqual(event.fields, {"control_key": "schedule", "control_revision": 1,
                                        "outcome": "committed", "episode_id": "ep-1", "count": 3})
        self.assertNotIn("secret", repr(event))

    def test_policy_classifies_required_and_diagnostic_before_effect(self):
        self.assertEqual(OperationPolicy.duty("settings"), "required")
        self.assertEqual(OperationPolicy.duty("publication"), "required")
        self.assertEqual(OperationPolicy.duty("deletion"), "required")
        self.assertEqual(OperationPolicy.duty("health_detail"), "diagnostic")
        with self.assertRaises(ValueError):
            OperationPolicy.duty("unknown")

    def test_required_event_needs_component_correlation_and_safe_outcome(self):
        with self.assertRaises((TypeError, ValueError)):
            OperationEvent.build("settings", "settings-1", "result", {"outcome": "committed"})
        with self.assertRaises(ValueError):
            self.build("settings", "settings-1", "result", {}, component="settings")
        with self.assertRaises(ValueError):
            self.build("settings", "settings-1", "result",
                                 {"outcome": "sk_test_abcdefghijklmnop", "control_key": "schedule"},
                                 component="settings")
        with self.assertRaises(ValueError):
            self.build("settings", "settings-1", "result",
                                 {"reason": "sk_test_abcdefghijklmnop", "control_key": "schedule"},
                                 component="settings")
        with self.assertRaises(ValueError):
            self.build("settings", "settings-1", "result",
                       {"outcome": "committed", "control_key": "sk_test_abcdefghijklmnop"})
        with self.assertRaises(ValueError):
            self.build("settings", "settings-1", "result", {"outcome": "committed"})

    def test_scheduled_run_requires_bounded_window_context(self):
        with self.assertRaises(ValueError):
            self.build("run", "run-1", "result",
                                 {"outcome": "completed", "run_id": "run-1", "trigger": "scheduled"},
                                 component="scheduler")
        event = self.build("run", "run-1", "result",
                                     {"outcome": "completed", "run_id": "run-1", "trigger": "scheduled"},
                                     component="scheduler", context={
                                         "nominal_slot": "2026-10-21T08:00:00+08:00",
                                         "schedule_revision": 2,
                                         "resolved_instant": "2026-10-21T00:00:00+00:00",
                                         "timezone": "Asia/Singapore",
                                         "rule_revision": "2026a",
                                         "actual_started": "2026-10-21T00:01:00+00:00",
                                         "actual_finished": "2026-10-21T00:03:00+00:00",
                                         "disposition": "on_time",
                                     })
        self.assertEqual(event.context["disposition"], "on_time")
        with StateStore.create(self.root) as store:
            store.record_run_event(event)
        with StateStore.open(self.root) as store:
            self.assertEqual(store.operation_events("run-1")[0]["context"], event.context)

    def test_scheduled_gap_and_future_only_preserve_unknown_instants_as_null(self):
        base = {"nominal_slot": "2026-03-08T02:30:00",
                "schedule_revision": 2, "resolved_instant": None,
                "timezone": "America/New_York", "rule_revision": "2026a",
                "actual_started": None, "actual_finished": None,
                "disposition": "gap"}
        with StateStore.create(self.root) as store:
            for operation_id, disposition in (("run-gap", "gap"),
                                              ("run-future", "future_only"),
                                              ("run-skipped", "skipped")):
                context = {**base, "disposition": disposition}
                event = self.build("run", operation_id, "result",
                                   {"outcome": "skipped", "run_id": operation_id,
                                    "trigger": "scheduled"}, context=context)
                store.record_run_event(event)
        with StateStore.open(self.root) as store:
            for operation_id in ("run-gap", "run-future", "run-skipped"):
                saved = store.operation_events(operation_id)[0]["context"]
                self.assertIsNone(saved["resolved_instant"])
                self.assertIsNone(saved["actual_started"])
                self.assertIsNone(saved["actual_finished"])

    def test_scheduled_window_rejects_known_result_with_missing_or_contradictory_bounds(self):
        base = {"nominal_slot": "2026-10-21T08:00:00",
                "schedule_revision": 2,
                "resolved_instant": "2026-10-21T00:00:00+00:00",
                "timezone": "Asia/Singapore", "rule_revision": "2026a",
                "actual_started": "2026-10-21T00:01:00+00:00",
                "actual_finished": "2026-10-21T00:03:00+00:00",
                "disposition": "on_time"}
        cases = (
            ({"resolved_instant": None}, "completed"),
            ({"actual_started": None, "actual_finished": None}, "completed"),
            ({"actual_started": None}, "completed"),
            ({"disposition": "future_only"}, "completed"),
            ({"disposition": "skipped"}, "completed"),
            ({"disposition": "skipped"}, "call_returned"),
            ({"disposition": "skipped", "actual_finished": None}, "started"),
            ({}, "pending"),
            ({"actual_started": "2026-10-21T00:01:00+00:00"}, "skipped"),
        )
        for changes, outcome in cases:
            with self.subTest(changes=changes, outcome=outcome), self.assertRaises(ValueError):
                self.build("run", "run-1", "result",
                           {"outcome": outcome, "run_id": "run-1", "trigger": "scheduled"},
                           context={**base, **changes})

    def test_event_id_and_original_observation_time_survive_delayed_persistence(self):
        with StateStore.create(self.root) as store:
            event = self.build("run", "run-1", "result",
                                         {"outcome": "call_returned", "run_id": "run-1", "trigger": "manual"},
                                         component="scheduler")
            delayed = replace(event, observed_at="2026-10-20T12:00:00+00:00")
            store.record_run_event(delayed)
            stored = store.operation_events("run-1")[0]
            self.assertEqual(stored["event_id"], delayed.event_id)
            self.assertEqual(stored["observed_at"], delayed.observed_at)
            self.assertEqual(stored["component"], "scheduler")

    def test_run_observation_survives_restart_without_claiming_an_effect(self):
        with StateStore.create(self.root) as store:
            store.record_run_event(self.build("run", "run-1", "result",
                                                        {"outcome": "call_returned", "count": 3,
                                                         "run_id": "run-1", "trigger": "manual"}))
        with StateStore.open(self.root) as store:
            events = store.operation_events("run-1")
            self.assertEqual(events[0]["fields"]["outcome"], "call_returned")
            self.assertEqual(store.pending_effects(), [])

    def test_required_settings_audit_is_atomic_with_control_revision(self):
        with StateStore.create(self.root) as store:
            revision = store.write_control_audited("settings_public", "schedule", 0, b"06:00",
                                                   self.event(outcome="committed"))
            self.assertEqual(revision, 1)
            self.assertEqual(len(store.operation_events("settings-1")), 1)
            with patch.object(store, "_insert_required_event", side_effect=OSError("audit failed")):
                with self.assertRaises(OSError):
                    store.write_control_audited("settings_public", "schedule", 1, b"07:00",
                                                self.event("settings-2", outcome="committed", control_revision=2))
            self.assertEqual(store.read_control("settings_public", "schedule"), (1, b"06:00"))
            self.assertEqual(store.operation_events("settings-2"), [])

    def test_settings_audit_rejects_wrong_key_revision_or_outcome_before_commit(self):
        with StateStore.create(self.root) as store:
            for fields in (
                {"outcome": "committed", "control_key": "timezone", "control_revision": 1},
                {"outcome": "committed", "control_key": "schedule", "control_revision": 999},
                {"outcome": "failed", "control_key": "schedule", "control_revision": 1},
            ):
                event = self.build("settings", "settings-1", "result", fields,
                                             component="settings")
                with self.assertRaises(ValueError):
                    store.write_control_audited("settings_public", "schedule", 0, b"06:00", event)
                self.assertIsNone(store.read_control("settings_public", "schedule"))
                self.assertEqual(store.operation_events("settings-1"), [])

    def test_required_audit_failure_blocks_publication_and_keeps_prior_readable(self):
        with StateStore.create(self.root) as store:
            store.put_artifact("article-1", "article", b"complete body", {})
            with patch.object(store, "_insert_required_event", side_effect=OSError("audit failed")):
                with self.assertRaises(OSError):
                    store.publish_episode_audited("ep-1", ["article-1"], ["coverage-1"], {},
                                                  self.publication_event())
            self.assertIsNone(store.episode("ep-1"))
            self.assertIsNone(store.coverage_owner("coverage-1"))
            self.assertEqual(store.read_artifact("article-1")[0], b"complete body")

    def test_publication_audit_commits_exact_episode_revision_and_membership(self):
        with StateStore.create(self.root) as store:
            store.put_artifact("article-1", "article", b"complete body", {})
            event = self.publication_event()
            store.publish_episode_audited("ep-1", ["article-1"], ["coverage-1"], {}, event)
        with StateStore.open(self.root) as store:
            self.assertEqual(store.episode("ep-1")["artifacts"], ["article-1"])
            self.assertEqual(store.coverage_owner("coverage-1"), "ep-1")
            saved = store.operation_events("publish-1")[0]
            self.assertEqual(saved["event_id"], event.event_id)
            self.assertEqual(saved["fields"]["episode_revision"], hashlib.sha256(b"{}").hexdigest())
            self.assertEqual(saved["fields"]["coverage_digest"], membership_digest(["coverage-1"]))

    def test_publication_audit_rejects_wrong_episode_evidence_or_outcome(self):
        with StateStore.create(self.root) as store:
            store.put_artifact("article-1", "article", b"complete body", {})
            for changes in (
                {"episode_id": "ep-other"},
                {"episode_revision": "0" * 64},
                {"evidence_digest": membership_digest(["article-other"])},
                {"coverage_digest": membership_digest(["fact-other"])},
                {"outcome": "failed"},
            ):
                event = self.publication_event(**changes)
                with self.assertRaises(ValueError):
                    store.publish_episode_audited("ep-1", ["article-1"], ["coverage-1"], {}, event)
                self.assertIsNone(store.episode("ep-1"))
                self.assertEqual(store.operation_events("publish-1"), [])

    def test_post_effect_audit_failure_keeps_original_pending_after_restart(self):
        with StateStore.create(self.root) as store:
            intent = self.build("deletion", "delete-1", "intent", {"artifact_id": "article-1"})
            store.begin_effect("delete-1", "deletion", intent)
            with patch.object(store, "_insert_required_event", side_effect=OSError("audit failed")):
                with self.assertRaises(OSError):
                    store.record_effect_result("delete-1", "verified_removed",
                                               self.build("deletion", "delete-1", "result", {"outcome": "verified_removed", "artifact_id": "article-1"}))
        with StateStore.open(self.root) as store:
            pending = store.pending_effects()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["id"], "delete-1")
            self.assertEqual(pending[0]["effect_status"], "unknown")
            self.assertEqual([e["phase"] for e in store.operation_events("delete-1")], ["intent"])
            store.reconcile_effect("delete-1", "observed_absent",
                                   self.build("deletion", "delete-1", "reconciliation", {"outcome": "observed_absent", "artifact_id": "article-1"}))
            self.assertEqual(store.pending_effects(), [])

    def test_pre_effect_intent_failure_never_authorizes_effect(self):
        with StateStore.create(self.root) as store:
            intent = self.build("deletion", "delete-1", "intent", {"artifact_id": "article-1"})
            with patch.object(store, "_insert_required_event", side_effect=OSError("audit unavailable")):
                with self.assertRaises(OSError):
                    store.begin_effect("delete-1", "deletion", intent)
            self.assertEqual(store.pending_effects(), [])
            self.assertEqual(store.operation_events("delete-1"), [])

    def test_verified_result_closes_bound_intent_and_mismatch_cannot_close_it(self):
        with StateStore.create(self.root) as store:
            store.begin_effect("delete-1", "deletion",
                               self.build("deletion", "delete-1", "intent", {"artifact_id": "article-1"}))
            with self.assertRaises(ValueError):
                store.record_effect_result("delete-1", "verified_removed",
                    self.build("deletion", "delete-1", "result", {"artifact_id": "article-2", "outcome": "verified_removed"}))
            self.assertEqual(len(store.pending_effects()), 1)
            store.record_effect_result("delete-1", "verified_removed",
                self.build("deletion", "delete-1", "result", {"artifact_id": "article-1", "outcome": "verified_removed"}))
            self.assertEqual(store.pending_effects(), [])
            self.assertEqual([event["phase"] for event in store.operation_events("delete-1")], ["intent", "result"])

    def test_ambiguous_reconciliation_remains_pending_without_repeating_effect(self):
        with StateStore.create(self.root) as store:
            store.begin_effect("delete-1", "deletion",
                               self.build("deletion", "delete-1", "intent", {"artifact_id": "article-1"}))
            store.reconcile_effect("delete-1", "ambiguous",
                self.build("deletion", "delete-1", "reconciliation", {"artifact_id": "article-1", "outcome": "ambiguous"}))
        with StateStore.open(self.root) as store:
            self.assertEqual(store.pending_effects()[0]["effect_status"], "ambiguous")

    def test_diagnostic_failure_warns_without_undoing_required_effect(self):
        with StateStore.create(self.root) as store:
            store.write_control_audited("settings_public", "schedule", 0, b"06:00",
                                        self.event(outcome="committed"))
            with patch.object(store, "append_diagnostic", side_effect=OSError("disk full")):
                warning = record_diagnostic(store, self.build("health_detail", "health-1", "result", {"outcome": "failed"}))
            self.assertEqual(warning, "diagnostic_not_persisted")
            with patch.object(store, "append_diagnostic", side_effect=sqlite3.OperationalError("disk full")):
                warning = record_diagnostic(store, self.build("health_detail", "health-2", "result", {"outcome": "failed"}))
            self.assertEqual(warning, "diagnostic_not_persisted")
            self.assertEqual(store.read_control("settings_public", "schedule"), (1, b"06:00"))

    def test_diagnostic_rotation_is_bounded_and_required_events_survive(self):
        with StateStore.create(self.root) as store:
            store.write_control_audited("settings_public", "schedule", 0, b"06:00",
                                        self.event(outcome="committed"))
            for index in range(6):
                store.append_diagnostic(self.build("health_detail", f"health-{index}", "result", {"outcome": "checked"}),
                                        max_records=2, max_bytes=2048)
            usage = store.operation_usage()
            self.assertEqual(usage["diagnostic_records"], 2)
            self.assertGreater(usage["required_records"], 0)
            self.assertGreater(usage["database_bytes"], 0)
            self.assertEqual(len(store.operation_events("settings-1")), 1)

    def test_legacy_jsonl_history_is_unchanged_and_never_treated_as_required_success(self):
        legacy_dir = Path(self.tmp.name) / "legacy"
        legacy_dir.mkdir()
        append_record(legacy_dir, "audit.jsonl", {"run_id": "settings-1", "action": "settings_update", "outcome": "handler_returned"})
        before = (legacy_dir / "audit.jsonl").read_bytes()
        with StateStore.create(self.root) as store:
            history = read_operation_history(legacy_dir, store, "settings-1")
        self.assertEqual(history["current_required"], [])
        self.assertEqual(history["legacy_references"][0]["classification"], "historical_unverified")
        self.assertNotIn("handler_returned", str(history))
        self.assertEqual((legacy_dir / "audit.jsonl").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

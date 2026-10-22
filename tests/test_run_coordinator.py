"""TP05: schedule resolution and run admission use retained state, not process TZ."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import os
import subprocess
import signal
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.run_coordinator import capture_zone_rules, resolve_wall, frozen_window, enumerate_nominal, scan_due, ensure_schedule_binding
from src.run_coordinator import acquire_run_lock, RunBusy, RunCoordinator, _worker, _live_claim_matches
from src.settings import SettingsService
from src.state_store import StateStore
from src.operations import OperationEvent


SETTINGS = {"max_episodes_retained": 14, "cron_schedule": "30 1 * * *",
            "timezone_id": "America/New_York", "chat_provider": "gemini",
            "chat_model": "gemini-3.6-flash"}


def run_intent(run_id, trigger="manual"):
    return OperationEvent.build("run", run_id, "intent",
        {"run_id": run_id, "trigger": trigger}, component="scheduler")


class CalendarResolutionTests(unittest.TestCase):
    def test_gap_uses_first_valid_boundary_not_gap_duration(self):
        rules = capture_zone_rules("America/New_York")
        result = resolve_wall(datetime(2024, 3, 10, 2, 30), rules)
        self.assertEqual(result.disposition, "gap")
        self.assertEqual(result.instant, datetime(2024, 3, 10, 7, tzinfo=timezone.utc))

    def test_fold_has_one_identity_at_earliest_utc_instant(self):
        rules = capture_zone_rules("America/New_York")
        result = resolve_wall(datetime(2024, 11, 3, 1, 30), rules)
        self.assertEqual(result.disposition, "fold_first")
        self.assertEqual(result.instant, datetime(2024, 11, 3, 5, 30, tzinfo=timezone.utc))

    def test_seven_named_dates_do_not_subtract_168_hours(self):
        rules = capture_zone_rules("America/New_York")
        started = datetime(2024, 3, 11, 12, tzinfo=timezone.utc)
        window = frozen_window(started, rules)
        self.assertEqual(window.local_date.isoformat(), "2024-03-11")
        self.assertEqual(window.lower_date.isoformat(), "2024-03-05")
        self.assertEqual(window.lower_utc, datetime(2024, 3, 5, 5, tzinfo=timezone.utc))
        self.assertEqual(window.upper_utc, started)

    def test_rule_bytes_are_bound_and_reopenable(self):
        rules = capture_zone_rules("Asia/Singapore")
        self.assertGreater(len(rules.data), 100)
        self.assertEqual(len(rules.sha256), 64)
        self.assertEqual(resolve_wall(datetime(2026, 10, 22, 7), rules).instant,
                         datetime(2026, 10, 21, 23, tzinfo=timezone.utc))

    def test_nominal_cron_enumeration_does_not_use_zone_wakeup_policy(self):
        slots = enumerate_nominal("30 1 * * *", datetime(2024, 11, 3, 0),
                                  datetime(2024, 11, 4, 0))
        self.assertEqual(slots, [datetime(2024, 11, 3, 1, 30)])


class DueLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        SettingsService(self.root).save(SETTINGS, expected_revision=0, initialization="fresh")

    def test_one_fold_wall_slot_survives_restart(self):
        with StateStore.open(self.root / "state") as store:
            self.assertTrue(store.record_schedule_slot(1, "2026-11-01T01:30:00",
                             "2026-11-01T05:30:00+00:00", "fold_first", "due"))
            self.assertFalse(store.record_schedule_slot(1, "2026-11-01T01:30:00",
                              "2026-11-01T06:30:00+00:00", "fold_first", "due"))
        with StateStore.open(self.root / "state") as store:
            self.assertEqual(store.schedule_slots()[0]["resolved_utc"],
                             "2026-11-01T05:30:00+00:00")
            self.assertEqual(len(store.schedule_slots()), 1)

    def test_stale_schedule_cannot_record_a_future_due_slot(self):
        SettingsService(self.root).save({**SETTINGS, "timezone_id": "Asia/Singapore"},
                                        expected_revision=1)
        with StateStore.open(self.root / "state") as store:
            with self.assertRaises(ValueError):
                store.record_schedule_slot(1, "2026-10-23T01:30:00",
                                           "2026-10-23T05:30:00+00:00", "on_time", "due")

    def test_handled_miss_cannot_be_reclaimed_after_restart(self):
        nominal = "2026-11-01T01:30:00"
        resolved = "2026-11-01T05:30:00+00:00"
        with StateStore.open(self.root / "state") as store:
            store.record_schedule_slot(1, nominal, resolved, "fold_first", "missed")
            self.assertTrue(store.set_schedule_slot_status(1, nominal, "missed", "handled",
                                                           run_id="run-catchup"))
        with StateStore.open(self.root / "state") as store:
            self.assertEqual(store.schedule_slots(status="missed"), [])
            self.assertFalse(store.set_schedule_slot_status(1, nominal, "missed", "handled",
                                                            run_id="run-again"))
            self.assertEqual(store.schedule_slots()[0]["run_id"], "run-catchup")

    def test_scan_records_confirmed_elapsed_slots_and_advances_cursor(self):
        service = SettingsService(self.root)
        service.save({**SETTINGS, "cron_schedule": "* * * * *"}, expected_revision=1)
        with StateStore.open(self.root / "state") as store:
            original = store.schedule_revisions()[-1]["cursor_utc"]
            future = datetime.fromisoformat(original) + timedelta(minutes=3)
            scan_due(store, future)
            rows = [r for r in store.schedule_slots(status="missed")
                    if r["schedule_revision"] == 2]
            self.assertGreaterEqual(len(rows), 2)
            self.assertEqual(store.schedule_revisions()[-1]["cursor_utc"], future.isoformat())
        with StateStore.open(self.root / "state") as store:
            scan_due(store, future)
            self.assertEqual(len([r for r in store.schedule_slots(status="missed")
                                  if r["schedule_revision"] == 2]), len(rows))

    def test_active_worker_skips_slot_without_creating_catch_up(self):
        SettingsService(self.root).save({**SETTINGS, "cron_schedule": "* * * * *"},
                                        expected_revision=1)
        with StateStore.open(self.root / "state") as store:
            original = datetime.fromisoformat(store.schedule_revisions()[-1]["cursor_utc"])
            added = scan_due(store, original + timedelta(minutes=2), active_worker=True)
            self.assertTrue(added)
            self.assertTrue(all(row["status"] == "skipped_active" for row in added))
            self.assertEqual(store.schedule_slots(status="missed"), [])
            self.assertEqual(len(store.operation_events(store._db.execute(
                "SELECT operation_id FROM operation_events WHERE kind = 'run' "
                "AND component = 'scheduler' ORDER BY rowid DESC LIMIT 1").fetchone()[0])), 1)

    def test_restart_recovers_unclaimed_due_as_confirmed_miss(self):
        SettingsService(self.root).save({**SETTINGS, "cron_schedule": "* * * * *"},
                                        expected_revision=1)
        with StateStore.open(self.root / "state") as store:
            cursor = datetime.fromisoformat(store.schedule_revisions()[-1]["cursor_utc"])
            resolved = (cursor + timedelta(minutes=2)).replace(second=0, microsecond=0)
            nominal = resolved.astimezone(capture_zone_rules("America/New_York").zone())
            store.record_schedule_slot(2, nominal.replace(tzinfo=None).isoformat(),
                                       resolved.isoformat(), "on_time", "due")
        coordinator = RunCoordinator(self.root)
        with patch.object(coordinator, "start_catch_up",
                          return_value={"status": "started"}) as catch_up:
            self.assertEqual(coordinator.tick(live_tick=False,
                now_utc=resolved + timedelta(minutes=2))["status"], "started")
        self.assertTrue(any(item["nominal_local"] == nominal.replace(tzinfo=None).isoformat()
                            for item in catch_up.call_args.args[0]))
        with StateStore.open(self.root / "state") as store:
            self.assertEqual([row for row in store.schedule_slots(status="due")
                              if row["nominal_local"] == nominal.replace(tzinfo=None).isoformat()], [])

    def test_old_due_before_zone_save_remains_confirmed_miss(self):
        with StateStore.open(self.root / "state") as store:
            effective = datetime.fromisoformat(store.schedule_revisions()[-1]["effective_from_utc"])
            resolved = effective + timedelta(microseconds=1)
            nominal = resolved.astimezone(capture_zone_rules("America/New_York").zone())
            store.record_schedule_slot(1, nominal.replace(tzinfo=None).isoformat(),
                                       resolved.isoformat(), "on_time", "due")
        SettingsService(self.root).save({**SETTINGS, "timezone_id": "Asia/Singapore"},
                                        expected_revision=1)
        coordinator = RunCoordinator(self.root)
        with patch.object(coordinator, "start_catch_up",
                          return_value={"status": "started"}) as catch_up:
            coordinator.tick(live_tick=False)
        self.assertTrue(any(item["schedule_revision"] == 1
                            for item in catch_up.call_args.args[0]))

    def test_migrated_tp23_settings_start_future_schedule_without_invented_history(self):
        with StateStore.open(self.root / "state") as store:
            store._db.execute("DROP TABLE schedule_slots")
            store._db.execute("DROP TABLE run_claims")
            store._db.execute("DROP TABLE schedule_revisions")
            store._db.execute("PRAGMA user_version = 2")
            store._db.commit()
        self.assertTrue(ensure_schedule_binding(self.root))
        with StateStore.open(self.root / "state") as store:
            schedules = store.schedule_revisions()
            self.assertEqual(len(schedules), 1)
            self.assertEqual(schedules[0]["revision"], 1)
            self.assertFalse(schedules[0]["history_known"])
            self.assertEqual(store.schedule_slots(), [])


class ProcessOwnershipTests(unittest.TestCase):
    def test_live_claim_requires_boot_start_and_worker_group_identity(self):
        claim = {"boot_id": "boot", "process_id": 42, "process_start": "start"}
        with patch("src.run_coordinator._boot_id", return_value="boot"), patch(
                "src.run_coordinator._process_start", return_value="start"), patch(
                "src.run_coordinator.os.getpgid", return_value=42):
            self.assertTrue(_live_claim_matches(claim))
        with patch("src.run_coordinator._boot_id", return_value="other"), patch(
                "src.run_coordinator._process_start", return_value="start"), patch(
                "src.run_coordinator.os.getpgid", return_value=42):
            self.assertFalse(_live_claim_matches(claim))

    def test_scheduled_audit_binds_exact_rule_digest(self):
        event = OperationEvent.build(
            "run", "run-scheduled", "intent", {"run_id": "run-scheduled", "trigger": "scheduled"},
            component="scheduler", context={
                "nominal_slot": "2026-11-01T01:30:00", "schedule_revision": 1,
                "resolved_instant": "2026-11-01T05:30:00+00:00",
                "timezone": "America/New_York", "rule_revision": "a" * 64,
                "actual_started": None, "actual_finished": None,
                "disposition": "fold_first"})
        self.assertEqual(event.context["rule_revision"], "a" * 64)

    def test_inherited_descriptor_keeps_lock_after_parent_closes(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            lease = acquire_run_lock(state)
            reader, writer = os.pipe()
            try:
                process = subprocess.Popen(
                    [sys.executable, "-c", "import os,sys; os.read(int(sys.argv[1]),1)",
                     str(reader)], pass_fds=(lease.fd, reader), start_new_session=True)
                os.close(reader)
                reader = -1
                lease.close()
                with self.assertRaises(RunBusy):
                    acquire_run_lock(state)
                os.write(writer, b"x")
                self.assertEqual(process.wait(timeout=5), 0)
                retry = acquire_run_lock(state)
                retry.close()
            finally:
                if reader >= 0:
                    os.close(reader)
                os.close(writer)

    def test_manual_claim_is_durable_and_active_context_stays_frozen(self):
        with tempfile.TemporaryDirectory() as root:
            service = SettingsService(root)
            service.save(SETTINGS, expected_revision=0, initialization="fresh")
            coordinator = RunCoordinator(root, worker_command=lambda *_: [
                sys.executable, "-c", "import time; time.sleep(30)"])
            with patch("src.run_coordinator._boot_id", return_value="test-boot"), patch(
                    "src.run_coordinator._process_start", return_value="test-start"):
                result = coordinator.start_manual()
            self.assertEqual(result["status"], "started")
            try:
                self.assertEqual(coordinator.start_manual()["status"], "busy")
                service.save({**SETTINGS, "timezone_id": "Asia/Singapore"},
                             expected_revision=1)
                with StateStore.open(Path(root) / "state") as store:
                    active = store.active_run()
                    self.assertEqual(active["context"]["settings_revision"], 1)
                    self.assertEqual(active["context"]["timezone_id"], "America/New_York")
                    self.assertEqual(len(store.operation_events(result["run_id"])), 1)
            finally:
                os.killpg(result["process_id"], signal.SIGKILL)
                coordinator.wait_owned(result["run_id"], timeout=5)

    def test_uncertain_previous_worker_blocks_restart_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            SettingsService(root).save({**SETTINGS, "cron_schedule": "0 0 1 1 *"},
                                       expected_revision=0, initialization="fresh")
            with StateStore.open(Path(root) / "state") as store:
                store.claim_run("run-crashed", "old-nonce", "old-boot", 123,
                                "old-start", {"settings_revision": 1},
                                intent_event=run_intent("run-crashed"))
            coordinator = RunCoordinator(root)
            with patch.object(coordinator, "start_scheduled") as scheduled, patch.object(
                    coordinator, "start_catch_up") as recovery:
                self.assertEqual(coordinator.tick()["status"], "blocked")
                scheduled.assert_not_called()
                recovery.assert_not_called()
            with StateStore.open(Path(root) / "state") as store:
                self.assertEqual(store.active_run()["run_id"], "run-crashed")

    def test_settings_save_does_not_immediately_launch_generation(self):
        with tempfile.TemporaryDirectory() as root:
            service = SettingsService(root)
            service.save({**SETTINGS, "cron_schedule": "0 0 1 1 *"},
                         expected_revision=0, initialization="fresh")
            service.save({**SETTINGS, "cron_schedule": "0 0 1 1 *",
                          "timezone_id": "Asia/Singapore"}, expected_revision=1)
            coordinator = RunCoordinator(root)
            with patch.object(coordinator, "start_scheduled") as scheduled, patch.object(
                    coordinator, "start_catch_up") as recovery:
                self.assertEqual(coordinator.tick()["status"], "ready")
                scheduled.assert_not_called()
                recovery.assert_not_called()

    def test_cancel_without_owned_worker_does_not_clear_retained_claim(self):
        with tempfile.TemporaryDirectory() as root:
            SettingsService(root).save(SETTINGS, expected_revision=0, initialization="fresh")
            with StateStore.open(Path(root) / "state") as store:
                store.claim_run("run-unknown", "nonce", "boot", 123, "start", {},
                                intent_event=run_intent("run-unknown"))
            result = RunCoordinator(root).cancel_owned()
            self.assertEqual(result["status"], "blocked")
            with StateStore.open(Path(root) / "state") as store:
                self.assertEqual(store.active_run()["run_id"], "run-unknown")

    def test_required_intent_failure_rolls_back_claim_and_prevents_worker_start(self):
        with tempfile.TemporaryDirectory() as root:
            SettingsService(root).save(SETTINGS, expected_revision=0, initialization="fresh")
            coordinator = RunCoordinator(root, worker_command=lambda *_: [
                sys.executable, "-c", "import time; time.sleep(30)"])
            with patch.object(StateStore, "_insert_required_event", side_effect=OSError("audit unavailable")):
                result = coordinator.start_manual()
            self.assertEqual(result["status"], "blocked")
            with StateStore.open(Path(root) / "state") as store:
                self.assertIsNone(store.active_run())
                self.assertIsNone(store.latest_run())

    def test_crashed_claim_blocks_new_generation_and_status_is_truthful(self):
        with tempfile.TemporaryDirectory() as root:
            SettingsService(root).save(SETTINGS, expected_revision=0, initialization="fresh")
            with StateStore.open(Path(root) / "state") as store:
                store.claim_run("run-before-crash", "nonce", "boot", 123, "start", {},
                                intent_event=run_intent("run-before-crash"))
            coordinator = RunCoordinator(root)
            self.assertEqual(coordinator.status()["status"], "blocked")
            self.assertEqual(coordinator.start_manual()["status"], "blocked")
            with StateStore.open(Path(root) / "state") as store:
                self.assertEqual(store.active_run()["run_id"], "run-before-crash")

    def test_one_confirmed_old_zone_miss_claims_current_zone_once(self):
        with tempfile.TemporaryDirectory() as root:
            service = SettingsService(root)
            service.save(SETTINGS, expected_revision=0, initialization="fresh")
            original = datetime.now(timezone.utc) + timedelta(days=2)
            nominal = original.astimezone(capture_zone_rules("America/New_York").zone())
            local = nominal.replace(tzinfo=None, second=0, microsecond=0).isoformat()
            instant = nominal.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()
            with StateStore.open(Path(root) / "state") as store:
                store.record_schedule_slot(1, local, instant, "on_time", "missed")
                old_miss = store.schedule_slots(status="missed")
            service.save({**SETTINGS, "timezone_id": "Asia/Singapore"}, expected_revision=1)
            coordinator = RunCoordinator(root, worker_command=lambda *_: [
                sys.executable, "-c", "import time; time.sleep(30)"])
            with patch("src.run_coordinator._boot_id", return_value="test-boot"), patch(
                    "src.run_coordinator._process_start", return_value="test-start"):
                result = coordinator.start_catch_up(old_miss)
            self.assertEqual(result["status"], "started")
            try:
                self.assertEqual(result["context"]["timezone_id"], "Asia/Singapore")
                self.assertEqual(result["context"]["settings_revision"], 2)
                self.assertEqual(result["context"]["catch_up_slots"], [[1, local]])
                with StateStore.open(Path(root) / "state") as store:
                    self.assertEqual(store.schedule_slots(status="missed"), [])
                    self.assertEqual(store.schedule_slots(status="handled")[0]["run_id"],
                                     result["run_id"])
                self.assertEqual(coordinator.start_catch_up(old_miss)["status"], "busy")
            finally:
                os.killpg(result["process_id"], signal.SIGKILL)
                coordinator.wait_owned(result["run_id"], timeout=5)
            self.assertEqual(RunCoordinator(root).start_catch_up(old_miss)["status"], "blocked")

    def test_worker_requires_claim_and_records_terminal_result(self):
        with tempfile.TemporaryDirectory() as root:
            SettingsService(root).save(SETTINGS, expected_revision=0, initialization="fresh")
            lease = acquire_run_lock(Path(root) / "state")
            read, write = os.pipe()
            try:
                os.write(write, b"1")
                with StateStore.open(Path(root) / "state") as store:
                    store.claim_run("run-worker", "nonce", "boot", os.getpid(), "start",
                                    {"trigger": "manual", "slot": None},
                                    intent_event=run_intent("run-worker"))

                async def finish(run_record):
                    run_record.data["status"] = "completed"

                with patch("src.run_coordinator._boot_id", return_value="boot"), patch(
                        "src.run_coordinator._process_start", return_value="start"), patch(
                        "src.main.run_daily_pipeline", new=AsyncMock(side_effect=finish)) as pipeline, patch.dict(
                        os.environ, {}, clear=False):
                    self.assertEqual(_worker("run-worker", "nonce", lease.fd, read, root), 0)
                    pipeline.assert_awaited_once()
                read = -1
                with StateStore.open(Path(root) / "state") as store:
                    self.assertIsNone(store.active_run())
                    self.assertEqual(store.latest_run()["status"], "completed")
                    self.assertEqual([r["phase"] for r in store.operation_events("run-worker")],
                                     ["intent", "result"])
            finally:
                if read >= 0:
                    os.close(read)
                os.close(write)
                lease.close()

    def test_failed_required_result_keeps_active_claim_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as root:
            SettingsService(root).save(SETTINGS, expected_revision=0, initialization="fresh")
            lease = acquire_run_lock(Path(root) / "state")
            read, write = os.pipe()
            try:
                os.write(write, b"1")
                with StateStore.open(Path(root) / "state") as store:
                    store.claim_run("run-audit-failure", "nonce", "boot", os.getpid(), "start",
                                    {"trigger": "manual", "slot": None},
                                    intent_event=run_intent("run-audit-failure"))

                async def finish(run_record):
                    run_record.data["status"] = "completed"

                with patch("src.run_coordinator._boot_id", return_value="boot"), patch(
                        "src.run_coordinator._process_start", return_value="start"), patch(
                        "src.main.run_daily_pipeline", new=AsyncMock(side_effect=finish)), patch.object(
                        StateStore, "_insert_required_event", side_effect=OSError("audit unavailable")), patch.dict(
                        os.environ, {}, clear=False):
                    with self.assertRaises(OSError):
                        _worker("run-audit-failure", "nonce", lease.fd, read, root)
                read = -1
                with StateStore.open(Path(root) / "state") as store:
                    self.assertEqual(store.active_run()["run_id"], "run-audit-failure")
                    self.assertEqual([r["phase"] for r in store.operation_events("run-audit-failure")],
                                     ["intent"])
            finally:
                if read >= 0:
                    os.close(read)
                os.close(write)
                lease.close()


if __name__ == "__main__":
    unittest.main()

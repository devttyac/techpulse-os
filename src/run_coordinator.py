"""Version-bound calendar resolution for durable TechPulse run coordination."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import asyncio
import hashlib
import fcntl
from io import BytesIO
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger


_ZONE_ID = re.compile(r"[A-Za-z_+-]+(?:/[A-Za-z_+0-9-]+)*\Z")


class RunBusy(RuntimeError):
    """Another cooperating worker still owns the local generation lease."""


class RunOwnershipUnknown(RuntimeError):
    """Retained ownership cannot be proved stopped or safe to replace."""


class RunLease:
    def __init__(self, fd):
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def acquire_run_lock(state_root):
    """Use an inheritable-at-spawn descriptor on a stable local lock inode."""
    state_root = Path(state_root)
    if not state_root.is_dir() or state_root.is_symlink():
        raise RunOwnershipUnknown("retained run state is unavailable")
    path = state_root / "run.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        identity = os.fstat(fd)
        actual = path.stat(follow_symlinks=False)
        if not os.path.isfile(path) or (identity.st_dev, identity.st_ino) != (actual.st_dev, actual.st_ino):
            raise RunOwnershipUnknown("run lock inode changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunBusy("generation worker remains active") from exc
        return RunLease(fd)
    except BaseException:
        if "fd" in locals():
            os.close(fd)
        raise


@dataclass(frozen=True)
class ZoneRules:
    zone_id: str
    data: bytes
    sha256: str
    provider: str

    def zone(self):
        if hashlib.sha256(self.data).hexdigest() != self.sha256:
            raise ValueError("timezone rule bytes changed")
        return ZoneInfo.from_file(BytesIO(self.data), key=self.zone_id)


@dataclass(frozen=True)
class WallResolution:
    instant: datetime
    disposition: str


@dataclass(frozen=True)
class FrozenWindow:
    local_date: date
    lower_date: date
    lower_utc: datetime
    upper_utc: datetime
    timezone_id: str
    rule_sha256: str


def capture_zone_rules(zone_id):
    """Retain exact TZif bytes; an installed zone name alone is not a binding."""
    if not isinstance(zone_id, str) or not _ZONE_ID.fullmatch(zone_id):
        raise ValueError("invalid IANA timezone identifier")
    try:
        ZoneInfo(zone_id)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("IANA timezone unavailable") from exc
    for root in TZPATH:
        path = Path(root) / zone_id
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if data.startswith(b"TZif"):
                return ZoneRules(zone_id, data, hashlib.sha256(data).hexdigest(), str(root))
    try:
        from importlib.resources import files
        node = files("tzdata.zoneinfo")
        for part in zone_id.split("/"):
            node = node.joinpath(part)
        data = node.read_bytes()
    except (ImportError, FileNotFoundError, ModuleNotFoundError) as exc:
        raise ValueError("IANA timezone rule bytes unavailable") from exc
    if not data.startswith(b"TZif"):
        raise ValueError("IANA timezone rule bytes are invalid")
    return ZoneRules(zone_id, data, hashlib.sha256(data).hexdigest(), "tzdata")


def resolve_wall(nominal, rules):
    """Map a nominal wall slot to one instant using retained TZif rules."""
    if not isinstance(nominal, datetime) or nominal.tzinfo is not None:
        raise ValueError("nominal local time must be naive")
    zone = rules.zone()
    candidates = []
    for fold in (0, 1):
        utc = nominal.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == nominal and utc not in candidates:
            candidates.append(utc)
    if candidates:
        return WallResolution(min(candidates), "fold_first" if len(candidates) == 2 else "on_time")
    # A gap's two zone-offset projections bracket the transition in UTC.
    projections = [nominal.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                   for fold in (0, 1)]
    low, high = sorted(projections)
    if not (low.astimezone(zone).replace(tzinfo=None) < nominal
            and high.astimezone(zone).replace(tzinfo=None) > nominal):
        raise ValueError("timezone rule transition cannot be resolved")
    left = int(low.timestamp())
    right = int(high.timestamp())
    while left + 1 < right:
        middle = (left + right) // 2
        local = datetime.fromtimestamp(middle, timezone.utc).astimezone(zone).replace(tzinfo=None)
        if local >= nominal:
            right = middle
        else:
            left = middle
    instant = datetime.fromtimestamp(right, timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) < nominal:
        raise ValueError("timezone gap boundary is inconsistent")
    return WallResolution(instant, "gap")


def frozen_window(started_utc, rules):
    if started_utc.tzinfo is None:
        raise ValueError("actual run start requires an offset")
    started = started_utc.astimezone(timezone.utc)
    local_date = started.astimezone(rules.zone()).date()
    lower_date = local_date - timedelta(days=6)
    lower = resolve_wall(datetime.combine(lower_date, datetime.min.time()), rules).instant
    return FrozenWindow(local_date, lower_date, lower, started, rules.zone_id, rules.sha256)


def enumerate_nominal(cron, start_local, end_local, *, max_slots=100000):
    """Enumerate local wall labels with UTC as a neutral cron grammar axis."""
    if (start_local.tzinfo is not None or end_local.tzinfo is not None
            or start_local >= end_local or type(max_slots) is not int or max_slots < 1):
        raise ValueError("invalid nominal enumeration interval")
    fields = cron.split() if isinstance(cron, str) else []
    if len(fields) != 5:
        raise ValueError("five-field cron required")
    minute, hour, day, month, day_of_week = fields
    trigger = CronTrigger(minute=minute, hour=hour, day=day, month=month,
                          day_of_week=day_of_week, timezone=timezone.utc)
    end = end_local.replace(tzinfo=timezone.utc)
    cursor = trigger.get_next_fire_time(None, start_local.replace(tzinfo=timezone.utc))
    result = []
    while cursor is not None and cursor < end:
        if len(result) >= max_slots:
            raise ValueError("nominal schedule exceeds safe enumeration bound")
        result.append(cursor.replace(tzinfo=None))
        cursor = trigger.get_next_fire_time(cursor, cursor)
    return result


def scan_due(store, now_utc, *, live_tick=False, active_worker=False):
    """Materialize provable elapsed slots before moving each durable cursor."""
    if now_utc.tzinfo is None:
        raise ValueError("schedule scan requires an actual UTC instant")
    now = now_utc.astimezone(timezone.utc)
    revisions = store.schedule_revisions()
    added = []
    for index, item in enumerate(revisions):
        effective = datetime.fromisoformat(item["effective_from_utc"])
        cursor = datetime.fromisoformat(item["cursor_utc"])
        next_effective = (datetime.fromisoformat(revisions[index + 1]["effective_from_utc"])
                          if index + 1 < len(revisions) else None)
        end = min(now, next_effective) if next_effective else now
        if end <= cursor:
            continue
        rules = ZoneRules(item["timezone_id"], item["rule_bytes"],
                          item["rule_sha256"], item["rule_provider"])
        zone = rules.zone()
        start_local = (cursor.astimezone(zone).replace(tzinfo=None) - timedelta(days=2))
        end_local = (end.astimezone(zone).replace(tzinfo=None) + timedelta(days=2))
        for nominal in enumerate_nominal(item["cron"], start_local, end_local):
            result = resolve_wall(nominal, rules)
            if (not (effective <= result.instant and cursor < result.instant <= end)
                    or (next_effective is not None and result.instant >= next_effective)):
                continue
            status = ("skipped_active" if active_worker else
                      "due" if live_tick and index == len(revisions) - 1
                      and (now - result.instant).total_seconds() <= 2 else "missed")
            slot = {"schedule_revision": item["revision"],
                    "nominal_local": nominal.isoformat(),
                    "resolved_utc": result.instant.isoformat(),
                    "disposition": result.disposition, "status": status,
                    "timezone_id": item["timezone_id"],
                    "rule_sha256": item["rule_sha256"]}
            event = None
            if status == "skipped_active":
                from src.operations import OperationEvent
                skip_id = "run-skip-" + uuid.uuid4().hex
                event = OperationEvent.build("run", skip_id, "result",
                    {"run_id": skip_id, "trigger": "scheduled", "outcome": "skipped"},
                    component="scheduler", context={
                        "nominal_slot": slot["nominal_local"],
                        "schedule_revision": item["revision"],
                        "resolved_instant": slot["resolved_utc"],
                        "timezone": item["timezone_id"],
                        "rule_revision": item["rule_sha256"],
                        "actual_started": None, "actual_finished": None,
                        "disposition": "skipped"})
            if store.record_schedule_slot(item["revision"], slot["nominal_local"],
                                          slot["resolved_utc"], result.disposition, status,
                                          event=event):
                added.append(slot)
        if not store.advance_schedule_cursor(item["revision"], item["cursor_utc"], end.isoformat()):
            raise RunOwnershipUnknown("schedule cursor changed during scan")
    return added


def ensure_schedule_binding(storage_root):
    """Adopt an existing verified TP23 Settings row from this instant forward."""
    from src.operations import OperationEvent
    from src.settings import SettingsService
    from src.state_store import StateStore, StoreIntegrityError
    settings = SettingsService(storage_root).read()
    with StateStore.open(Path(storage_root) / "state") as store:
        revisions = store.schedule_revisions()
        if revisions:
            current = revisions[-1]
            if (current["revision"] != settings["revision"]
                    or current["cron"] != settings["config"]["cron_schedule"]
                    or current["timezone_id"] != settings["config"]["timezone_id"]):
                raise StoreIntegrityError("Settings and retained schedule disagree")
            return False
        raw = store.read_settings_control_verified("settings_snapshot")
        if raw is None:
            raise StoreIntegrityError("Settings revision unavailable")
        rules = capture_zone_rules(settings["config"]["timezone_id"])
        event_id = f"schedule-bootstrap-{settings['revision']}"
        event = OperationEvent.build("run", event_id, "result",
            {"run_id": event_id, "trigger": "recovery", "outcome": "checked"},
            component="scheduler")
        return store.bootstrap_schedule_binding(settings["revision"],
            hashlib.sha256(raw[1]).hexdigest(),
            {"cron": settings["config"]["cron_schedule"],
             "timezone_id": rules.zone_id, "rule_bytes": rules.data,
             "rule_sha256": rules.sha256, "rule_provider": rules.provider}, event)


def _boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise RunOwnershipUnknown("Linux boot identity unavailable") from exc
    if not re.fullmatch(r"[0-9a-f-]{36}", value):
        raise RunOwnershipUnknown("Linux boot identity invalid")
    return value


def _process_start(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError as exc:
        raise RunOwnershipUnknown("worker process identity unavailable") from exc
    tail = stat.rsplit(")", 1)[1].split()
    if len(tail) < 20 or not tail[19].isdigit():
        raise RunOwnershipUnknown("worker process identity invalid")
    return tail[19]


def _live_claim_matches(active):
    """Do not equate an occupied lock or reused PID with the saved worker."""
    try:
        return (active["boot_id"] == _boot_id()
                and active["process_start"] == _process_start(active["process_id"])
                and os.getpgid(active["process_id"]) == active["process_id"])
    except (RunOwnershipUnknown, ProcessLookupError, PermissionError, OSError):
        return False


class RunCoordinator:
    """Start a separate, lock-inheriting worker only after a durable claim."""

    def __init__(self, storage_root, *, worker_command=None):
        self.root = Path(storage_root).resolve()
        self.state_root = self.root / "state"
        self._worker_command = worker_command or self._default_command
        self._owned = {}

    def _default_command(self, run_id, nonce, lock_fd, gate_fd):
        return [sys.executable, "-m", "src.run_coordinator", "--worker", run_id,
                nonce, str(lock_fd), str(gate_fd), str(self.root)]

    def start_manual(self):
        return self._start("manual")

    def start_scheduled(self, slot):
        return self._start("scheduled", slot=slot)

    def start_catch_up(self, slots):
        return self._start("recovery", catch_up=slots)

    def tick(self, *, live_tick=False, now_utc=None):
        """Refresh committed Settings and process only durable, provable slots."""
        from src.operations import OperationEvent
        from src.state_store import StateStore, StoreIntegrityError
        from src.settings import SettingsIntegrityError
        try:
            ensure_schedule_binding(self.root)
            now = now_utc or datetime.now(timezone.utc)
            lease = None
            try:
                lease = acquire_run_lock(self.state_root)
                worker_active = False
            except RunBusy:
                worker_active = True
            try:
                with StateStore.open(self.state_root) as store:
                    active = store.active_run()
                    if ((worker_active and (active is None or not _live_claim_matches(active)))
                            or (not worker_active and active is not None)):
                        return {"status": "blocked", "reason": "uncertain_worker_or_publication"}
                    scan_due(store, now, live_tick=live_tick,
                             active_worker=worker_active)
                    revisions = {item["revision"]: item for item in store.schedule_revisions()}
                    due = []
                    for row in store.schedule_slots(status="due"):
                        resolved = datetime.fromisoformat(row["resolved_utc"])
                        if resolved > now:
                            continue
                        if row["schedule_revision"] != max(revisions):
                            later_effective = min(datetime.fromisoformat(
                                revision["effective_from_utc"]) for revision in revisions.values()
                                if revision["revision"] > row["schedule_revision"])
                            store.set_schedule_slot_status(row["schedule_revision"],
                                row["nominal_local"], "due",
                                "missed" if resolved < later_effective else "superseded")
                            continue
                        if worker_active:
                            revision = revisions[row["schedule_revision"]]
                            slot = {**row, "timezone_id": revision["timezone_id"],
                                    "rule_sha256": revision["rule_sha256"]}
                            skip_id = "run-skip-" + uuid.uuid4().hex
                            context = self._event_context(slot)
                            context["disposition"] = "skipped"
                            event = OperationEvent.build("run", skip_id, "result",
                                {"run_id": skip_id, "trigger": "scheduled", "outcome": "skipped"},
                                component="scheduler", context=context)
                            store.set_schedule_slot_status(row["schedule_revision"],
                                row["nominal_local"], "due", "skipped_active", event=event)
                        elif not live_tick or (now - resolved).total_seconds() > 2:
                            store.set_schedule_slot_status(row["schedule_revision"],
                                row["nominal_local"], "due", "missed")
                        else:
                            revision = revisions[row["schedule_revision"]]
                            due.append({**row, "timezone_id": revision["timezone_id"],
                                        "rule_sha256": revision["rule_sha256"]})
                    misses = store.schedule_slots(status="missed") if not worker_active else []
            finally:
                if lease is not None:
                    lease.close()
            if worker_active:
                return {"status": "busy", "reason": "active_worker_lock"}
            if due:
                slot = due[0]
                result = self.start_scheduled(slot)
                if result["status"] != "started":
                    status = "superseded" if result["status"] == "stale" else "skipped_active"
                    with StateStore.open(self.state_root) as store:
                        event = None
                        if status == "skipped_active":
                            skip_id = "run-skip-" + uuid.uuid4().hex
                            context = self._event_context(slot)
                            context["disposition"] = "skipped"
                            event = OperationEvent.build("run", skip_id, "result",
                                {"run_id": skip_id, "trigger": "scheduled", "outcome": "skipped"},
                                component="scheduler", context=context)
                        store.set_schedule_slot_status(
                            slot["schedule_revision"], slot["nominal_local"], "due",
                            status, event=event)
                return result
            if misses:
                return self.start_catch_up(misses)
            return {"status": "ready", "reason": "no_due_slot"}
        except (StoreIntegrityError, SettingsIntegrityError, ValueError, OSError,
                RunOwnershipUnknown) as exc:
            return {"status": "blocked", "reason": type(exc).__name__}

    def _start(self, trigger, *, slot=None, catch_up=None):
        from src.operations import OperationEvent
        from src.settings import SettingsService, SettingsIntegrityError
        from src.state_store import StateStore, StoreIntegrityError
        try:
            lease = acquire_run_lock(self.state_root)
        except RunBusy:
            return {"status": "busy", "reason": "active_worker_lock"}
        except (RunOwnershipUnknown, OSError) as exc:
            return {"status": "blocked", "reason": type(exc).__name__}
        try:
            settings = SettingsService(self.root).read()
            with StateStore.open(self.state_root) as store:
                if store.active_run() is not None:
                    # A free lock does not prove whether legacy publication
                    # committed before an interrupted worker stopped. TP06
                    # owns that separate reconciliation.
                    return {"status": "blocked", "reason": "unreconciled_previous_run"}
                revisions = store.schedule_revisions()
                if not revisions or revisions[-1]["revision"] != settings["revision"]:
                    return {"status": "blocked", "reason": "unbound_schedule_revision"}
                schedule = revisions[-1]
                if (schedule["cron"] != settings["config"]["cron_schedule"]
                        or schedule["timezone_id"] != settings["config"]["timezone_id"]):
                    return {"status": "blocked", "reason": "schedule_settings_mismatch"}
                rules = ZoneRules(schedule["timezone_id"], schedule["rule_bytes"],
                                  schedule["rule_sha256"], schedule["rule_provider"])
                window = frozen_window(datetime.now(timezone.utc), rules)
                if slot is not None:
                    if (slot["schedule_revision"] != schedule["revision"]
                            or slot["status"] != "due"):
                        return {"status": "stale", "reason": "schedule_revision_changed"}
                if catch_up is not None and (not catch_up or any(
                        item["status"] != "missed" for item in catch_up)):
                    return {"status": "blocked", "reason": "unconfirmed_miss_history"}
                context = {
                    "trigger": trigger, "settings_revision": settings["revision"],
                    "settings": settings["config"], "schedule_revision": schedule["revision"],
                    "timezone_id": rules.zone_id, "rule_sha256": rules.sha256,
                    "rule_provider": rules.provider,
                    "local_date": window.local_date.isoformat(),
                    "lower_date": window.lower_date.isoformat(),
                    "lower_utc": window.lower_utc.isoformat(),
                    "started_utc": window.upper_utc.isoformat(),
                    "slot": slot,
                    "catch_up_slots": [[item["schedule_revision"], item["nominal_local"]]
                                       for item in (catch_up or [])]}
                run_id = "run-" + uuid.uuid4().hex
                nonce = uuid.uuid4().hex
                gate_read, gate_write = os.pipe()
                process = None
                try:
                    command = self._worker_command(run_id, nonce, lease.fd, gate_read)
                    process = subprocess.Popen(command, pass_fds=(lease.fd, gate_read),
                                               start_new_session=True, close_fds=True,
                                               cwd=str(Path(__file__).resolve().parents[1]),
                                               env={**os.environ, "STORAGE_DIR": str(self.root)})
                    os.close(gate_read)
                    gate_read = -1
                    boot = _boot_id()
                    start = _process_start(process.pid)
                    event_context = self._event_context(slot) if slot is not None else None
                    intent = OperationEvent.build("run", run_id, "intent",
                        {"run_id": run_id, "trigger": trigger}, component="scheduler",
                        context=event_context)
                    slot_key = ((slot["schedule_revision"], slot["nominal_local"], "due")
                                if slot is not None else None)
                    if not store.claim_run(run_id, nonce, boot, process.pid, start, context,
                                           intent_event=intent, schedule_slot=slot_key,
                                           missed_slots=tuple(tuple(item) for item in
                                                              context["catch_up_slots"])):
                        os.killpg(process.pid, 9)
                        process.wait(timeout=5)
                        return {"status": "busy", "reason": "active_run_claim"}
                    os.write(gate_write, b"1")
                    self._owned[run_id] = process
                    return {"status": "started", "run_id": run_id,
                            "process_id": process.pid, "context": context}
                except BaseException:
                    if process is not None:
                        try:
                            os.killpg(process.pid, 9)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                    raise
                finally:
                    if gate_read >= 0:
                        os.close(gate_read)
                    os.close(gate_write)
        except (StoreIntegrityError, SettingsIntegrityError, ValueError, OSError,
                RunOwnershipUnknown) as exc:
            return {"status": "blocked", "reason": type(exc).__name__}
        finally:
            lease.close()

    @staticmethod
    def _event_context(slot):
        return {"nominal_slot": slot["nominal_local"],
                "schedule_revision": slot["schedule_revision"],
                "resolved_instant": slot["resolved_utc"],
                "timezone": slot["timezone_id"],
                "rule_revision": slot["rule_sha256"],
                "actual_started": None, "actual_finished": None,
                "disposition": slot["disposition"]}

    def wait_owned(self, run_id, timeout=None):
        process = self._owned.pop(run_id, None)
        return process.wait(timeout=timeout) if process else None

    def status(self):
        """Report retained run state without interpreting a stale UI task flag."""
        from src.state_store import StateStore, StoreIntegrityError
        try:
            with StateStore.open(self.state_root) as store:
                latest = store.latest_run()
                active = store.active_run()
            try:
                lease = acquire_run_lock(self.state_root)
            except RunBusy:
                lock_held = True
            else:
                lease.close()
                lock_held = False
            if active and lock_held and _live_claim_matches(active):
                return {"status": "running", "run_id": active["run_id"],
                        "settings_revision": active["context"].get("settings_revision")}
            if active or lock_held:
                return {"status": "blocked", "reason": "uncertain_worker_or_publication",
                        "run_id": active["run_id"] if active else None}
            return {"status": latest["status"] if latest else "idle",
                    "run_id": latest["run_id"] if latest else None}
        except (StoreIntegrityError, OSError, RunOwnershipUnknown):
            return {"status": "blocked", "reason": "retained_run_state_unavailable"}

    def cancel_owned(self):
        """Stop only this parent's worker group; never clear uncertain claims."""
        from src.state_store import StateStore, StoreIntegrityError
        try:
            with StateStore.open(self.state_root) as store:
                active = store.active_run()
        except StoreIntegrityError:
            return {"status": "blocked", "reason": "retained_run_state_unavailable"}
        if active is None:
            return {"status": "no_active_task"}
        process = self._owned.get(active["run_id"])
        if (process is None or process.pid != active["process_id"]
                or process.poll() is not None or not _live_claim_matches(active)):
            return {"status": "blocked", "reason": "worker_not_owned_by_this_server"}
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
        self._owned.pop(active["run_id"], None)
        try:
            lease = acquire_run_lock(self.state_root)
        except RunBusy:
            return {"status": "pending", "reason": "child_still_holds_run_lock"}
        else:
            lease.close()
        return {"status": "stopped_pending_reconciliation", "run_id": active["run_id"]}


def _worker(run_id, nonce, lock_fd, gate_fd, storage_root):
    """Wait for parent claim commit before importing the legacy pipeline."""
    from src.operations import OperationEvent
    from src.state_store import StateStore
    try:
        os.fstat(lock_fd)
        if os.read(gate_fd, 1) != b"1":
            return 2
    finally:
        os.close(gate_fd)
    with StateStore.open(Path(storage_root) / "state") as store:
        active = store.active_run()
        if (active is None or active["run_id"] != run_id or active["nonce"] != nonce
                or active["boot_id"] != _boot_id() or active["process_id"] != os.getpid()
                or active["process_start"] != _process_start(os.getpid())):
            return 3
        context = active["context"]
    os.environ["STORAGE_DIR"] = storage_root
    os.environ["TECHPULSE_RUN_LOCK_FD"] = str(lock_fd)
    from src.provenance import RunRecord
    from src.main import run_daily_pipeline
    run = RunRecord(context["trigger"])
    run.data["run_id"] = run_id
    run.data["frozen_run_context"] = context
    started = datetime.now(timezone.utc).isoformat()
    try:
        asyncio.run(run_daily_pipeline(run_record=run))
        status = "completed" if run.data["status"] not in ("failed", "cancelled") else "failed"
    except BaseException:
        status = "failed"
    finished = datetime.now(timezone.utc).isoformat()
    with StateStore.open(Path(storage_root) / "state") as store:
        slot = context.get("slot")
        event_context = RunCoordinator._event_context(slot) if slot else None
        if event_context is not None:
            event_context["actual_started"] = started
            event_context["actual_finished"] = finished
        event = OperationEvent.build("run", run_id, "result",
            {"run_id": run_id, "trigger": context["trigger"], "outcome": status},
            component="pipeline", context=event_context)
        if not store.finish_run(run_id, nonce, status, result_event=event):
            return 4
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    if len(sys.argv) != 7 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    raise SystemExit(_worker(sys.argv[2], sys.argv[3], int(sys.argv[4]),
                             int(sys.argv[5]), sys.argv[6]))

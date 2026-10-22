"""TP23 nonsecret Settings snapshots and disabled model admission.

The current scheduler, chat adapter, key custody and cleanup consumers have
separate owners. This module reports their pending integration explicitly.
"""

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from src.operations import OperationEvent
from src.state_store import StateStore, StoreIntegrityError


CONTROL_KEY = "settings_snapshot"
FIELDS = frozenset({"max_episodes_retained", "cron_schedule", "timezone_id",
                    "chat_provider", "chat_model"})
MODEL_BINDINGS = frozenset({"api_endpoint", "model_version", "price_revision",
                            "capability_revision", "grounding_revision",
                            "citation_revision", "pi_revision"})
CURRENT_MODEL_BINDINGS = {
    "api_endpoint": "v1beta/models/gemini-3.6-flash",
    "model_version": "3.6-flash-07-2026",
    "price_revision": None,
    "capability_revision": None,
    "grounding_revision": None,
    "citation_revision": None,
    "pi_revision": None,
}
MODEL_REGISTRY = ({
    "provider": "gemini",
    "model": "gemini-3.6-flash",
    "approval_status": "pending",
    "approval_ref": None,
    "bindings": dict(CURRENT_MODEL_BINDINGS),
    "evaluation_reference": "F05 Gemini Model Validation 2026-10-21",
},)


class SettingsIntegrityError(RuntimeError):
    """Retained Settings are missing, corrupt or require explicit migration."""


class SettingsValidationError(ValueError):
    def __init__(self, fields):
        self.fields = fields
        super().__init__("invalid Settings fields")


class SettingsConflict(ValueError):
    """The supplied revision is no longer current."""


def assess_model(entry, current_bindings):
    """Assess trusted registry evidence against only model-relevant inputs.

    The Settings API has no approval-writing route. TP44 must supply reviewed
    evidence and a separately controlled registry transition before enablement.
    """
    if (entry.get("provider") != "gemini" or entry.get("model") != "gemini-3.6-flash"
            or not isinstance(current_bindings, dict)
            or set(current_bindings) != MODEL_BINDINGS):
        return {"status": "disabled", "reason": "unsupported_registry_entry"}
    evidence = entry.get("bindings")
    if not isinstance(evidence, dict) or set(evidence) != MODEL_BINDINGS:
        return {"status": "disabled", "reason": "incomplete_evidence"}
    if entry.get("approval_status") != "approved" or not entry.get("approval_ref"):
        return {"status": "disabled", "reason": "approval_pending"}
    if any(not isinstance(evidence[key], str) or not evidence[key]
           for key in MODEL_BINDINGS):
        return {"status": "disabled", "reason": "incomplete_evidence"}
    if any(current_bindings[key] != evidence[key] for key in MODEL_BINDINGS):
        return {"status": "stale", "reason": "relevant_binding_changed"}
    return {"status": "approved", "reason": "current_reviewed_evidence"}


def validate_snapshot(value):
    """Return a canonical snapshot or identify every invalid submitted field."""
    if not isinstance(value, dict):
        raise SettingsValidationError({"config": "object_required"})
    errors = {}
    for name in FIELDS - value.keys():
        errors[name] = "required"
    for name in value.keys() - FIELDS:
        errors[name] = "unknown_field"
    retained = value.get("max_episodes_retained")
    if type(retained) is not int or retained < 1:
        errors["max_episodes_retained"] = "positive_integer_required"
    cron = value.get("cron_schedule")
    zone_id = value.get("timezone_id")
    zone = None
    if not isinstance(zone_id, str) or not zone_id or len(zone_id) > 128:
        errors["timezone_id"] = "iana_zone_required"
    else:
        try:
            zone = ZoneInfo(zone_id)
        except (ZoneInfoNotFoundError, ValueError):
            errors["timezone_id"] = "available_iana_zone_required"
    if not isinstance(cron, str) or len(cron) > 128 or len(cron.split()) != 5:
        errors["cron_schedule"] = "five_field_cron_required"
    elif zone is not None:
        try:
            minute, hour, day, month, day_of_week = cron.split()
            CronTrigger(minute=minute, hour=hour, day=day, month=month,
                        day_of_week=day_of_week, timezone=zone)
        except ValueError:
            errors["cron_schedule"] = "valid_cron_required"
    if value.get("chat_provider") != "gemini":
        errors["chat_provider"] = "approved_provider_required"
    if value.get("chat_model") != "gemini-3.6-flash":
        errors["chat_model"] = "named_initial_model_required"
    if errors:
        raise SettingsValidationError(errors)
    return {key: value[key] for key in sorted(FIELDS)}


def _decode(raw):
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "config"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError("unsupported Settings snapshot")
        return validate_snapshot(value["config"])
    except (TypeError, ValueError, SettingsValidationError) as exc:
        raise SettingsIntegrityError("retained Settings snapshot is unreadable") from exc


def _validate_legacy_config(path, zone_id):
    """Check historical control bytes before an explicit conversion."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise SettingsIntegrityError("legacy configuration is unreadable")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {"max_episodes_retained", "chapters_per_episode",
                   "gemini_model", "cron_schedule"}
        if not isinstance(data, dict) or not set(data) <= allowed:
            raise ValueError("legacy configuration shape")
        for name in ("max_episodes_retained", "chapters_per_episode"):
            if name in data and (type(data[name]) is not int or data[name] < 1):
                raise ValueError("legacy integer")
        if "gemini_model" in data and (not isinstance(data["gemini_model"], str)
                                       or not data["gemini_model"]):
            raise ValueError("legacy model")
        if "cron_schedule" in data:
            cron = data["cron_schedule"]
            if not isinstance(cron, str):
                raise ValueError("legacy cron")
            parts = cron.split()
            if len(parts) != 5:
                raise ValueError("legacy cron")
            CronTrigger(minute=parts[0], hour=parts[1], day=parts[2],
                        month=parts[3], day_of_week=parts[4], timezone=ZoneInfo(zone_id))
    except (OSError, UnicodeError, ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise SettingsIntegrityError("legacy configuration is invalid") from exc


class SettingsService:
    def __init__(self, storage_root):
        self.root = Path(storage_root)
        # TP02/TP28 publication, lifecycle and required audit share this root.
        self.store_root = self.root / "state"

    def _open(self):
        if not self.store_root.exists():
            if ((self.root / "config.json").exists() or any(
                    any((self.root / folder).glob(pattern))
                    for folder, pattern in (("episodes", "ep-*.json"), ("audio", "*.mp3")))):
                raise SettingsIntegrityError("retained Settings require explicit migration")
            raise SettingsIntegrityError("Settings require explicit fresh initialization")
        try:
            return StateStore.open(self.store_root)
        except StoreIntegrityError as exc:
            raise SettingsIntegrityError("retained Settings store is unavailable") from exc

    def read(self):
        with self._open() as store:
            row = store.read_control("settings_public", CONTROL_KEY)
        if row is None:
            raise SettingsIntegrityError("Settings require explicit initialization or migration")
        revision, raw = row
        if type(revision) is not int or revision < 1:
            raise SettingsIntegrityError("retained Settings revision is invalid")
        config = _decode(raw)
        return self._present(revision, config)

    @staticmethod
    def _present(revision, config):
        credentials = {}
        for provider, variable in (("gemini", "GEMINI_API_KEY"),
                                   ("openrouter", "OPENROUTER_API_KEY")):
            present = bool(os.environ.get(variable, "").strip())
            credentials[provider] = {
                "source": "environment" if present else "unconfigured",
                "status": "present_unverified" if present else "missing",
            }
        registry = []
        for entry in MODEL_REGISTRY:
            result = assess_model(entry, CURRENT_MODEL_BINDINGS)
            registry.append({"provider": entry["provider"], "model": entry["model"],
                             "evaluation_reference": entry["evaluation_reference"],
                             "approval_status": entry["approval_status"],
                             "approval_ref": entry["approval_ref"],
                             "evidence_bindings": dict(entry["bindings"]),
                             "current_bindings": dict(CURRENT_MODEL_BINDINGS), **result})
        selected = registry[0]
        return {
            "revision": revision,
            "config": config,
            "registry_revision": 1,
            "model_registry": registry,
            "selected_model_status": selected["status"],
            "approved_chat_available": False,
            "legacy_chat_route_status": "unconverted_unapproved",
            "provider_credentials": credentials,
            "application_status": {
                "storage": "committed",
                "chat": "blocked_pending_model_and_adapter_gate",
                "schedule": "pending_tp05_scheduler_integration",
                "retention": "pending_next_tp26_cleanup",
            },
        }

    def save(self, snapshot, *, expected_revision, initialization=None):
        config = validate_snapshot(snapshot)
        if type(expected_revision) is not int or expected_revision < 0:
            raise SettingsValidationError({"expected_revision": "nonnegative_integer_required"})
        if initialization not in (None, "fresh", "migrate"):
            raise SettingsValidationError({"initialization": "unknown_disposition"})
        if not self.store_root.exists():
            if expected_revision != 0 or initialization is None:
                raise SettingsIntegrityError("Settings require explicit initialization or migration")
            has_legacy = (self.root / "config.json").exists()
            has_retained = any(
                any((self.root / folder).glob(pattern))
                for folder, pattern in (("episodes", "ep-*.json"), ("audio", "*.mp3"))
            )
            if initialization == "fresh" and (has_legacy or has_retained):
                raise SettingsIntegrityError("retained data requires explicit migration")
            if initialization == "migrate" and not (has_legacy or has_retained):
                raise SettingsIntegrityError("no retained Settings to migrate")
            if initialization == "migrate" and has_legacy:
                _validate_legacy_config(self.root / "config.json", config["timezone_id"])
            try:
                StateStore.create(self.store_root).close()
            except StoreIntegrityError as exc:
                raise SettingsIntegrityError("could not initialize Settings store") from exc
        with self._open() as store:
            old = store.read_control("settings_public", CONTROL_KEY)
            if old is None:
                if expected_revision != 0 or initialization is None:
                    raise SettingsIntegrityError("Settings require explicit initialization or migration")
                has_legacy = (self.root / "config.json").exists()
                has_retained = any(
                    any((self.root / folder).glob(pattern))
                    for folder, pattern in (("episodes", "ep-*.json"), ("audio", "*.mp3"))
                ) or store.has_retained_records()
                if initialization == "fresh" and (has_legacy or has_retained):
                    raise SettingsIntegrityError("retained data requires explicit migration")
                if initialization == "migrate" and not (has_legacy or has_retained):
                    raise SettingsIntegrityError("no retained Settings to migrate")
                if initialization == "migrate" and has_legacy:
                    _validate_legacy_config(self.root / "config.json", config["timezone_id"])
            else:
                _decode(old[1])
                if initialization is not None:
                    raise SettingsConflict("already initialized")
                if old[0] != expected_revision:
                    raise SettingsConflict("stale Settings revision")
            event = OperationEvent.build(
                "settings", f"settings-revision-{expected_revision + 1}", "result",
                {"control_key": CONTROL_KEY, "control_revision": expected_revision + 1,
                 "outcome": "committed"}, component="settings")
            encoded = json.dumps({"schema_version": 1, "config": config},
                                 sort_keys=True, separators=(",", ":")).encode()
            result = self._present(expected_revision + 1, config)
            try:
                store.write_control_audited("settings_public", CONTROL_KEY,
                                            expected_revision, encoded, event)
            except ValueError as exc:
                if "stale control revision" in str(exc):
                    raise SettingsConflict("stale Settings revision") from exc
                raise
        return result

"""Versioned nonsecret Settings snapshots and disabled model admission.

TP05 binds the saved schedule to retained timezone rules. Chat adapter, key
custody and cleanup consumers have separate owners.
"""

import json
import hashlib
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from src.operations import OperationEvent
from src.run_coordinator import capture_zone_rules
from src.state_store import StateStore, StoreIntegrityError


CONTROL_KEY = "settings_snapshot"
FIELDS = frozenset({"max_episodes_retained", "cron_schedule", "timezone_id",
                    "chat_provider", "chat_model"})
MODEL_BINDINGS = frozenset({"api_endpoint", "model_version", "price_revision",
                            "capability_revision", "adapter_revision", "budget_revision",
                            "grounding_revision", "citation_revision", "pi_revision",
                            "real_article_revision"})
CURRENT_MODEL_BINDINGS = {
    "api_endpoint": "v1beta/models/gemini-3.6-flash",
    "model_version": "3.6-flash-07-2026",
    "price_revision": None,
    "capability_revision": None,
    "adapter_revision": None,
    "budget_revision": "direct-gemini-usd-0.05-20000-2048-one-call",
    "grounding_revision": None,
    "citation_revision": None,
    "pi_revision": None,
    "real_article_revision": None,
}
MODEL_REGISTRY = ({
    "provider": "gemini",
    "model": "gemini-3.6-flash",
    "approval_status": "pending",
    "approval_ref": None,
    "review_evidence": None,
    "bindings": dict(CURRENT_MODEL_BINDINGS),
    "evaluation_reference": "F05 Gemini Model Validation 2026-10-21",
    "budget": {"max_usd": 0.05, "max_input_tokens": 20000,
               "max_output_and_thinking_tokens": 2048, "max_calls": 1},
},)


def _initial_registry():
    return {"schema_version": 1, "revision": 1,
            "change_ref": None,
            "current_bindings": dict(CURRENT_MODEL_BINDINGS),
            "entries": [json.loads(json.dumps(MODEL_REGISTRY[0]))]}


def _validate_registry(value):
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "revision", "change_ref",
                              "current_bindings", "entries"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["revision"]) is not int or value["revision"] < 1
            or not isinstance(value["entries"], list) or len(value["entries"]) != 1
            or not isinstance(value["current_bindings"], dict)
            or set(value["current_bindings"]) != MODEL_BINDINGS):
        raise SettingsIntegrityError("retained model registry is invalid")
    entry = value["entries"][0]
    if (not isinstance(entry, dict) or set(entry) != set(MODEL_REGISTRY[0])
            or entry.get("provider") != "gemini"
            or entry.get("model") != "gemini-3.6-flash"
            or entry.get("approval_status") not in ("pending", "approved")
            or not isinstance(entry.get("bindings"), dict)
            or set(entry["bindings"]) != MODEL_BINDINGS
            or entry.get("budget") != MODEL_REGISTRY[0]["budget"]
            or (entry["approval_status"] == "pending" and
                (entry.get("approval_ref") is not None or entry.get("review_evidence") is not None))
            or (entry["approval_status"] == "approved" and
                (not entry.get("approval_ref") or not isinstance(entry.get("review_evidence"), dict)))):
        raise SettingsIntegrityError("retained model registry entry is invalid")
    return value


class SettingsIntegrityError(RuntimeError):
    """Retained Settings are missing, corrupt or require explicit migration."""


class SettingsValidationError(ValueError):
    def __init__(self, fields):
        self.fields = fields
        super().__init__("invalid Settings fields")


class SettingsConflict(ValueError):
    """The supplied revision is no longer current."""


def assess_model(entry, current_bindings, runtime_bindings=None):
    """Assess trusted registry evidence against only model-relevant inputs.

    The Settings API has no approval-writing route. TP44 must supply reviewed
    evidence and a separately controlled registry transition before enablement.
    """
    runtime_bindings = current_bindings if runtime_bindings is None else runtime_bindings
    if (entry.get("provider") != "gemini" or entry.get("model") != "gemini-3.6-flash"
            or not isinstance(current_bindings, dict)
            or set(current_bindings) != MODEL_BINDINGS
            or not isinstance(runtime_bindings, dict)
            or set(runtime_bindings) != MODEL_BINDINGS):
        return {"status": "disabled", "reason": "unsupported_registry_entry"}
    evidence = entry.get("bindings")
    if not isinstance(evidence, dict) or set(evidence) != MODEL_BINDINGS:
        return {"status": "disabled", "reason": "incomplete_evidence"}
    if entry.get("approval_status") != "approved" or not entry.get("approval_ref"):
        return {"status": "disabled", "reason": "approval_pending"}
    if not isinstance(entry.get("review_evidence"), dict):
        return {"status": "disabled", "reason": "incomplete_evidence"}
    if any(not isinstance(evidence[key], str) or not evidence[key]
           for key in MODEL_BINDINGS):
        return {"status": "disabled", "reason": "incomplete_evidence"}
    if any(current_bindings[key] != evidence[key] or runtime_bindings[key] != evidence[key]
           for key in MODEL_BINDINGS):
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
        if (not isinstance(value, dict) or set(value) != {"schema_version", "config", "registry"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 2):
            raise ValueError("unsupported Settings snapshot")
        return validate_snapshot(value["config"]), _validate_registry(value["registry"])
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


def _has_retained_data(root):
    """Treat any unknown nonempty sibling as retained, never as a fresh install."""
    if not root.exists():
        return False
    for path in root.iterdir():
        if path.name == "state":
            continue
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            return True
    return False


class SettingsService:
    @staticmethod
    def fresh_defaults():
        """Only the two owner-approved new-install defaults."""
        return {"max_episodes_retained": 14, "timezone_id": "Asia/Singapore"}

    def __init__(self, storage_root):
        self.root = Path(storage_root)
        # TP02/TP28 publication, lifecycle and required audit share this root.
        self.store_root = self.root / "state"

    def initialization_kind(self):
        """Classify an absent Settings row without guessing through corruption."""
        retained_files = _has_retained_data(self.root)
        if not self.store_root.exists():
            return "migrate" if retained_files else "fresh"
        try:
            with StateStore.open(self.store_root) as store:
                if store.read_control("settings_public", CONTROL_KEY) is not None:
                    return "blocked"
                return "migrate" if retained_files or store.has_retained_records() else "fresh"
        except StoreIntegrityError:
            return "blocked"

    def _open(self):
        if not self.store_root.exists():
            if _has_retained_data(self.root):
                raise SettingsIntegrityError("retained Settings require explicit migration")
            raise SettingsIntegrityError("Settings require explicit fresh initialization")
        try:
            return StateStore.open(self.store_root)
        except StoreIntegrityError as exc:
            raise SettingsIntegrityError("retained Settings store is unavailable") from exc

    def read(self):
        try:
            with self._open() as store:
                row = store.read_settings_control_verified(CONTROL_KEY)
        except StoreIntegrityError as exc:
            raise SettingsIntegrityError("retained Settings audit is unavailable") from exc
        if row is None:
            raise SettingsIntegrityError("Settings require explicit initialization or migration")
        revision, raw = row
        if type(revision) is not int or revision < 1:
            raise SettingsIntegrityError("retained Settings revision is invalid")
        config, registry = _decode(raw)
        return self._present(revision, config, registry)

    @staticmethod
    def _present(revision, config, retained_registry):
        credentials = {}
        for provider, variable in (("gemini", "GEMINI_API_KEY"),
                                   ("openrouter", "OPENROUTER_API_KEY")):
            present = bool(os.environ.get(variable, "").strip())
            credentials[provider] = {
                "source": "environment" if present else "unconfigured",
                "status": "present_unverified" if present else "missing",
            }
        registry = []
        for entry in retained_registry["entries"]:
            result = assess_model(entry, retained_registry["current_bindings"],
                                  CURRENT_MODEL_BINDINGS)
            registry.append({"provider": entry["provider"], "model": entry["model"],
                             "evaluation_reference": entry["evaluation_reference"],
                             "approval_status": entry["approval_status"],
                             "approval_ref": entry["approval_ref"],
                             "review_evidence": entry["review_evidence"],
                             "evidence_bindings": dict(entry["bindings"]),
                             "current_bindings": dict(retained_registry["current_bindings"]),
                             "runtime_bindings": dict(CURRENT_MODEL_BINDINGS),
                             "budget": dict(entry["budget"]), **result})
        selected = registry[0]
        return {
            "revision": revision,
            "config": config,
            "registry_revision": retained_registry["revision"],
            "registry_change_ref": retained_registry["change_ref"],
            "model_registry": registry,
            "selected_model_status": selected["status"],
            "approved_chat_available": False,
            "legacy_chat_route_status": "unconverted_unapproved",
            "provider_credentials": credentials,
            "application_status": {
                "storage": "committed",
                "chat": "blocked_pending_model_and_adapter_gate",
                "schedule": "committed_pending_scheduler_observation",
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
            has_retained = _has_retained_data(self.root)
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
            try:
                old = store.read_settings_control_verified(CONTROL_KEY)
            except StoreIntegrityError as exc:
                raise SettingsIntegrityError("retained Settings audit is unavailable") from exc
            if old is None:
                if expected_revision != 0 or initialization is None:
                    raise SettingsIntegrityError("Settings require explicit initialization or migration")
                has_legacy = (self.root / "config.json").exists()
                has_retained = _has_retained_data(self.root) or store.has_retained_records()
                if initialization == "fresh" and (has_legacy or has_retained):
                    raise SettingsIntegrityError("retained data requires explicit migration")
                if initialization == "migrate" and not (has_legacy or has_retained):
                    raise SettingsIntegrityError("no retained Settings to migrate")
                if initialization == "migrate" and has_legacy:
                    _validate_legacy_config(self.root / "config.json", config["timezone_id"])
                registry = _initial_registry()
            else:
                _, registry = _decode(old[1])
                if initialization is not None:
                    raise SettingsConflict("already initialized")
                if old[0] != expected_revision:
                    raise SettingsConflict("stale Settings revision")
            return self._commit(store, config, registry, expected_revision)

    def _commit(self, store, config, registry, expected_revision):
        try:
            rules = capture_zone_rules(config["timezone_id"])
        except ValueError as exc:
            raise SettingsValidationError({"timezone_id": "rule_bytes_unavailable"}) from exc
        encoded = json.dumps({"schema_version": 2, "config": config,
                              "registry": registry},
                             sort_keys=True, separators=(",", ":")).encode()
        event = OperationEvent.build(
            "settings", f"settings-revision-{expected_revision + 1}", "result",
            {"control_key": CONTROL_KEY, "control_revision": expected_revision + 1,
             "evidence_digest": hashlib.sha256(encoded).hexdigest(),
             "outcome": "committed"}, component="settings")
        result = self._present(expected_revision + 1, config, registry)
        try:
            store.write_control_audited("settings_public", CONTROL_KEY,
                                        expected_revision, encoded, event,
                                        schedule={"cron": config["cron_schedule"],
                                                  "timezone_id": rules.zone_id,
                                                  "rule_bytes": rules.data,
                                                  "rule_sha256": rules.sha256,
                                                  "rule_provider": rules.provider})
        except ValueError as exc:
            if "stale control revision" in str(exc):
                raise SettingsConflict("stale Settings revision") from exc
            raise
        return result

    def record_model_approval(self, review, *, expected_revision):
        """Persist a revision-bound TP44 review result; no public API or call enablement.

        TP44 must verify the referenced real-article, provider, cost and PI
        evidence before invoking this internal transition.
        """
        required = {"decision", "approval_ref", "approval_event", "reviewed_settings_revision",
                    "bindings", "real_article_pass_ref", "price_pass_ref",
                    "capability_pass_ref", "adapter_pass_ref", "budget_pass_ref",
                    "grounding_pass_ref", "citation_pass_ref", "pi_pass_ref"}
        gate_names = {"real_article_pass_ref", "price_pass_ref", "capability_pass_ref",
                      "adapter_pass_ref", "budget_pass_ref", "grounding_pass_ref",
                      "citation_pass_ref", "pi_pass_ref"}
        def valid_digest(value):
            return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        def valid_gate(value):
            return (isinstance(value, dict)
                    and set(value) == {"verdict", "artifact_sha256", "reference"}
                    and value["verdict"] == "PASS"
                    and valid_digest(value["artifact_sha256"])
                    and isinstance(value["reference"], str) and bool(value["reference"]))
        approval = review.get("approval_event") if isinstance(review, dict) else None
        if (not isinstance(review, dict) or set(review) != required
                or review.get("decision") != "approved"
                or type(review.get("reviewed_settings_revision")) is not int
                or review["reviewed_settings_revision"] != expected_revision
                or not isinstance(review.get("approval_ref"), str) or not review["approval_ref"]
                or not isinstance(approval, dict)
                or set(approval) != {"review_stage", "verdict", "reviewed_settings_revision",
                                     "approval_ref", "report_sha256"}
                or approval["review_stage"] != "TP44"
                or approval["verdict"] != "PASS"
                or approval["reviewed_settings_revision"] != expected_revision
                or approval["approval_ref"] != review["approval_ref"]
                or not valid_digest(approval["report_sha256"])
                or any(not valid_gate(review.get(key)) for key in gate_names)
                or not isinstance(review.get("bindings"), dict)
                or set(review["bindings"]) != MODEL_BINDINGS
                or any(not isinstance(value, str) or not value
                       for value in review["bindings"].values())
                or review["bindings"] != CURRENT_MODEL_BINDINGS):
            raise SettingsValidationError({"model_review": "complete_current_review_required"})
        with self._open() as store:
            try:
                old = store.read_settings_control_verified(CONTROL_KEY)
            except StoreIntegrityError as exc:
                raise SettingsIntegrityError("retained Settings audit is unavailable") from exc
            if old is None:
                raise SettingsIntegrityError("Settings require explicit initialization")
            if old[0] != expected_revision:
                raise SettingsConflict("stale Settings revision")
            config, registry = _decode(old[1])
            if registry["entries"][0]["approval_status"] != "pending":
                raise SettingsConflict("model review transition already recorded")
            registry["revision"] += 1
            registry["change_ref"] = review["approval_ref"]
            registry["current_bindings"] = dict(review["bindings"])
            registry["entries"][0]["bindings"] = dict(review["bindings"])
            registry["entries"][0]["approval_status"] = "approved"
            registry["entries"][0]["approval_ref"] = review["approval_ref"]
            registry["entries"][0]["review_evidence"] = {
                key: review[key] for key in required - {"decision", "bindings"}}
            return self._commit(store, config, registry, expected_revision)

    def record_model_binding_change(self, binding, value, *, reason_ref, expected_revision):
        """Record a relevant provider/model evidence change and stale its approval."""
        if (binding not in MODEL_BINDINGS or not isinstance(value, str) or not value
                or not isinstance(reason_ref, str) or not reason_ref):
            raise SettingsValidationError({"model_binding": "revision_and_reason_required"})
        with self._open() as store:
            try:
                old = store.read_settings_control_verified(CONTROL_KEY)
            except StoreIntegrityError as exc:
                raise SettingsIntegrityError("retained Settings audit is unavailable") from exc
            if old is None:
                raise SettingsIntegrityError("Settings require explicit initialization")
            if old[0] != expected_revision:
                raise SettingsConflict("stale Settings revision")
            config, registry = _decode(old[1])
            if registry["current_bindings"][binding] == value:
                raise SettingsConflict("model binding is unchanged")
            registry["revision"] += 1
            registry["change_ref"] = reason_ref
            registry["current_bindings"][binding] = value
            return self._commit(store, config, registry, expected_revision)

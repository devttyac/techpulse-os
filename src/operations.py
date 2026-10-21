"""Sanitized, versioned operation events for the TP28 retained repository.

Required records share a database transaction with their affected state. A
diagnostic record has its own bounded sink and cannot authorize an effect.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import sqlite3
import uuid


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELD_NAMES = frozenset({
    "run_id", "request_id", "episode_id", "article_id", "artifact_id",
    "source_kind", "source_revision", "model_id", "recipe_revision",
    "outcome", "reason", "error_category", "status_code", "count",
    "duration_ms", "byte_count", "control_revision", "control_key",
    "evidence_digest", "coverage_digest", "episode_revision", "evidence_count", "coverage_count",
    "observed_state", "trigger",
})
_NUMERIC_FIELDS = frozenset({"status_code", "count", "duration_ms", "byte_count",
                            "control_revision", "evidence_count", "coverage_count"})
_DIGEST_FIELDS = frozenset({"source_revision", "recipe_revision", "evidence_digest",
                           "coverage_digest", "episode_revision"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ENUM_FIELDS = {
    "outcome": frozenset({"committed", "failed", "verified_removed", "verified_present",
                          "observed_absent", "observed_present", "ambiguous", "unknown",
                          "call_returned", "checked", "rejected", "pending", "started",
                          "completed", "skipped", "interrupted", "unavailable", "warning"}),
    "reason": frozenset({"audit_unavailable", "source_unavailable", "policy_rejected",
                         "integrity_failed", "resource_deferred", "unknown"}),
    "error_category": frozenset({"cancelled", "timeout", "transport", "auth", "quota",
                                 "unexpected_error", "integrity", "policy", "resource", "unknown"}),
    "source_kind": frozenset({"rss", "html", "api", "manual", "unknown"}),
    "model_id": frozenset({"gemini-3.6-flash", "unknown_model"}),
    "observed_state": frozenset({"observed_absent", "observed_present", "ambiguous", "unknown"}),
    "trigger": frozenset({"scheduled", "manual", "recovery"}),
    "control_key": frozenset({"schedule", "cron_schedule", "timezone",
                              "max_episodes_retained", "chapters_per_episode",
                              "gemini_model", "source_policy", "provider_source",
                              "retention_policy"}),
}
_COMPONENTS = {
    "settings": frozenset({"settings"}),
    "publication": frozenset({"publisher"}),
    "deletion": frozenset({"lifecycle"}),
    "restoration": frozenset({"restoration"}),
    "run": frozenset({"scheduler", "pipeline"}),
    "health_detail": frozenset({"health"}),
    "request_detail": frozenset({"api"}),
    "source_detail": frozenset({"source"}),
}
_CONTEXT_KEYS = frozenset({"nominal_slot", "schedule_revision", "resolved_instant",
                           "timezone", "rule_revision", "actual_started", "actual_finished",
                           "disposition"})
_WINDOW_DISPOSITIONS = frozenset({"on_time", "gap", "fold_first", "fold_second",
                                   "catch_up", "skipped", "future_only", "unknown"})


def _qualified_time(value):
    if not isinstance(value, str):
        raise ValueError("operation time must be a timezone-qualified string")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("operation time is invalid") from exc
    if instant.utcoffset() is None:
        raise ValueError("operation time must include an offset")
    return value


def _nominal_time(value):
    if not isinstance(value, str):
        raise ValueError("nominal slot must be an ISO date and time")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("nominal slot is invalid") from exc
    return value


def _safe_identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("operation value must be a bounded identifier")
    if value.lower().startswith(("sk_", "sk-", "key_", "secret_", "bearer", "aiza")):
        raise ValueError("secret-like value is not an operation identifier")
    for name in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "API_SECRET_KEY"):
        secret = os.environ.get(name, "").strip()
        if secret and secret in value:
            raise ValueError("operation value contains a secret")
    return value


def membership_digest(identifiers):
    """Hash an ordered, validated set of references without logging bodies."""
    values = [_safe_identifier(value) for value in identifiers]
    if len(set(values)) != len(values):
        raise ValueError("operation membership is repeated")
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class OperationPolicy:
    """A caller must classify its operation before allowing side effects."""

    REQUIRED = frozenset({"settings", "publication", "deletion", "restoration", "run"})
    DIAGNOSTIC = frozenset({"health_detail", "request_detail", "source_detail"})

    @classmethod
    def duty(cls, kind):
        if kind in cls.REQUIRED:
            return "required"
        if kind in cls.DIAGNOSTIC:
            return "diagnostic"
        raise ValueError("unknown operation kind")


@dataclass(frozen=True)
class OperationEvent:
    schema_version: int
    event_id: str
    operation_id: str
    kind: str
    duty: str
    phase: str
    component: str
    observed_at: str
    fields: dict
    context: dict

    @classmethod
    def build(cls, kind, operation_id, phase, fields=None, *, component, context=None):
        duty = OperationPolicy.duty(kind)
        if phase not in ("intent", "result", "reconciliation"):
            raise ValueError("unknown operation phase")
        if duty == "diagnostic" and phase != "result":
            raise ValueError("diagnostic events cannot authorize effects")
        if component not in _COMPONENTS[kind]:
            raise ValueError("unknown component for operation kind")
        if not isinstance(fields, dict) or not fields or not set(fields) <= _FIELD_NAMES:
            raise ValueError("operation fields are not allowlisted")
        safe = {}
        for key, value in fields.items():
            if key in _NUMERIC_FIELDS:
                if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
                    raise ValueError("invalid operation measure")
                safe[key] = value
            elif key in _ENUM_FIELDS:
                if value not in _ENUM_FIELDS[key]:
                    raise ValueError("unknown operation category")
                safe[key] = value
            elif key in _DIGEST_FIELDS:
                if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                    raise ValueError("operation digest must be SHA-256")
                safe[key] = value
            elif value is None:
                safe[key] = None
            else:
                safe[key] = _safe_identifier(value)
        required_identity = {"settings": "control_key", "publication": "episode_id",
                             "deletion": "artifact_id", "restoration": "episode_id", "run": "run_id"}
        if duty == "required" and not safe.get(required_identity[kind]):
            raise ValueError("required operation lacks subject correlation")
        if kind == "run" and safe["run_id"] != operation_id:
            raise ValueError("run identity mismatch")
        if phase in ("result", "reconciliation") and duty == "required" and "outcome" not in safe:
            raise ValueError("required result lacks outcome")
        if kind == "run" and "trigger" not in safe:
            raise ValueError("run event lacks trigger")
        safe_context = cls._validate_context(kind, safe, context)
        return cls(2, str(uuid.uuid4()), _safe_identifier(operation_id), kind,
                   duty, phase, component, datetime.now(timezone.utc).isoformat(), safe,
                   safe_context)

    @staticmethod
    def _validate_context(kind, fields, context):
        if context is None:
            context = {}
        if not isinstance(context, dict):
            raise ValueError("operation context must be an object")
        if kind != "run" or fields.get("trigger") != "scheduled":
            if context:
                raise ValueError("window context requires a scheduled run")
            return {}
        if set(context) != _CONTEXT_KEYS:
            raise ValueError("scheduled run lacks complete window context")
        safe = dict(context)
        _nominal_time(safe["nominal_slot"])
        for key in ("resolved_instant", "actual_started"):
            _qualified_time(safe[key])
        if safe["actual_finished"] is not None:
            _qualified_time(safe["actual_finished"])
        if type(safe["schedule_revision"]) is not int or safe["schedule_revision"] < 1:
            raise ValueError("invalid schedule revision")
        if (not isinstance(safe["timezone"], str) or
                not re.fullmatch(r"[A-Za-z_+-]+(?:/[A-Za-z_+0-9-]+)*", safe["timezone"])):
            raise ValueError("invalid timezone identifier")
        if not isinstance(safe["rule_revision"], str) or not re.fullmatch(r"[0-9]{4}[a-z]", safe["rule_revision"]):
            raise ValueError("invalid timezone rule revision")
        if safe["disposition"] not in _WINDOW_DISPOSITIONS:
            raise ValueError("invalid window disposition")
        return safe

    def validate(self):
        """Check a possibly replaced/mutated event before durable storage."""
        rebuilt = self.build(self.kind, self.operation_id, self.phase, self.fields,
                             component=self.component, context=self.context)
        if self.schema_version != 2 or self.duty != rebuilt.duty:
            raise ValueError("invalid operation event schema or duty")
        try:
            if str(uuid.UUID(self.event_id)) != self.event_id:
                raise ValueError("invalid event identifier")
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid event identifier") from exc
        _qualified_time(self.observed_at)


def record_diagnostic(store, event, *, max_records=1000, max_bytes=1024 * 1024):
    """Return a displayable warning if ordinary detail could not persist."""
    try:
        store.append_diagnostic(event, max_records=max_records, max_bytes=max_bytes)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return "diagnostic_not_persisted"
    return None

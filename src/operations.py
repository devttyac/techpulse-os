"""Sanitized, versioned operation events for the TP28 retained repository.

Required records share a database transaction with their affected state. A
diagnostic record has its own bounded sink and cannot authorize an effect.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import sqlite3
import uuid


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FIELD_NAMES = frozenset({
    "run_id", "request_id", "episode_id", "article_id", "artifact_id",
    "source_kind", "source_revision", "model_id", "recipe_revision",
    "outcome", "reason", "error_category", "status_code", "count",
    "duration_ms", "byte_count", "control_revision", "observed_state",
})
_NUMERIC_FIELDS = frozenset({"status_code", "count", "duration_ms", "byte_count", "control_revision"})


def _safe_identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("operation value must be a bounded identifier")
    for name in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "API_SECRET_KEY"):
        secret = os.environ.get(name, "").strip()
        if secret and secret in value:
            raise ValueError("operation value contains a secret")
    return value


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
    observed_at: str
    fields: dict

    @classmethod
    def build(cls, kind, operation_id, phase, fields=None):
        duty = OperationPolicy.duty(kind)
        if phase not in ("intent", "result", "reconciliation"):
            raise ValueError("unknown operation phase")
        if duty == "diagnostic" and phase != "result":
            raise ValueError("diagnostic events cannot authorize effects")
        if not isinstance(fields, dict) or not set(fields) <= _FIELD_NAMES:
            raise ValueError("operation fields are not allowlisted")
        safe = {}
        for key, value in fields.items():
            if key in _NUMERIC_FIELDS:
                if type(value) not in (int, float) or value < 0 or value != value:
                    raise ValueError("invalid operation measure")
                safe[key] = value
            elif value is None:
                safe[key] = None
            else:
                safe[key] = _safe_identifier(value)
        return cls(2, str(uuid.uuid4()), _safe_identifier(operation_id), kind,
                   duty, phase, datetime.now(timezone.utc).isoformat(), safe)


def record_diagnostic(store, event, *, max_records=1000, max_bytes=1024 * 1024):
    """Return a displayable warning if ordinary detail could not persist."""
    try:
        store.append_diagnostic(event, max_records=max_records, max_bytes=max_bytes)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return "diagnostic_not_persisted"
    return None

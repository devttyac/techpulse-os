"""Versioned local state and TP28 transactional operation primitives.

This module does not store credentials. Callers must validate evidence and
protect secrets before using later integrations.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import quote

from src.operations import OperationEvent, OperationPolicy, membership_digest


_SCHEMA_VERSION = 2
_APPLICATION_ID = 0x54504F53
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TABLES = frozenset({
    "artifacts", "episodes", "episode_artifacts", "coverage",
    "control_revisions", "operation_journal",
})
_TABLES_V2 = _TABLES | frozenset({"operation_events", "effect_intents", "diagnostic_events"})
_EXPECTED_COLUMNS = {
    "artifacts": ("artifact_id", "kind", "content_sha256", "byte_length", "metadata_json"),
    "episodes": ("episode_id", "metadata_json"),
    "episode_artifacts": ("episode_id", "artifact_id", "position"),
    "coverage": ("coverage_key", "episode_id"),
    "control_revisions": ("namespace", "control_key", "revision", "value"),
    "operation_journal": ("operation_id", "kind", "status", "metadata_json"),
    "operation_events": ("event_id", "operation_id", "kind", "phase", "component",
                         "observed_at", "fields_json", "context_json"),
    "effect_intents": ("operation_id", "kind", "status", "effect_status", "intent_event_id", "resolution_event_id"),
    "diagnostic_events": ("sequence", "event_id", "observed_at", "event_json", "byte_length"),
}


class StoreIntegrityError(RuntimeError):
    """The retained store is missing, incompatible, or not trustworthy."""


def _json_object(value):
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_json_object(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StoreIntegrityError("invalid retained metadata") from exc
    if not isinstance(value, dict):
        raise StoreIntegrityError("invalid retained metadata")
    return value


def _id(value):
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ValueError("invalid identifier")
    return value


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class StateStore:
    """Single-process SQLite index with immutable same-volume object files.

    Rollback journaling and FULL sync are used until TP49 qualifies WAL for a
    particular local filesystem. A returned write has flushed its object and
    database transaction; later deployment must still verify volume behavior.
    """

    def __init__(self, root, connection):
        self.root = Path(root)
        self._db = connection

    @classmethod
    def create(cls, root):
        """Initialize only an explicitly requested empty local retained root."""
        root = Path(root)
        if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise StoreIntegrityError("retained root is not empty")
        root.mkdir(parents=True, exist_ok=True)
        (root / "objects").mkdir()
        (root / "staging").mkdir()
        db_path = root / "state.sqlite3"
        descriptor = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        _sync_directory(root)
        try:
            db = cls._connect(db_path)
            schema = (Path(__file__).parent / "storage" / "schema_v1.sql").read_text(encoding="utf-8")
            db.executescript(schema)
            schema_v2 = (Path(__file__).parent / "storage" / "schema_v2.sql").read_text(encoding="utf-8")
            db.executescript(schema_v2)
            db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            db.commit()
            store = cls(root, db)
            store._verify()
            return store
        except (OSError, sqlite3.Error) as exc:
            raise StoreIntegrityError("failed to initialize retained store") from exc

    @classmethod
    def open(cls, root):
        """Open only a known, valid store. Never create state on this path."""
        root = Path(root)
        db_path = root / "state.sqlite3"
        if (root.is_symlink() or not root.is_dir() or not db_path.is_file()
                or db_path.is_symlink() or not (root / "objects").is_dir()
                or (root / "objects").is_symlink()
                or not (root / "staging").is_dir()
                or (root / "staging").is_symlink()):
            raise StoreIntegrityError("retained store is missing or incomplete")
        try:
            db = cls._connect(db_path)
            store = cls(root, db)
            store._verify()
            if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                store._migrate_v1()
                store._verify()
            return store
        except sqlite3.Error as exc:
            raise StoreIntegrityError("retained database is unreadable") from exc

    @staticmethod
    def _connect(path):
        db = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=rw", uri=True, timeout=5)
        db.execute("PRAGMA foreign_keys = ON")
        mode = db.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if mode.lower() != "delete":
            db.close()
            raise StoreIntegrityError("rollback journal unavailable")
        db.execute("PRAGMA synchronous = FULL")
        return db

    def _verify(self):
        try:
            if self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise StoreIntegrityError("retained database failed integrity check")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            app_id = self._db.execute("PRAGMA application_id").fetchone()[0]
            tables = {row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            expected = _TABLES if version == 1 else _TABLES_V2 if version == 2 else None
            if expected is None or app_id != _APPLICATION_ID or not expected <= tables:
                raise StoreIntegrityError("retained schema is missing or incompatible")
            for table in expected:
                columns = tuple(row[1] for row in self._db.execute(f"PRAGMA table_info({table})"))
                if columns != _EXPECTED_COLUMNS[table]:
                    raise StoreIntegrityError("retained schema shape is incompatible")
            if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreIntegrityError("retained database has broken references")
            if version == 2:
                self._verify_effects()
        except sqlite3.Error as exc:
            raise StoreIntegrityError("retained database failed integrity check") from exc

    def _verify_effects(self):
        rows = self._db.execute(
            "SELECT e.operation_id, e.kind, e.status, e.effect_status, "
            "i.operation_id, i.kind, i.phase, i.fields_json, "
            "r.operation_id, r.kind, r.phase, r.fields_json "
            "FROM effect_intents e "
            "LEFT JOIN operation_events i ON i.event_id = e.intent_event_id "
            "LEFT JOIN operation_events r ON r.event_id = e.resolution_event_id"
        )
        for (operation_id, kind, status, effect_status, intent_op, intent_kind,
             intent_phase, intent_fields, result_op, result_kind, result_phase,
             result_fields) in rows:
            if (intent_op != operation_id or intent_kind != kind or intent_phase != "intent"
                    or kind not in ("deletion", "restoration")):
                raise StoreIntegrityError("effect intent has invalid required record")
            identity_key = "artifact_id" if kind == "deletion" else "episode_id"
            original = _read_json_object(intent_fields)
            if not original.get(identity_key):
                raise StoreIntegrityError("effect intent lacks retained identity")
            if status == "resolved" and result_op is None:
                raise StoreIntegrityError("resolved effect lacks durable result")
            if ((status == "pending" and effect_status not in ("unknown", "ambiguous"))
                    or (status == "resolved" and effect_status not in (
                        "verified_removed", "verified_present", "failed",
                        "observed_absent", "observed_present"))):
                raise StoreIntegrityError("effect status contradicts resolution state")
            if result_op is not None:
                if (result_op != operation_id or result_kind != kind
                        or result_phase not in ("result", "reconciliation")):
                    raise StoreIntegrityError("effect result record does not match intent")
                if ((result_phase == "result" and effect_status not in (
                        "verified_removed", "verified_present", "unknown", "failed"))
                        or (result_phase == "reconciliation" and effect_status not in (
                            "observed_absent", "observed_present", "ambiguous"))):
                    raise StoreIntegrityError("effect phase contradicts observed status")
                result = _read_json_object(result_fields)
                if (result.get(identity_key) != original[identity_key]
                        or result.get("outcome") != effect_status):
                    raise StoreIntegrityError("effect result contradicts retained state")
            elif effect_status != "unknown":
                raise StoreIntegrityError("effect status has no durable observation")

    def _migrate_v1(self):
        """Add v2 tables atomically without rewriting historical v1 records."""
        schema = (Path(__file__).parent / "storage" / "schema_v2.sql").read_text(encoding="utf-8")
        try:
            self._db.executescript("BEGIN IMMEDIATE;\n" + schema +
                                   "\nPRAGMA user_version = 2;\nCOMMIT;")
        except sqlite3.Error as exc:
            self._db.rollback()
            raise StoreIntegrityError("retained migration failed") from exc

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _blob_path(self, digest):
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise StoreIntegrityError("invalid retained digest")
        return self.root / "objects" / digest

    def _verify_blob(self, digest, size):
        path = self._blob_path(digest)
        if path.is_symlink():
            raise StoreIntegrityError("retained object is not a regular local file")
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise StoreIntegrityError("retained object unavailable") from exc
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise StoreIntegrityError("retained object checksum mismatch")
        return body

    def put_artifact(self, artifact_id, kind, body, metadata):
        """Install verified bytes before committing the immutable index row."""
        artifact_id, kind = _id(artifact_id), _id(kind)
        if not isinstance(body, bytes):
            raise ValueError("artifact body must be bytes")
        encoded = _json_object(metadata)
        digest = hashlib.sha256(body).hexdigest()
        target = self._blob_path(digest)
        temporary = None
        try:
            if target.is_symlink():
                raise StoreIntegrityError("retained object is not a regular local file")
            if target.exists():
                self._verify_blob(digest, len(body))
            else:
                with tempfile.NamedTemporaryFile(dir=self.root / "staging", prefix="blob-", delete=False) as fp:
                    temporary = Path(fp.name)
                    os.chmod(temporary, 0o600)
                    fp.write(body)
                    fp.flush()
                    os.fsync(fp.fileno())
                self._verify_staged(temporary, digest, len(body))
                os.replace(temporary, target)
                temporary = None
                _sync_directory(target.parent)
            with self._transaction():
                self._db.execute(
                    "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?)",
                    (artifact_id, kind, digest, len(body), encoded),
                )
            return digest
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_staged(path, digest, size):
        staged = path.read_bytes()
        if len(staged) != size or hashlib.sha256(staged).hexdigest() != digest:
            raise StoreIntegrityError("staged object checksum mismatch")

    def read_artifact(self, artifact_id):
        row = self._db.execute(
            "SELECT content_sha256, byte_length, metadata_json FROM artifacts WHERE artifact_id = ?",
            (_id(artifact_id),),
        ).fetchone()
        if row is None:
            return None
        digest, size, metadata = row
        return self._verify_blob(digest, size), _read_json_object(metadata)

    def list_artifact_ids(self):
        ids = [row[0] for row in self._db.execute("SELECT artifact_id FROM artifacts ORDER BY artifact_id")]
        for artifact_id in ids:
            self.read_artifact(artifact_id)
        return ids

    def publish_episode(self, episode_id, artifact_ids, coverage_keys, metadata):
        """Publish episode joins and coverage in one database transaction."""
        episode_id = _id(episode_id)
        ids = [_id(item) for item in artifact_ids]
        keys = [_id(item) for item in coverage_keys]
        if not ids or len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise ValueError("invalid publication references")
        encoded = _json_object(metadata)
        for item in ids:
            if self.read_artifact(item) is None:
                raise StoreIntegrityError("publication evidence unavailable")
        with self._transaction():
            self._insert_episode(episode_id, ids, keys, encoded)

    def _insert_episode(self, episode_id, ids, keys, encoded):
        self._db.execute("INSERT INTO episodes VALUES (?, ?)", (episode_id, encoded))
        self._db.executemany(
            "INSERT INTO episode_artifacts VALUES (?, ?, ?)",
            ((episode_id, item, index) for index, item in enumerate(ids)),
        )
        self._db.executemany(
            "INSERT INTO coverage VALUES (?, ?)",
            ((key, episode_id) for key in keys),
        )

    def publish_episode_audited(self, episode_id, artifact_ids, coverage_keys, metadata, event):
        """Commit publication and its required result in one transaction."""
        self._require_event(event, "publication", "result")
        episode_id = _id(episode_id)
        ids = [_id(item) for item in artifact_ids]
        keys = [_id(item) for item in coverage_keys]
        if not ids or len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise ValueError("invalid publication references")
        encoded = _json_object(metadata)
        for item in ids:
            if self.read_artifact(item) is None:
                raise StoreIntegrityError("publication evidence unavailable")
        with self._transaction():
            if event.fields.get("outcome") != "committed" or event.fields.get("episode_id") != episode_id:
                raise ValueError("publication audit result contradicts committed episode")
            if (event.fields.get("evidence_digest") != membership_digest(ids)
                    or event.fields.get("coverage_digest") != membership_digest(keys)
                    or event.fields.get("episode_revision") != hashlib.sha256(encoded.encode()).hexdigest()
                    or event.fields.get("evidence_count") != len(ids)
                    or event.fields.get("coverage_count") != len(keys)):
                raise ValueError("publication audit references do not match exact joins")
            self._insert_episode(episode_id, ids, keys, encoded)
            self._insert_required_event(event)

    def episode(self, episode_id):
        episode_id = _id(episode_id)
        row = self._db.execute("SELECT metadata_json FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()
        if row is None:
            return None
        ids = [record[0] for record in self._db.execute(
            "SELECT artifact_id FROM episode_artifacts WHERE episode_id = ? ORDER BY position",
            (episode_id,),
        )]
        if not ids:
            raise StoreIntegrityError("published episode has no evidence")
        for item in ids:
            if self.read_artifact(item) is None:
                raise StoreIntegrityError("published evidence unavailable")
        return {"metadata": _read_json_object(row[0]), "artifacts": ids}

    def coverage_owner(self, key):
        row = self._db.execute("SELECT episode_id FROM coverage WHERE coverage_key = ?", (_id(key),)).fetchone()
        if row is None:
            return None
        if self.episode(row[0]) is None:
            raise StoreIntegrityError("coverage episode unavailable")
        return row[0]

    def write_control(self, namespace, key, expected_revision, value):
        """Commit non-secret public settings or restoration control bytes by CAS."""
        if namespace not in ("settings_public", "restoration"):
            raise ValueError("unsupported control namespace")
        key = _id(key)
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(value, bytes):
            raise ValueError("invalid control revision or value")
        with self._transaction():
            return self._write_control(namespace, key, expected_revision, value)

    def _write_control(self, namespace, key, expected_revision, value):
        row = self._db.execute(
            "SELECT revision FROM control_revisions WHERE namespace = ? AND control_key = ?",
            (namespace, key),
        ).fetchone()
        actual = row[0] if row else 0
        if actual != expected_revision:
            raise ValueError("stale control revision")
        revision = actual + 1
        self._db.execute(
            "INSERT INTO control_revisions VALUES (?, ?, ?, ?) "
            "ON CONFLICT(namespace, control_key) DO UPDATE SET revision = excluded.revision, value = excluded.value",
            (namespace, key, revision, value),
        )
        return revision

    def write_control_audited(self, namespace, key, expected_revision, value, event):
        """Atomically write public control bytes and their required audit."""
        self._require_event(event, "settings", "result")
        if namespace != "settings_public":
            raise ValueError("audited settings require settings_public namespace")
        key = _id(key)
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(value, bytes):
            raise ValueError("invalid control revision or value")
        with self._transaction():
            revision = self._write_control(namespace, key, expected_revision, value)
            if (event.fields.get("outcome") != "committed"
                    or event.fields.get("control_key") != key
                    or event.fields.get("control_revision") != revision):
                raise ValueError("settings audit result contradicts committed control")
            self._insert_required_event(event)
            return revision

    def read_control(self, namespace, key):
        if namespace not in ("settings_public", "restoration"):
            raise ValueError("unsupported control namespace")
        row = self._db.execute(
            "SELECT revision, value FROM control_revisions WHERE namespace = ? AND control_key = ?",
            (namespace, _id(key)),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def begin_operation(self, operation_id, kind, metadata):
        if kind not in ("publication", "restoration"):
            raise ValueError("unsupported operation kind")
        with self._transaction():
            self._db.execute(
                "INSERT INTO operation_journal VALUES (?, ?, 'pending', ?)",
                (_id(operation_id), kind, _json_object(metadata)),
            )

    def finish_operation(self, operation_id, status):
        if status not in ("completed", "interrupted", "failed"):
            raise ValueError("invalid operation outcome")
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE operation_journal SET status = ? WHERE operation_id = ? AND status = 'pending'",
                (status, _id(operation_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("operation is not pending")

    def pending_operations(self):
        return [
            {"id": operation_id, "kind": kind, "metadata": _read_json_object(metadata)}
            for operation_id, kind, metadata in self._db.execute(
                "SELECT operation_id, kind, metadata_json FROM operation_journal "
                "WHERE status = 'pending' ORDER BY operation_id"
            )
        ]

    @staticmethod
    def _require_event(event, kind, phase):
        if (not isinstance(event, OperationEvent) or event.kind != kind
                or event.phase != phase or event.duty != OperationPolicy.duty(kind)
                or event.schema_version != 2):
            raise ValueError("event does not match the required operation boundary")
        event.validate()

    def _insert_required_event(self, event):
        if not isinstance(event, OperationEvent) or event.duty != "required":
            raise ValueError("required event expected")
        event.validate()
        self._db.execute(
            "INSERT INTO operation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, event.operation_id, event.kind, event.phase,
             event.component, event.observed_at, _json_object(event.fields),
             _json_object(event.context)),
        )
        return event.event_id

    def operation_events(self, operation_id):
        """Read required records without interpreting intent as success."""
        rows = self._db.execute(
            "SELECT event_id, kind, phase, component, observed_at, fields_json, context_json "
            "FROM operation_events "
            "WHERE operation_id = ? ORDER BY rowid", (_id(operation_id),),
        )
        return [{"event_id": event_id, "kind": kind, "phase": phase,
                 "component": component, "observed_at": observed_at,
                 "fields": _read_json_object(fields), "context": _read_json_object(context)}
                for event_id, kind, phase, component, observed_at, fields, context in rows]

    def record_run_event(self, event):
        """Persist a run observation; callers must not use this for effects."""
        self._require_event(event, "run", "result")
        with self._transaction():
            return self._insert_required_event(event)

    def begin_effect(self, operation_id, kind, intent):
        """Persist authorization before a caller attempts any physical effect."""
        if kind not in ("deletion", "restoration") or operation_id != intent.operation_id:
            raise ValueError("effect intent identity mismatch")
        self._require_event(intent, kind, "intent")
        identity_key = "artifact_id" if kind == "deletion" else "episode_id"
        if not intent.fields.get(identity_key):
            raise ValueError("effect intent lacks bound retained identity")
        with self._transaction():
            event_id = self._insert_required_event(intent)
            self._db.execute(
                "INSERT INTO effect_intents VALUES (?, ?, 'pending', 'unknown', ?, NULL)",
                (_id(operation_id), kind, event_id),
            )

    def record_effect_result(self, operation_id, effect_status, result):
        """Persist verified result; failed commit leaves the original pending."""
        if effect_status not in ("verified_removed", "verified_present", "unknown", "failed"):
            raise ValueError("invalid effect status")
        operation_id = _id(operation_id)
        if result.operation_id != operation_id:
            raise ValueError("effect result identity mismatch")
        with self._transaction():
            row = self._db.execute(
                "SELECT kind, intent_event_id FROM effect_intents WHERE operation_id = ? AND status = 'pending'",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("effect intent is not pending")
            self._require_event(result, row[0], "result")
            self._check_effect_binding(row[0], row[1], result, effect_status)
            event_id = self._insert_required_event(result)
            status = "pending" if effect_status == "unknown" else "resolved"
            self._db.execute(
                "UPDATE effect_intents SET status = ?, effect_status = ?, resolution_event_id = ? "
                "WHERE operation_id = ? AND status = 'pending'",
                (status, effect_status, event_id, operation_id),
            )

    def reconcile_effect(self, operation_id, observed_state, observation):
        """Save a caller's current inspection, never replaying its effect."""
        if observed_state not in ("observed_absent", "observed_present", "ambiguous"):
            raise ValueError("invalid reconciliation observation")
        operation_id = _id(operation_id)
        if observation.operation_id != operation_id:
            raise ValueError("reconciliation identity mismatch")
        with self._transaction():
            row = self._db.execute(
                "SELECT kind, intent_event_id FROM effect_intents WHERE operation_id = ? AND status = 'pending'",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("effect intent is not pending")
            self._require_event(observation, row[0], "reconciliation")
            self._check_effect_binding(row[0], row[1], observation, observed_state)
            event_id = self._insert_required_event(observation)
            status = "pending" if observed_state == "ambiguous" else "resolved"
            self._db.execute(
                "UPDATE effect_intents SET status = ?, effect_status = ?, resolution_event_id = ? "
                "WHERE operation_id = ? AND status = 'pending'",
                (status, observed_state, event_id, operation_id),
            )

    def _check_effect_binding(self, kind, intent_event_id, event, outcome):
        identity_key = "artifact_id" if kind == "deletion" else "episode_id"
        row = self._db.execute("SELECT fields_json FROM operation_events WHERE event_id = ?",
                               (intent_event_id,)).fetchone()
        if row is None:
            raise StoreIntegrityError("effect intent record unavailable")
        original = _read_json_object(row[0])
        if (event.fields.get(identity_key) != original.get(identity_key)
                or event.fields.get("outcome") != outcome):
            raise ValueError("effect outcome does not match bound intent and observation")

    def pending_effects(self):
        rows = self._db.execute(
            "SELECT operation_id, kind, effect_status, intent_event_id FROM effect_intents "
            "WHERE status = 'pending' ORDER BY operation_id"
        )
        return [{"id": op_id, "kind": kind, "effect_status": effect_status,
                 "intent_event_id": intent_id} for op_id, kind, effect_status, intent_id in rows]

    def append_diagnostic(self, event, *, max_records=1000, max_bytes=1024 * 1024):
        if not isinstance(event, OperationEvent) or event.duty != "diagnostic":
            raise ValueError("diagnostic event expected")
        self._require_event(event, event.kind, "result")
        if (type(max_records) is not int or max_records < 1 or
                type(max_bytes) is not int or max_bytes < 256):
            raise ValueError("invalid diagnostic bound")
        encoded = _json_object({"kind": event.kind, "operation_id": event.operation_id,
                                "phase": event.phase, "component": event.component,
                                "fields": event.fields, "context": event.context})
        byte_length = len(encoded.encode("utf-8"))
        if byte_length > max_bytes:
            raise ValueError("diagnostic event exceeds storage bound")
        with self._transaction():
            self._db.execute("INSERT INTO diagnostic_events(event_id, observed_at, event_json, byte_length) "
                             "VALUES (?, ?, ?, ?)",
                             (event.event_id, event.observed_at, encoded, byte_length))
            while True:
                count, size = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(byte_length), 0) FROM diagnostic_events"
                ).fetchone()
                if count <= max_records and size <= max_bytes:
                    break
                self._db.execute("DELETE FROM diagnostic_events WHERE sequence = "
                                 "(SELECT MIN(sequence) FROM diagnostic_events)")

    def operation_usage(self):
        """Measure current storage, keeping required and diagnostic distinct."""
        required = self._db.execute("SELECT COUNT(*) FROM operation_events").fetchone()[0]
        diagnostic, diagnostic_bytes = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(byte_length), 0) FROM diagnostic_events"
        ).fetchone()
        pages = self._db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        object_bytes = sum(path.stat().st_size for path in (self.root / "objects").iterdir()
                           if path.is_file() and not path.is_symlink())
        return {"required_records": required, "diagnostic_records": diagnostic,
                "diagnostic_payload_bytes": diagnostic_bytes,
                "database_bytes": pages * page_size, "object_bytes": object_bytes}

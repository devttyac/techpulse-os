"""TP02 durable local state primitives for later TechPulse components.

This module does not migrate legacy data or store credentials. Callers must
validate evidence and protect secrets before using later integrations.
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


_SCHEMA_VERSION = 1
_APPLICATION_ID = 0x54504F53
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TABLES = frozenset({
    "artifacts", "episodes", "episode_artifacts", "coverage",
    "control_revisions", "operation_journal",
})


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
            if self._db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise StoreIntegrityError("retained database failed integrity check")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            app_id = self._db.execute("PRAGMA application_id").fetchone()[0]
            tables = {row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if version != _SCHEMA_VERSION or app_id != _APPLICATION_ID or not _TABLES <= tables:
                raise StoreIntegrityError("retained schema is missing or incompatible")
        except sqlite3.Error as exc:
            raise StoreIntegrityError("retained database failed integrity check") from exc

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
            self._db.execute("INSERT INTO episodes VALUES (?, ?)", (episode_id, encoded))
            self._db.executemany(
                "INSERT INTO episode_artifacts VALUES (?, ?, ?)",
                ((episode_id, item, index) for index, item in enumerate(ids)),
            )
            self._db.executemany(
                "INSERT INTO coverage VALUES (?, ?)",
                ((key, episode_id) for key in keys),
            )

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

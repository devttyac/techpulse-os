"""Fail-closed retention of explicitly managed episode media.

Legacy files have no proven ownership binding and are never unlinked here.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sqlite3
from urllib.parse import quote

from src.state_store import StateStore, StoreIntegrityError


def measure_storage(root):
    """Measure physical retained files without following unknown links."""
    root = Path(root)
    result = {"total_episodes": 0, "total_audio": 0, "disk_usage_bytes": 0,
              "bytes_by_class": {"episodes": 0, "audio": 0, "evidence": 0,
                                 "staging": 0, "audit_control": 0, "other": 0}}
    if root.is_symlink() or not root.is_dir():
        raise StoreIntegrityError("storage root is unavailable")
    object_kinds = {}
    database = root / "state" / "state.sqlite3"
    if database.exists():
        if database.is_symlink() or not database.is_file():
            raise StoreIntegrityError("retained index is unavailable")
        try:
            db = sqlite3.connect(f"file:{quote(str(database), safe='/')}?mode=ro", uri=True)
            try:
                for digest, kind in db.execute("SELECT content_sha256,kind FROM artifacts"):
                    object_kinds.setdefault(digest, set()).add(kind)
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise StoreIntegrityError("retained index is unreadable") from exc
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise StoreIntegrityError("storage contains an unresolved link")
        for name in files:
            path = Path(current) / name
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise StoreIntegrityError("storage contains a non-file")
            relative = path.relative_to(root).parts
            if relative[0] == "episodes":
                category = "episodes"
                if len(relative) == 2 and name.startswith("ep-") and name.endswith(".json"):
                    result["total_episodes"] += 1
            elif relative[0] == "audio":
                category = "audio"
                if len(relative) == 2 and name.endswith(".mp3"):
                    result["total_audio"] += 1
            elif relative[:2] == ("state", "objects"):
                kinds = object_kinds.get(name, set())
                category = "audio" if kinds == {"main_audio"} else "evidence"
                if category == "audio":
                    result["total_audio"] += 1
            elif relative[:2] == ("state", "staging"):
                category = "staging"
            elif name.endswith(".jsonl") or relative[0] == "state":
                category = "audit_control"
            else:
                category = "other"
            result["bytes_by_class"][category] += info.st_size
            result["disk_usage_bytes"] += info.st_size
    result["disk_usage_mb"] = round(result["disk_usage_bytes"] / (1024 * 1024), 2)
    return result


class LifecycleService:
    def __init__(self, state_root):
        self.state_root = Path(state_root)

    @contextmanager
    def _locked(self):
        if self.state_root.is_symlink() or not self.state_root.is_dir():
            raise StoreIntegrityError("retained lifecycle root unavailable")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.state_root / "lifecycle.lock", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StoreIntegrityError("lifecycle lock is not a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _before_unlink(path):
        """No-op seam for the final identity recheck in adverse tests."""

    @staticmethod
    def _inspect(path, tombstone):
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"status": "observed_absent"}
        except OSError:
            return {"status": "ambiguous"}
        try:
            info = os.fstat(descriptor)
            inspected = {"file_device": info.st_dev, "file_inode": info.st_ino,
                         "file_mtime_ns": info.st_mtime_ns}
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) !=
                       (tombstone["device"], tombstone["inode"], tombstone["mtime_ns"], tombstone["bytes"])):
                return {"status": "ambiguous", **inspected}
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {"status": "observed_present" if digest.hexdigest() == tombstone["digest"] else "ambiguous",
                    **inspected}
        except OSError:
            return {"status": "ambiguous"}
        finally:
            os.close(descriptor)

    def retire_to_limit(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("retention limit must be a positive integer")
        result = {"removed": 0, "blocked": 0, "pending": 0, "freed_bytes": 0,
                  "observed_removed": 0, "observed_freed_bytes": 0,
                  "managed_active": 0}
        with self._locked(), StateStore.open(self.state_root) as store:
            pending = [row for row in store.pending_effects() if row["kind"] == "deletion"]
            if pending:
                result["pending"] = len(pending)
                result["managed_active"] = len(store.active_episode_ids())
                return result
            active = store.active_episode_ids()
            result["managed_active"] = len(active)
            for episode_id in active[:-limit]:
                try:
                    tombstone = store.plan_lifecycle_retirement(episode_id)
                except (StoreIntegrityError, ValueError, OSError):
                    result["blocked"] += 1
                    break
                path = store.artifact_file(tombstone["artifact_id"])
                self._before_unlink(path)
                try:
                    # A short SQLite write lock prevents a new managed lease
                    # between the last lease check and the physical effect.
                    with store._transaction():
                        if (not store.lifecycle_effect_eligible(tombstone)
                                or self._inspect(path, tombstone)["status"] != "observed_present"):
                            result["pending"] += 1
                            break
                        path.unlink()
                    observed = self._inspect(path, tombstone)
                    if observed["status"] != "observed_absent":
                        result["pending"] += 1
                        break
                    result["observed_removed"] += 1
                    result["observed_freed_bytes"] += tombstone["bytes"]
                    store.record_lifecycle_result(tombstone, "verified_removed")
                except (OSError, sqlite3.Error, StoreIntegrityError, ValueError):
                    result["pending"] += 1
                    break
                result["removed"] += 1
                result["freed_bytes"] += tombstone["bytes"]
            result["managed_active"] = len(store.active_episode_ids())
        return result

    def reconcile_pending(self):
        result = {"observed_absent": 0, "observed_present": 0, "ambiguous": 0,
                  "pending": 0}
        with self._locked(), StateStore.open(self.state_root) as store:
            for tombstone in store.pending_lifecycle():
                path = store.artifact_file(tombstone["artifact_id"])
                try:
                    observation = store.record_lifecycle_observation(
                        tombstone, lambda: self._inspect(path, tombstone))
                except (OSError, sqlite3.Error, StoreIntegrityError, ValueError):
                    result["pending"] += 1
                    continue
                result[observation] += 1
        return result

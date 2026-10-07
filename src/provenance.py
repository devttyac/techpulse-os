"""Bounded operational records, separate from article content and provider logs."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

DOMAIN_ORDER = ("ai", "cloud", "data", "sec", "devops", "arch", "finops", "gov")
STAGES = ("ingestion", "dedup", "synthesis", "podcast_audio", "domain_audio", "episode_write", "retention")
_APPEND_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def model_identifier(value):
    # Never copy arbitrary environment contents or request URLs into the log.
    return value if isinstance(value, str) and not contains_secret(value) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) else "unrecognized_model"


def contains_secret(value):
    return any(secret and secret in value for secret in
               (os.getenv("GEMINI_API_KEY", "").strip(), os.getenv("API_SECRET_KEY", "").strip()))


def error_category(exc):
    """Classification inspects errors in memory; it never serializes their prose."""
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "transport"
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    if code in (401, 403) or "unauthorized" in text or "invalid api key" in text:
        return "auth"
    if code == 429 or "quota" in text or "resource_exhausted" in text:
        return "quota"
    if "timeout" in text or "timed out" in text or "connection" in text:
        return "transport"
    return "unexpected_error"


def article_identity(url):
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname
        if ":" in host:
            host = "[" + host + "]"
        if parts.port:
            host += ":" + str(parts.port)
        clean = urlunsplit((parts.scheme, host, parts.path, "", ""))[:2048]
        if contains_secret(clean):
            clean = None
        return {"url": clean, "url_sha256": hashlib.sha256(url.encode()).hexdigest()}
    except ValueError:
        return None


def selection_record(corpus, episode, path, *, provider_attempted=False):
    result = {}
    chapters = episode.get("chapters", []) if episode else []
    for i, domain in enumerate(DOMAIN_ORDER):
        articles = corpus.get(domain, [])
        chapter = next((c for c in chapters if isinstance(c, dict) and c.get("domain") == domain), None)
        if chapter is None and len(chapters) == 8 and not episode.get("content_availability") and all(isinstance(c, dict) and "domain" not in c for c in chapters):
            chapter = chapters[i]
        chapter_url = chapter.get("source_url") if chapter else None
        result[domain] = {
            "candidate_count": len(articles),
            "reason": "no_candidates" if not articles else ("position_0" if path == "deterministic_fallback" else "feed_order_prefix_then_model_choice"),
            "primary_candidate": article_identity(articles[0].get("url")) if articles else None,
            "prompt_articles": [article_identity(a.get("url")) for a in articles[:3]] if path == "llm" or provider_attempted else [],
            "context_articles": [article_identity(a.get("url")) for a in articles[:4]],
            "chapter_citation": article_identity(chapter_url),
        }
    return result


class RunRecord:
    def __init__(self, trigger="scheduled"):
        self.started = time.monotonic()
        self.finalized = False
        self.append_attempted = False
        self.data = {
            "schema_version": 1, "run_id": str(uuid.uuid4()), "trigger": trigger,
            "started_at": utc_now(), "finished_at": None, "total_duration_ms": None,
            "status": "running", "episode_id": None, "error_category": None,
            "stages": {name: {"status": "not_started", "duration_ms": None} for name in STAGES},
            "ingestion": {"feeds": [], "per_domain": {}, "feeds_failed": 0, "links_rejected": 0},
            "synthesis": {"path": None, "model": None, "schema_variant": None, "fallback_reason": None, "attempts": [], "url_substitutions": 0},
            "selection": {},
        }

    @contextmanager
    def stage(self, name, returned_status="completed"):
        stage = self.data["stages"][name]
        started = time.monotonic()
        stage["status"] = "running"
        try:
            yield stage
        except BaseException as exc:
            stage["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            stage["error_category"] = error_category(exc)
            raise
        else:
            stage["status"] = returned_status
        finally:
            stage["duration_ms"] = round((time.monotonic() - started) * 1000, 3)

    def finish(self, status, category=None):
        if self.finalized:
            return
        self.finalized = True
        self.data.update(status=status, error_category=category, finished_at=utc_now(),
                         total_duration_ms=round((time.monotonic() - self.started) * 1000, 3))


def append_record(storage_dir, filename, record):
    if filename not in ("runs.jsonl", "audit.jsonl"):
        raise ValueError("Unknown audit stream")
    # Producers construct allowlisted records; no exception object or arbitrary
    # input mapping crosses this boundary. One worker uses one serialized append.
    blob = (json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(blob) > 256 * 1024:
        raise ValueError("Audit record exceeds limit")
    with _APPEND_LOCK:
        fd = os.open(Path(storage_dir) / filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            view = memoryview(blob)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Audit append failed")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


def attach_episode_record(path, episode, record):
    episode["pipeline_run"] = record
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=Path(path).parent, delete=False) as fp:
            temporary = fp.name
            json.dump(episode, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def action_record(action, outcome, *, run_id=None, changed_fields=()):
    allowed_actions = {"refresh", "refresh_cancel", "refresh_reset", "settings_update", "settings_cleanup"}
    allowed_outcomes = {"request_accepted", "no_active_task", "busy", "handler_returned", "handler_failed"}
    allowed_fields = {"max_episodes_retained", "chapters_per_episode", "gemini_model", "cron_schedule"}
    if action not in allowed_actions or outcome not in allowed_outcomes:
        raise ValueError("Unknown audit action")
    return {"schema_version": 1, "event_id": str(uuid.uuid4()), "timestamp": utc_now(),
            "action": action, "outcome": outcome, "run_id": run_id,
            "changed_fields": sorted(set(changed_fields) & allowed_fields)}

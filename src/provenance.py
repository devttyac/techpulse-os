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


def discovery_row_consistent(outcome, candidates, sponsor_excluded, placement_unresolved,
                             received=None, rejected=None):
    """Reject impossible discovery claims before status reduction or recovery."""
    counts = (candidates, sponsor_excluded, placement_unresolved)
    if any(type(count) is not int or count < 0 or count > 500 for count in counts):
        return False
    if received is not None or rejected is not None:
        if (type(received) is not int or type(rejected) is not int or
                not (0 <= received <= 500 and 0 <= rejected <= received) or
                sponsor_excluded + placement_unresolved > rejected or
                candidates + rejected > received):
            return False
    if outcome in ('disabled', 'not_checked', 'parse_error'):
        return not any(counts) and (received in (None, 0)) and (rejected in (None, 0))
    if outcome == 'valid_empty':
        return candidates == 0 and placement_unresolved == 0
    if outcome == 'candidate':
        return candidates > 0 and placement_unresolved == 0
    if outcome == 'candidate_partial':
        return candidates > 0 and placement_unresolved > 0
    if outcome == 'placement_unresolved':
        return candidates == 0 and placement_unresolved > 0
    return False


def expected_discovery_status(rows):
    outcomes = [row['outcome'] for row in rows]
    if outcomes and all(outcome == 'disabled' for outcome in outcomes):
        return 'disabled'
    if outcomes and all(outcome == 'parse_error' for outcome in outcomes):
        return 'failed'
    if any(outcome in ('not_checked', 'parse_error', 'candidate_partial',
                       'placement_unresolved', 'disabled') for outcome in outcomes):
        return 'partial'
    return 'fixture_only'


def discovery_issue_identity_consistent(edition_id, outcome, issue_id, issue_source):
    """Use the fixture parser's exact issue route for checked discoveries."""
    from src.acquisition.tldr import _issue_ok
    if outcome in ('disabled', 'not_checked', 'parse_error'):
        return issue_id is None and issue_source is None
    if not isinstance(issue_source, (str, dict)):
        return False
    url = issue_source if isinstance(issue_source, str) else issue_source.get('url')
    if not _issue_ok({'edition_id': edition_id, 'issue_id': issue_id, 'issue_url': url}, edition_id):
        return False
    return isinstance(issue_source, str) or issue_source == article_identity(url)


def discovery_record(result):
    """Keep bounded discovery diagnostics, never newsletter text or candidate URLs."""
    from src.acquisition.catalogue import CATALOGUE_REVISION, EDITION_IDS
    value = result if isinstance(result, dict) else {}
    allowed_status = {'disabled', 'fixture_only', 'partial', 'failed'}
    allowed_outcome = {'disabled', 'candidate', 'candidate_partial', 'valid_empty',
                       'placement_unresolved', 'parse_error', 'not_checked'}
    rows = value.get('editions') if isinstance(value.get('editions'), list) else []
    by_id = {row.get('edition_id'): row for row in rows
             if isinstance(row, dict) and isinstance(row.get('edition_id'), str) and row['edition_id'] in EDITION_IDS}
    malformed_rows = len(rows) != len(EDITION_IDS) or [row.get('edition_id') if isinstance(row, dict) else None for row in rows] != list(EDITION_IDS)
    editions = []
    invalid = (malformed_rows or type(value.get('schema_version')) is not int or
               value.get('schema_version') != 1 or
               type(value.get('catalogue_revision')) is not int or
               value.get('catalogue_revision') != CATALOGUE_REVISION)
    for selected in EDITION_IDS:
        row = by_id.get(selected, {})
        issue_id = row.get('issue_id')
        outcome = row.get('outcome')
        counts_valid = (type(row.get('received')) is int and type(row.get('rejected')) is int and
            discovery_row_consistent(outcome, row.get('candidates'),
            row.get('sponsor_excluded'), row.get('placement_unresolved'),
            row.get('received'), row.get('rejected')))
        if not counts_valid:
            invalid = True
        identity_valid = discovery_issue_identity_consistent(selected, outcome, issue_id, row.get('issue_url'))
        if not identity_valid:
            invalid = True
        editions.append({
            'edition_id': selected,
            'outcome': outcome if counts_valid and identity_valid and outcome in allowed_outcome else 'not_checked',
            'reason': 'inconsistent_outcome_counts' if not counts_valid else 'invalid_issue_identity' if not identity_valid else None,
            'issue_id': issue_id if isinstance(issue_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', issue_id) else None,
            'issue_source': article_identity(row.get('issue_url')),
            'received': row['received'] if counts_valid else 0,
            'rejected': row['rejected'] if counts_valid else 0,
            'sponsor_excluded': row['sponsor_excluded'] if counts_valid else 0,
            'placement_unresolved': row['placement_unresolved'] if counts_valid else 0,
            'candidates': row['candidates'] if counts_valid else 0,
        })
    candidates = value.get('candidates')
    candidate_count = min(3000, len(candidates)) if isinstance(candidates, list) else 0
    recorded_candidates = sum(row['candidates'] for row in editions)
    if candidate_count > recorded_candidates or bool(candidate_count) != bool(recorded_candidates):
        invalid = True
    status = value.get('status')
    if not isinstance(status, str) or status not in allowed_status or status != expected_discovery_status(editions):
        invalid = True
    return {
        'catalogue_revision': CATALOGUE_REVISION,
        'status': ('failed' if not isinstance(status, str) or status not in allowed_status or status == 'disabled' else 'partial') if invalid else status,
        'reason': 'invalid_discovery_state' if invalid else 'tldr_interface_and_discovery_use_unverified' if status == 'disabled' else None,
        'candidate_count': candidate_count if not invalid else 0,
        'editions': editions,
    }


def selection_record(corpus, episode, path, *, provider_attempted=False):
    if isinstance(episode, dict) and 'story_manifest' in episode:
        from src.story_manifest import manifest_from_dict, validate_story_episode
        if not validate_story_episode(episode)[0]:
            raise ValueError('Invalid source-bound episode provenance')
        manifest = manifest_from_dict(episode['story_manifest'])
        return {domain: {
            'candidate_count': len(corpus.get(domain, [])),
            'reason': 'no_candidates' if not any(s.domain == domain for s in manifest.stories)
                      else 'received_prefix_exact_url_dedup',
            'stories': [
                {'story_id': s.story_id, 'article_id': s.article_id,
                 'evidence_digest': s.evidence_digest,
                 'selected_unit_ids': list(s.selected_unit_ids),
                 'evidence_basis': 'rss_excerpts' if any(
                     u.field == 'summary' and u.unit_id in s.selected_unit_ids for u in s.units) else 'headline_only',
                 'chapter_citation': article_identity(s.source_url),
                 'context_article': article_identity(s.source_url)}
                for s in manifest.stories if s.domain == domain],
        } for domain in DOMAIN_ORDER}
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


def read_operation_history(storage_dir, store, operation_id, *, max_legacy_records=100000):
    """Bridge retained JSONL to v2 readers without rewriting or upgrading it.

    Legacy entries are hash-addressed historical references only. Their old
    action names and outcomes cannot satisfy a v2 required effect/result gate.
    """
    from src.state_store import StoreIntegrityError

    current = store.operation_events(operation_id)
    legacy = []
    for filename in ("runs.jsonl", "audit.jsonl"):
        path = Path(storage_dir) / filename
        if path.is_symlink():
            raise StoreIntegrityError("legacy operation stream is not a regular file")
        if not path.exists():
            continue
        if not path.is_file():
            raise StoreIntegrityError("legacy operation stream is not a regular file")
        with path.open("rb") as fp:
            for line_number, raw in enumerate(fp, 1):
                if line_number > max_legacy_records or len(raw) > 256 * 1024 or not raw.endswith(b"\n"):
                    raise StoreIntegrityError("legacy operation stream exceeds safe read bound")
                try:
                    value = json.loads(raw)
                except (UnicodeError, ValueError) as exc:
                    raise StoreIntegrityError("legacy operation stream contains invalid JSON") from exc
                if not isinstance(value, dict):
                    raise StoreIntegrityError("legacy operation stream contains invalid record")
                if operation_id in (value.get("run_id"), value.get("event_id")):
                    # Only expose a digest and location: older records may
                    # contain unrestricted prose or URLs. No success inferred.
                    legacy.append({"stream": filename, "line": line_number,
                                   "sha256": hashlib.sha256(raw).hexdigest(),
                                   "classification": "historical_unverified"})
    return {"current_required": current, "legacy_references": legacy}


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

"""Captured RSS coverage; absence is not a claim that a domain has no news."""
DOMAIN_ORDER = ('ai', 'cloud', 'data', 'sec', 'devops', 'arch', 'finops', 'gov')
SOURCE_FAILURES = frozenset({'http_error', 'failed', 'parse_error'})


def build_content_availability(corpus, diagnostics=None):
    """Keep eight status panels independently of the active content count."""
    corpus = corpus if isinstance(corpus, dict) else {}
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    feeds = diagnostics.get('feeds', [])
    feeds = feeds if isinstance(feeds, list) else []
    domains = {}
    for domain in DOMAIN_ORDER:
        articles = corpus.get(domain) or []
        count = len(articles) if isinstance(articles, list) else 0
        checks = [f for f in feeds if isinstance(f, dict) and f.get('domain') == domain]
        if count:
            status, reason = 'available', 'Articles available from received RSS candidates.'
        elif checks and all(f.get('outcome') in SOURCE_FAILURES for f in checks):
            status, reason = 'source_unavailable', 'Checked sources were unavailable; no articles received.'
        else:
            status, reason = 'no_received_candidates', 'No articles available from checked sources.'
        domains[domain] = {'status': status, 'reason': reason, 'candidate_count': count}
    return {'schema_version': 1, 'domains': domains}


def active_domains(availability):
    """Return ordered IDs only for a valid captured coverage contract."""
    if not isinstance(availability, dict) or type(availability.get('schema_version')) is not int or availability['schema_version'] != 1:
        return []
    states = availability.get('domains')
    if not isinstance(states, dict) or set(states) != set(DOMAIN_ORDER):
        return []
    for state in states.values():
        if not isinstance(state, dict) or state.get('status') not in ('available', 'no_received_candidates', 'source_unavailable'):
            return []
        count = state.get('candidate_count')
        if type(count) is not int or count < 0 or not isinstance(state.get('reason'), str):
            return []
        if (state['status'] == 'available') != (count > 0):
            return []
    return [d for d in DOMAIN_ORDER if states[d]['status'] == 'available']



# Health is a bounded operational snapshot, independent of episode publication.
# Source names always come from the configured catalogue, never remote content.
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime

MAX_HEALTH_BYTES = 64 * 1024
HEALTH_STATUSES = frozenset({'not_checked', 'healthy', 'partial', 'unavailable', 'cancelled', 'unknown'})
RUN_STATUSES = frozenset({'no_content', 'completed', 'failed', 'cancelled', 'skipped'})
SOURCE_OUTCOMES = frozenset({'available', 'quiet', 'unavailable', 'not_checked'})


def _catalogue():
    from src.ingestion import DOMAIN_FEEDS
    return [(f'{domain}:{i}', domain, feed['name']) for domain, feeds in DOMAIN_FEEDS.items()
            for i, feed in enumerate(feeds)]


def _count(value):
    return min(5, max(0, value)) if type(value) is int else 0


def _identifier(value):
    from src.provenance import contains_secret
    return value if isinstance(value, str) and not contains_secret(value) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed.tzinfo is not None else None
    except ValueError:
        return None


def empty_source_health(status='not_checked'):
    return {'schema_version': 1, 'check_id': None, 'run_id': None, 'checked_at': None,
            'status': status if status in HEALTH_STATUSES else 'unknown',
            'available_sources': 0, 'total_sources': len(_catalogue()),
            'body_acquisition': 'not_checked',
            'sources': [{'name': name, 'domain': domain, 'outcome': 'not_checked',
                         'accepted': 0, 'parse_warning': False} for _, domain, name in _catalogue()],
            'latest_run': None, 'last_attempt_at': None, 'last_completed_at': None}


def _safe_sources(values):
    values = values if isinstance(values, list) else []
    sources = []
    # Recovery uses fixed ordering and names. Missing or mismatched entries are
    # unknown rather than inheriting arbitrary fields from a stored JSON object.
    for i, (_, domain, name) in enumerate(_catalogue()):
        value = values[i] if i < len(values) and isinstance(values[i], dict) else {}
        if value.get('domain') != domain:
            value = {}
        outcome = value.get('outcome')
        sources.append({'name': name, 'domain': domain,
                        'outcome': outcome if outcome in SOURCE_OUTCOMES else 'not_checked',
                        'accepted': _count(value.get('accepted')),
                        'parse_warning': value.get('parse_warning') is True})
    return sources


def sanitize_source_health(value):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 1:
        return empty_source_health('unknown')
    result = empty_source_health('unknown')
    result.update(check_id=_identifier(value.get('check_id')), run_id=_identifier(value.get('run_id')),
                  checked_at=_timestamp(value.get('checked_at')),
                  status=value.get('status') if value.get('status') in HEALTH_STATUSES else 'unknown',
                  sources=_safe_sources(value.get('sources')),
                  last_attempt_at=_timestamp(value.get('last_attempt_at')),
                  last_completed_at=_timestamp(value.get('last_completed_at')))
    result['available_sources'] = sum(s['outcome'] in ('available', 'quiet') for s in result['sources'])
    # Incomplete or inconsistent recovered source detail cannot claim health.
    completed = result['available_sources']
    failed = sum(s['outcome'] == 'unavailable' for s in result['sources'])
    total = result['total_sources']
    if ((result['status'] == 'healthy' and completed != total)
            or (result['status'] == 'unavailable' and failed != total)
            or (result['status'] == 'partial' and not (completed or failed))
            or (result['status'] in ('healthy', 'partial', 'unavailable') and not result['checked_at'])):
        result['status'] = 'unknown'
    latest = value.get('latest_run')
    if isinstance(latest, dict) and latest.get('status') in RUN_STATUSES:
        result['latest_run'] = {'status': latest['status'], 'finished_at': _timestamp(latest.get('finished_at')),
                                'episode_id': _identifier(latest.get('episode_id'))}
    attempt = value.get('last_attempt')
    if isinstance(attempt, dict):
        sources = _safe_sources(attempt.get('sources'))
        result['last_attempt'] = {'run_id': _identifier(attempt.get('run_id')),
                                  'checked_at': _timestamp(attempt.get('checked_at')),
                                  'status': attempt.get('status') if attempt.get('status') in HEALTH_STATUSES else 'unknown',
                                  'available_sources': sum(s['outcome'] in ('available', 'quiet') for s in sources),
                                  'sources': sources}
    return result


def build_source_health(diagnostics, run_id, checked_at):
    result = empty_source_health()
    feeds = diagnostics.get('feeds', []) if isinstance(diagnostics, dict) else []
    feeds = feeds if isinstance(feeds, list) else []
    by_id = {feed.get('feed_id'): feed for feed in feeds if isinstance(feed, dict)
             and isinstance(feed.get('feed_id'), str)}
    for source, (feed_id, _, _) in zip(result['sources'], _catalogue()):
        feed = by_id.get(feed_id, {})
        outcome = feed.get('outcome')
        accepted = _count(feed.get('accepted'))
        source.update(accepted=accepted, parse_warning=feed.get('parse_warning') is True)
        if outcome == 'success':
            source['outcome'] = 'available' if accepted else 'quiet'
        elif outcome in SOURCE_FAILURES:
            source['outcome'] = 'unavailable'
    available = sum(s['outcome'] in ('available', 'quiet') for s in result['sources'])
    unavailable = sum(s['outcome'] == 'unavailable' for s in result['sources'])
    total = len(result['sources'])
    status = ('healthy' if available == total else 'unavailable' if unavailable == total
              else 'partial' if available or unavailable else 'unknown')
    result.update(check_id=_identifier(run_id), run_id=_identifier(run_id), checked_at=_timestamp(checked_at),
                  status=status, available_sources=available,
                  last_attempt_at=_timestamp(checked_at), last_completed_at=_timestamp(checked_at))
    return result


def load_source_health(storage_dir):
    path = Path(storage_dir) / 'latest_source_health.json'
    try:
        with path.open('rb') as fp:
            blob = fp.read(MAX_HEALTH_BYTES + 1)
        if len(blob) > MAX_HEALTH_BYTES:
            return empty_source_health('unknown')
        return sanitize_source_health(json.loads(blob))
    except FileNotFoundError:
        return empty_source_health()
    except (OSError, ValueError, TypeError, RecursionError):
        return empty_source_health('unknown')


def save_source_health(storage_dir, snapshot):
    blob = json.dumps(sanitize_source_health(snapshot), separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(blob) > MAX_HEALTH_BYTES:
        raise ValueError('Source snapshot exceeds limit')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=storage_dir, prefix='.source-health-', delete=False) as fp:
            temporary = fp.name
            os.chmod(temporary, 0o600)
            fp.write(blob)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temporary, Path(storage_dir) / 'latest_source_health.json')
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)

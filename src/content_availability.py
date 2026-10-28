"""Captured RSS coverage; absence is not a claim that a domain has no news."""
DOMAIN_ORDER = ('ai', 'cloud', 'data', 'sec', 'devops', 'arch', 'finops', 'gov')
SOURCE_FAILURES = frozenset({'http_error', 'failed', 'parse_error'})

# Historical v1 source-health identities only. No URL or fetch path is retained.
LEGACY_SOURCE_NAMES = {
    'ai': ('Simon Willison (AI Architecture)', 'OpenAI News', 'Hugging Face'),
    'cloud': ('Microsoft Azure Architecture', 'AWS Architecture Blog', 'Google Cloud Blog'),
    'data': ('Microsoft Fabric Blog', 'Databricks Blog'),
    'sec': ('Cloudflare Engineering', 'CNCF Security', 'Krebs on Security'),
    'devops': ('Kubernetes Official Blog', 'SRE Weekly'),
    'arch': ('Martin Fowler', 'InfoQ Architecture'),
    'finops': ('FinOps Foundation', 'AWS Compute Blog'),
    'gov': ('NIST AI & Cybersecurity',),
}


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
HEALTH_STATUSES = frozenset({'not_checked', 'healthy', 'partial', 'unavailable', 'cancelled', 'unknown', 'disabled'})
LEGACY_HEALTH_STATUSES = HEALTH_STATUSES - {'disabled'}
RUN_STATUSES = frozenset({'no_content', 'completed', 'failed', 'cancelled', 'skipped'})
SOURCE_OUTCOMES = frozenset({'available', 'quiet', 'unavailable', 'not_checked'})


def _catalogue():
    return [(f'{domain}:{i}', domain, name) for domain, names in LEGACY_SOURCE_NAMES.items()
            for i, name in enumerate(names)]


def _count(value):
    return min(5, max(0, value)) if type(value) is int else 0


def _discovery_count(value):
    return min(500, max(0, value)) if type(value) is int else 0


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
    from src.acquisition.catalogue import CATALOGUE_REVISION, catalogue
    sources = catalogue()
    return {'schema_version': 2, 'catalogue_revision': CATALOGUE_REVISION,
            'check_id': None, 'run_id': None, 'checked_at': None,
            'status': status if status in HEALTH_STATUSES else 'unknown',
            'reason': None,
            'available_sources': 0, 'total_sources': len(sources),
            'body_acquisition': 'not_checked',
            'sources': [{'name': source['name'], 'edition_id': source['id'],
                         'outcome': 'not_checked', 'accepted': 0,
                         'issue_id': None, 'issue_source': None,
                         'sponsor_excluded': 0, 'placement_unresolved': 0,
                         'reason': None,
                         'parse_warning': False}
                        for source in sources],
            'latest_run': None, 'last_attempt_at': None, 'last_completed_at': None}


def _empty_legacy_source_health(status='not_checked'):
    return {'schema_version': 1, 'check_id': None, 'run_id': None, 'checked_at': None,
            'status': status if status in LEGACY_HEALTH_STATUSES else 'unknown',
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
    if isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 2:
        return _sanitize_discovery_health(value)
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') != 1:
        return empty_source_health('unknown')
    result = _empty_legacy_source_health('unknown')
    result.update(check_id=_identifier(value.get('check_id')), run_id=_identifier(value.get('run_id')),
                  checked_at=_timestamp(value.get('checked_at')),
                  status=value.get('status') if isinstance(value.get('status'), str) and value['status'] in LEGACY_HEALTH_STATUSES else 'unknown',
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


def _sanitize_discovery_health(value):
    from src.acquisition.catalogue import CATALOGUE_REVISION, EDITION_IDS
    from src.provenance import discovery_row_consistent, discovery_issue_identity_consistent
    result = empty_source_health('unknown')
    if value.get('catalogue_revision') != CATALOGUE_REVISION:
        return result
    rows = value.get('sources')
    if not isinstance(rows, list) or len(rows) != len(EDITION_IDS):
        return result
    allowed = {'disabled', 'candidate', 'candidate_partial', 'valid_empty',
               'placement_unresolved', 'parse_error', 'not_checked'}
    for index, (source, selected) in enumerate(zip(result['sources'], EDITION_IDS)):
        row = rows[index]
        if not isinstance(row, dict) or row.get('edition_id') != selected or not isinstance(row.get('outcome'), str) or row['outcome'] not in allowed:
            return result
        if not discovery_row_consistent(row['outcome'], row.get('accepted'),
                                        row.get('sponsor_excluded'), row.get('placement_unresolved')):
            result['reason'] = 'inconsistent_outcome_counts'
            return result
        if not discovery_issue_identity_consistent(selected, row['outcome'],
                                                    row.get('issue_id'), row.get('issue_source')):
            result['reason'] = 'invalid_issue_identity'
            return result
        source['outcome'] = row['outcome']
        source['issue_id'] = row.get('issue_id')
        source['issue_source'] = row.get('issue_source')
        source['accepted'] = _discovery_count(row.get('accepted'))
        source['sponsor_excluded'] = _discovery_count(row.get('sponsor_excluded'))
        source['placement_unresolved'] = _discovery_count(row.get('placement_unresolved'))
        source['reason'] = row.get('reason') if row.get('reason') == 'inconsistent_outcome_counts' else None
    result.update(check_id=_identifier(value.get('check_id')), run_id=_identifier(value.get('run_id')),
                  checked_at=_timestamp(value.get('checked_at')),
                  last_attempt_at=_timestamp(value.get('last_attempt_at')),
                  last_completed_at=_timestamp(value.get('last_completed_at')))
    completed = sum(s['outcome'] in ('candidate', 'candidate_partial', 'valid_empty') for s in result['sources'])
    failed = sum(s['outcome'] == 'parse_error' for s in result['sources'])
    unresolved = any(s['outcome'] in ('candidate_partial', 'placement_unresolved') for s in result['sources'])
    disabled = sum(s['outcome'] == 'disabled' for s in result['sources'])
    result['available_sources'] = completed
    if disabled == len(EDITION_IDS) and not result['checked_at']:
        result['status'] = 'disabled'
    elif unresolved and result['checked_at']:
        result['status'] = 'partial'
    elif completed == len(EDITION_IDS) and result['checked_at']:
        result['status'] = 'healthy'
    elif failed == len(EDITION_IDS) and result['checked_at']:
        result['status'] = 'unavailable'
    elif (completed or failed) and result['checked_at']:
        result['status'] = 'partial'
    elif all(s['outcome'] == 'not_checked' for s in result['sources']) and not result['checked_at']:
        result['status'] = 'not_checked' if value.get('status') == 'not_checked' else 'unknown'
    if value.get('status') in ('cancelled', 'unknown') and result['last_attempt_at']:
        result['status'] = value['status']
    if value.get('status') != result['status']:
        result = empty_source_health('unknown')
        result['reason'] = ('invalid_disabled_diagnostic' if value.get('status') == 'disabled' or
                            any(row.get('outcome') == 'disabled' for row in rows) else 'invalid_discovery_state')
        return result
    if value.get('reason') == 'invalid_discovery_state':
        result['reason'] = 'invalid_discovery_state'
    attempt = value.get('last_attempt')
    if isinstance(attempt, dict) and isinstance(attempt.get('sources'), list) and len(attempt['sources']) == len(EDITION_IDS):
        attempt_sources = []
        for index, selected in enumerate(EDITION_IDS):
            row = attempt['sources'][index]
            if (not isinstance(row, dict) or row.get('edition_id') != selected or
                    not isinstance(row.get('outcome'), str) or row['outcome'] not in allowed):
                break
            if not discovery_row_consistent(row['outcome'], row.get('accepted'),
                                            row.get('sponsor_excluded'), row.get('placement_unresolved')):
                break
            attempt_sources.append({'name': result['sources'][index]['name'], 'edition_id': selected,
                                    'outcome': row['outcome'], 'accepted': _discovery_count(row.get('accepted')),
                                    'sponsor_excluded': _discovery_count(row.get('sponsor_excluded')),
                                    'placement_unresolved': _discovery_count(row.get('placement_unresolved')),
                                    'reason': row.get('reason') if row.get('reason') == 'inconsistent_outcome_counts' else None,
                                    'parse_warning': False})
        if len(attempt_sources) == len(EDITION_IDS):
            result['last_attempt'] = {'run_id': _identifier(attempt.get('run_id')),
                                      'checked_at': _timestamp(attempt.get('checked_at')),
                                      'status': attempt.get('status') if attempt.get('status') in ('cancelled', 'unknown') else 'unknown',
                                      'available_sources': sum(s['outcome'] in ('candidate', 'candidate_partial', 'valid_empty') for s in attempt_sources),
                                      'sources': attempt_sources}
    latest = value.get('latest_run')
    if isinstance(latest, dict) and latest.get('status') in RUN_STATUSES:
        result['latest_run'] = {'status': latest['status'], 'finished_at': _timestamp(latest.get('finished_at')),
                                'episode_id': _identifier(latest.get('episode_id'))}
    return result


def build_source_health(diagnostics, run_id, checked_at):
    if isinstance(diagnostics, dict) and isinstance(diagnostics.get('discovery'), dict):
        from src.acquisition.catalogue import EDITION_IDS
        from src.provenance import (article_identity, discovery_issue_identity_consistent,
                                    discovery_row_consistent, expected_discovery_status)
        discovery = diagnostics['discovery']
        result = empty_source_health()
        rows = discovery.get('editions')
        if (not isinstance(rows, list) or len(rows) != len(EDITION_IDS) or
                [row.get('edition_id') if isinstance(row, dict) else None for row in rows] != list(EDITION_IDS)):
            result['status'] = 'unknown'
            result['reason'] = 'invalid_disabled_diagnostic' if discovery.get('status') == 'disabled' else 'invalid_discovery_state'
            return result
        if discovery.get('reason') == 'invalid_discovery_state':
            result['status'] = 'unknown'
            result['reason'] = 'invalid_discovery_state'
            return result
        for row in rows:
            if not discovery_row_consistent(row.get('outcome'), row.get('candidates'),
                                            row.get('sponsor_excluded'), row.get('placement_unresolved'),
                                            row.get('received'), row.get('rejected')):
                result['status'] = 'unknown'
                result['reason'] = 'inconsistent_outcome_counts'
                return result
            source_identity = row.get('issue_source', row.get('issue_url'))
            if not discovery_issue_identity_consistent(row['edition_id'], row.get('outcome'),
                                                        row.get('issue_id'), source_identity):
                result['status'] = 'unknown'
                result['reason'] = 'invalid_issue_identity'
                return result
        if discovery.get('reason') == 'invalid_discovery_state' or discovery.get('status') != expected_discovery_status(rows):
            result['status'] = 'unknown'
            result['reason'] = 'invalid_disabled_diagnostic' if discovery.get('status') == 'disabled' else 'invalid_discovery_state'
            return result
        if discovery.get('status') == 'disabled':
            for source in result['sources']:
                source['outcome'] = 'disabled'
            result['status'] = 'disabled'
            return result
        by_id = {row.get('edition_id'): row for row in rows
                 if isinstance(row, dict) and isinstance(row.get('edition_id'), str)}
        for source in result['sources']:
            row = by_id.get(source['edition_id'], {})
            source['outcome'] = row.get('outcome') if row.get('outcome') in ('candidate', 'candidate_partial', 'valid_empty', 'placement_unresolved', 'parse_error') else 'not_checked'
            source['issue_id'] = row.get('issue_id')
            source['issue_source'] = row.get('issue_source') or article_identity(row.get('issue_url'))
            source['accepted'] = _discovery_count(row.get('candidates'))
            source['sponsor_excluded'] = _discovery_count(row.get('sponsor_excluded'))
            source['placement_unresolved'] = _discovery_count(row.get('placement_unresolved'))
            source['reason'] = row.get('reason') if row.get('reason') == 'inconsistent_outcome_counts' else None
        result['status'] = {'fixture_only': 'healthy', 'partial': 'partial',
                            'failed': 'unavailable'}[discovery['status']]
        result.update(run_id=_identifier(run_id), check_id=_identifier(run_id),
                      checked_at=_timestamp(checked_at), last_attempt_at=_timestamp(checked_at),
                      last_completed_at=_timestamp(checked_at))
        return _sanitize_discovery_health(result)
    result = _empty_legacy_source_health()
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

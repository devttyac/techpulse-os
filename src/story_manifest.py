"""Immutable RSS evidence and application-owned extractive story narration.

Source text remains untrusted data. Exact tracing establishes provenance to
received RSS fields, not the truth of a publisher's claims.
"""
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

from src.content_availability import DOMAIN_ORDER, build_content_availability


@dataclass(frozen=True)
class EvidenceUnit:
    unit_id: str
    field: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class DomainCoverage:
    domain: str
    status: str
    reason: str


@dataclass(frozen=True)
class Story:
    story_id: str
    domain: str
    article_id: str
    source_title: str
    source_name: str
    source_url: str
    source_summary: str
    published_at: str
    evidence_digest: str
    units: tuple[EvidenceUnit, ...]
    selected_unit_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoryManifest:
    coverage: tuple[DomainCoverage, ...]
    stories: tuple[Story, ...]
    schema_version: int = 1
    selection_cap: int = 3


_STATUSES = ('available', 'no_received_candidates', 'source_unavailable')
_STORY_FIELDS = set(Story.__dataclass_fields__)
_UNIT_FIELDS = set(EvidenceUnit.__dataclass_fields__)
_TIMING_FIELDS = {'seconds', 'time', 'start_seconds', 'end_seconds', 'duration_seconds'}


def _digest(value):
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _evidence_digest(title, summary):
    return _digest({'title': title, 'summary': summary})


def _safe_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        return False
    try:
        parsed = urlsplit(value)
        # No browser-executable schemes or ambiguous credential-bearing URLs.
        return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        return False


def _units(title, summary):
    result = []
    for field, source in (('title', title), ('summary', summary)):
        number = 0
        # Prefer source sentence boundaries; split oversized sentences at exact
        # word spans. Both sentence whitespace and original offsets are retained.
        for sentence in re.finditer(r'\S.*?(?:[.!?](?=\s|$)|$)', source, re.DOTALL):
            words = list(re.finditer(r'\S+', sentence.group()))
            for offset in range(0, len(words), 24):
                group = words[offset:offset + 24]
                start = sentence.start() + group[0].start()
                end = sentence.start() + group[-1].end()
                result.append(EvidenceUnit(f'{field}-{number}', field, start, end, source[start:end]))
                number += 1
    return tuple(result)


def _exact_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f'Invalid {label} fields')


def _check_story_evidence(story):
    """Frozen constructors alone do not establish validity of caller-made objects."""
    if not isinstance(story, Story):
        raise ValueError('Expected story')
    if any(type(getattr(story, k)) is not str for k in _STORY_FIELDS - {'units', 'selected_unit_ids'}):
        raise ValueError('Story metadata must be text')
    if story.domain not in DOMAIN_ORDER or not _safe_url(story.source_url):
        raise ValueError('Invalid story domain or URL')
    article_id = hashlib.sha256(story.source_url.encode('utf-8')).hexdigest()
    if story.article_id != article_id or story.story_id != f'{story.domain}-{article_id}':
        raise ValueError('Story identity mismatch')
    if story.evidence_digest != _evidence_digest(story.source_title, story.source_summary):
        raise ValueError('Evidence digest mismatch')
    expected = _units(story.source_title, story.source_summary)
    if not expected or type(story.units) is not tuple or len(story.units) != len(expected):
        raise ValueError('Invalid evidence units')
    for actual, unit in zip(story.units, expected):
        if (not isinstance(actual, EvidenceUnit) or type(actual.start) is not int
                or type(actual.end) is not int or actual != unit):
            raise ValueError('Evidence units do not match source spans')
    if type(story.selected_unit_ids) is not tuple:
        raise ValueError('Selected IDs must be immutable')


def validate_evidence_selection(story: Story, payload: dict) -> tuple[str, ...]:
    """Accept IDs only; mandatory title and one summary, canonical title-first order."""
    _check_story_evidence(story)
    _exact_keys(payload, {'story_id', 'unit_ids'}, 'selection')
    if type(payload['story_id']) is not str or payload['story_id'] != story.story_id:
        raise ValueError('Selection story identity mismatch')
    ids = payload['unit_ids']
    if not isinstance(ids, list) or not 1 <= len(ids) <= 2 or any(type(i) is not str for i in ids):
        raise ValueError('Selection must contain one or two evidence IDs')
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate evidence selection')
    known = {u.unit_id: u for u in story.units}
    if any(i not in known for i in ids):
        raise ValueError('Unknown evidence selection')
    title = next((u.unit_id for u in story.units if u.field == 'title'), None)
    summaries = [u.unit_id for u in story.units if u.field == 'summary']
    expected_count = int(title is not None) + int(bool(summaries))
    if len(ids) != expected_count or (title is not None and title not in ids):
        raise ValueError('Selection requires first title and one available summary')
    selected_summary = [i for i in ids if i in summaries]
    if len(selected_summary) != int(bool(summaries)):
        raise ValueError('Selection requires exactly one available summary')
    return tuple(([title] if title else []) + selected_summary)


def deterministic_evidence_selection(story: Story) -> tuple[str, ...]:
    ids = [next((u.unit_id for u in story.units if u.field == field), None)
           for field in ('title', 'summary')]
    return validate_evidence_selection(story, {'story_id': story.story_id,
                                              'unit_ids': [i for i in ids if i]})


def freeze_story_manifest(corpus: dict, availability: dict) -> StoryManifest:
    """Capture the received prefix before exact-URL deduplication; never backfill."""
    if not isinstance(corpus, dict) or any(d not in DOMAIN_ORDER for d in corpus):
        raise ValueError('Invalid corpus domains')
    if availability is None:
        availability = build_content_availability(corpus)
    if (not isinstance(availability, dict) or type(availability.get('schema_version')) is not int
            or availability['schema_version'] != 1
            or not isinstance(availability.get('domains'), dict)
            or set(availability['domains']) != set(DOMAIN_ORDER)):
        raise ValueError('Invalid content availability')
    coverage, stories = [], []
    for domain in DOMAIN_ORDER:
        state = availability['domains'][domain]
        if not isinstance(state, dict):
            raise ValueError('Invalid coverage state')
        coverage.append(DomainCoverage(domain, state.get('status'), state.get('reason')))
        received = corpus.get(domain, [])
        if not isinstance(received, list):
            raise ValueError('Candidates must be a list')
        seen = set()
        for candidate in received[:3]:
            if not isinstance(candidate, dict) or not _safe_url(candidate.get('url')):
                raise ValueError('Unsafe or missing source URL')
            url = candidate['url']
            if url in seen:
                continue
            seen.add(url)
            title = candidate.get('title', '')
            summary = candidate.get('summary', '')
            # Missing or unusable summaries provide no summary evidence.
            summary = summary if isinstance(summary, str) else ''
            name, published = candidate.get('source_name', ''), candidate.get('published_at', '')
            if any(not isinstance(v, str) for v in (title, name, published)):
                raise ValueError('Source metadata must be text')
            units = _units(title, summary)
            if not units:
                raise ValueError('Story has no usable evidence')
            article_id = hashlib.sha256(url.encode('utf-8')).hexdigest()
            stories.append(Story(f'{domain}-{article_id}', domain, article_id, title, name,
                                 url, summary, published, _evidence_digest(title, summary), units))
    return manifest_from_dict(manifest_to_dict(StoryManifest(tuple(coverage), tuple(stories))))


def manifest_to_dict(manifest: StoryManifest) -> dict:
    """Return detached JSON-compatible data, including selected IDs when present."""
    if not isinstance(manifest, StoryManifest):
        raise ValueError('Expected story manifest')
    return {'schema_version': manifest.schema_version, 'selection_cap': manifest.selection_cap,
            'coverage': [asdict(c) for c in manifest.coverage],
            'stories': [{**asdict(s), 'units': [asdict(u) for u in s.units],
                         'selected_unit_ids': list(s.selected_unit_ids)} for s in manifest.stories]}


def manifest_from_dict(payload: dict) -> StoryManifest:
    """Strict new-contract reader; reconstruct exact units instead of trusting spans."""
    _exact_keys(payload, {'schema_version', 'selection_cap', 'coverage', 'stories'}, 'manifest')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('Unsupported story schema version')
    if type(payload['selection_cap']) is not int or payload['selection_cap'] != 3:
        raise ValueError('Unsupported story selection cap')
    coverage_values, story_values = payload['coverage'], payload['stories']
    if not isinstance(coverage_values, list) or len(coverage_values) != len(DOMAIN_ORDER):
        raise ValueError('Coverage must contain eight ordered domains')
    coverage = []
    for domain, value in zip(DOMAIN_ORDER, coverage_values):
        _exact_keys(value, {'domain', 'status', 'reason'}, 'coverage')
        if (value['domain'] != domain or type(value['status']) is not str
                or value['status'] not in _STATUSES or type(value['reason']) is not str):
            raise ValueError('Invalid coverage identity/status/reason')
        coverage.append(DomainCoverage(**value))
    if not isinstance(story_values, list) or len(story_values) > 24:
        raise ValueError('Invalid story collection')
    stories, seen, counts = [], set(), {d: 0 for d in DOMAIN_ORDER}
    previous_domain = -1
    for value in story_values:
        _exact_keys(value, _STORY_FIELDS, 'story')
        if any(type(value[k]) is not str for k in _STORY_FIELDS - {'units', 'selected_unit_ids'}):
            raise ValueError('Story metadata must be text')
        domain = value['domain']
        if domain not in DOMAIN_ORDER or DOMAIN_ORDER.index(domain) < previous_domain:
            raise ValueError('Invalid story domain order')
        previous_domain = DOMAIN_ORDER.index(domain)
        counts[domain] += 1
        if counts[domain] > 3 or not _safe_url(value['source_url']):
            raise ValueError('Invalid story cap or citation URL')
        article_id = hashlib.sha256(value['source_url'].encode('utf-8')).hexdigest()
        story_id = f'{domain}-{article_id}'
        if value['article_id'] != article_id or value['story_id'] != story_id or story_id in seen:
            raise ValueError('Invalid or duplicate story identity')
        seen.add(story_id)
        title, summary = value['source_title'], value['source_summary']
        if value['evidence_digest'] != _evidence_digest(title, summary):
            raise ValueError('Evidence digest mismatch')
        expected_units = _units(title, summary)
        raw_units = value['units']
        if not expected_units or not isinstance(raw_units, list) or len(raw_units) != len(expected_units):
            raise ValueError('Invalid evidence units')
        for raw, expected in zip(raw_units, expected_units):
            _exact_keys(raw, _UNIT_FIELDS, 'evidence unit')
            if (any(type(raw[k]) is not str for k in ('unit_id', 'field', 'text'))
                    or type(raw['start']) is not int or type(raw['end']) is not int
                    or raw != asdict(expected)):
                raise ValueError('Evidence units do not match source spans')
        ids = value['selected_unit_ids']
        if not isinstance(ids, list):
            raise ValueError('Selected evidence IDs must be a list')
        story = Story(**{**value, 'units': expected_units, 'selected_unit_ids': ()})
        if ids:
            accepted = validate_evidence_selection(story, {'story_id': story_id, 'unit_ids': ids})
            if tuple(ids) != accepted:
                raise ValueError('Selected evidence order mismatch')
            story = replace(story, selected_unit_ids=accepted)
        stories.append(story)
    if any((c.status == 'available') != (counts[c.domain] > 0) for c in coverage):
        raise ValueError('Coverage/story availability mismatch')
    return StoryManifest(tuple(coverage), tuple(stories))


def apply_evidence_selections(manifest: StoryManifest,
                             selections: dict[str, tuple[str, ...]]) -> StoryManifest:
    manifest = manifest_from_dict(manifest_to_dict(manifest))
    if not isinstance(selections, dict) or set(selections) != {s.story_id for s in manifest.stories}:
        raise ValueError('Selections must cover exactly the manifest stories')
    stories = []
    for story in manifest.stories:
        ids = selections[story.story_id]
        if not isinstance(ids, tuple):
            raise ValueError('Accepted selections must be tuples')
        accepted = validate_evidence_selection(story, {'story_id': story.story_id, 'unit_ids': list(ids)})
        stories.append(replace(story, selected_unit_ids=accepted))
    return replace(manifest, stories=tuple(stories))


def render_story_segments(story: Story) -> tuple[dict, ...]:
    accepted = validate_evidence_selection(story, {'story_id': story.story_id,
                                                  'unit_ids': list(story.selected_unit_ids)})
    if accepted != story.selected_unit_ids:
        raise ValueError('Selected evidence order mismatch')
    known = {u.unit_id: u for u in story.units}
    segments = tuple({'story_id': story.story_id, 'domain': story.domain,
                      'speaker': 'Host A' if i == 0 else 'Host B',
                      'chapter_title': story.source_title, 'evidence_unit_ids': [unit_id],
                      'text': f'{"Source excerpt:" if i == 0 else "Further excerpt:"} "{known[unit_id].text}"'}
                     for i, unit_id in enumerate(accepted))
    if sum(len(s['text'].split()) for s in segments) > 60:
        raise ValueError('Story exceeds spoken word budget')
    return segments


def _matches_with_timing(actual, expected):
    if not isinstance(actual, dict) or not set(expected) <= set(actual):
        return False
    if set(actual) - set(expected) - _TIMING_FIELDS:
        return False
    if any(actual[k] != v for k, v in expected.items()):
        return False
    for key in set(actual) - set(expected):
        value = actual[key]
        if key == 'time':
            if not isinstance(value, str) or not re.fullmatch(r'\d+:\d{2}', value):
                return False
        elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return False
    return True


def validate_story_episode(episode: dict) -> tuple[bool, str]:
    """Check exact narration, ordered chapter identities and citations; never repair."""
    try:
        if not isinstance(episode, dict) or 'story_manifest' not in episode:
            raise ValueError('Missing story manifest')
        manifest = manifest_from_dict(episode['story_manifest'])
        if not manifest.stories:
            raise ValueError('No stories to publish')
        segments = [seg for story in manifest.stories for seg in render_story_segments(story)]
        chapters = [{'story_id': s.story_id, 'domain': s.domain, 'title': s.source_title,
                     'source_name': s.source_name, 'source_url': s.source_url} for s in manifest.stories]
        for field, expected in (('script_segments', segments), ('chapters', chapters)):
            actual = episode.get(field)
            if (not isinstance(actual, list) or len(actual) != len(expected)
                    or any((a != e if field == 'script_segments' else not _matches_with_timing(a, e))
                           for a, e in zip(actual, expected))):
                raise ValueError(f'Exact story {field} mismatch')
        return True, ''
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False, 'Invalid source-bound story contract'


def episode_fingerprint(manifest: StoryManifest) -> str:
    manifest = manifest_from_dict(manifest_to_dict(manifest))
    return _digest({'schema_version': manifest.schema_version, 'selection_cap': manifest.selection_cap,
                    'coverage': [asdict(c) for c in manifest.coverage],
                    'stories': [{'story_id': s.story_id, 'domain': s.domain,
                                 'article_id': s.article_id, 'evidence_digest': s.evidence_digest}
                                for s in manifest.stories]})


def audio_recipe_fingerprint(manifest: StoryManifest, voice_map: dict[str, str]) -> str:
    manifest = manifest_from_dict(manifest_to_dict(manifest))
    if not isinstance(voice_map, dict):
        raise ValueError('Invalid voice map')
    recipe = []
    for story in manifest.stories:
        for segment in render_story_segments(story):
            voice = voice_map.get(segment['speaker'])
            if not isinstance(voice, str) or not voice:
                raise ValueError('Missing story voice')
            recipe.append({'story_id': story.story_id,
                           'evidence_unit_ids': segment['evidence_unit_ids'],
                           'text': segment['text'], 'voice': voice})
    return _digest({'schema_version': manifest.schema_version, 'segments': recipe})


def selected_evidence_context(manifest: StoryManifest) -> dict[str, list[dict]]:
    """Selected excerpts only, in manifest order, with their source identity."""
    manifest = manifest_from_dict(manifest_to_dict(manifest))
    context = {domain: [] for domain in DOMAIN_ORDER}
    for story in manifest.stories:
        render_story_segments(story)  # An unselected snapshot is not usable chat evidence.
        chosen = [u for u in story.units if u.unit_id in story.selected_unit_ids]
        context[story.domain].append({
            'story_id': story.story_id, 'domain': story.domain,
            'title': story.source_title, 'source_name': story.source_name,
            'url': story.source_url,
            'evidence': [{'unit_id': u.unit_id, 'text': u.text} for u in chosen]})
    return context

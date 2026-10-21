"""TP03 typed, immutable complete-source evidence contract.

The acquisition/admission stages must establish body completeness. This
contract records their explicit decision and rejects partial or raw-only
inputs; a digest alone cannot prove that a publisher supplied its full body.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_SOURCE_KINDS = frozenset({"publisher_article", "technical_documentation", "research_paper",
                           "repository", "discussion", "publisher_transcript"})
_BLOCK_KINDS = frozenset({"heading", "paragraph", "list", "code", "table", "formula",
                          "caption", "qualification", "transcript_cue"})
_REQUIRED_CHECKS = {
    "publisher_article": frozenset({"identity", "body_boundary", "continuation", "structure"}),
    "technical_documentation": frozenset({"identity", "page_set", "structure"}),
    "research_paper": frozenset({"identity", "all_pages", "structure", "visual_meaning"}),
    "repository": frozenset({"identity", "resolved_revision", "file_scope", "structure"}),
    "discussion": frozenset({"identity", "cursor_coverage", "membership_stability", "structure"}),
    "publisher_transcript": frozenset({"identity", "media_identity", "speech_coverage",
                                        "visual_context", "structure"}),
}
_LOCATORS = {
    "html": {"selector": str},
    "document": {"section": str},
    "pdf": {"page": int, "region": str},
    "repository": {"commit": str, "path": str, "start_line": int, "end_line": int},
    "discussion": {"member_id": str},
    "transcript": {},
}
_OPTIONAL_LOCATOR_FIELDS = {
    "discussion": {"parent_id": str},
    "transcript": {"speaker": str, "cue_start": str, "cue_end": str},
}
_SOURCE_LOCATORS = {
    "publisher_article": frozenset({"html"}),
    "technical_documentation": frozenset({"html", "document", "pdf"}),
    "research_paper": frozenset({"html", "pdf"}),
    "repository": frozenset({"repository"}),
    "discussion": frozenset({"discussion"}),
    "publisher_transcript": frozenset({"transcript"}),
}


@dataclass(frozen=True)
class SourceLocator:
    kind: str
    details: dict


@dataclass(frozen=True)
class SourceBlock:
    block_id: str
    kind: str
    text: str
    source_locator: SourceLocator
    structure: "ListPosition | TableGrid | None" = None
    relations: tuple["BlockRelation", ...] = ()


@dataclass(frozen=True)
class ListPosition:
    depth: int
    ordinal: int
    parent_item_id: str | None
    list_id: str = "list-1"


@dataclass(frozen=True)
class TableCell:
    row: int
    column: int
    row_span: int
    column_span: int
    text: str
    role: str


@dataclass(frozen=True)
class TableGrid:
    rows: int
    columns: int
    cells: tuple[TableCell, ...]


@dataclass(frozen=True)
class BlockRelation:
    relation: str
    target_block_id: str


@dataclass(frozen=True)
class DateEvidence:
    event_type: str
    status: str
    raw_value: str
    source_location: str
    precision: str
    offset: str
    owner: str
    conflicting_values: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class PublicationProvenance:
    publisher: str
    source_url: str
    events: tuple[DateEvidence, ...]


@dataclass(frozen=True)
class SourceMember:
    member_id: str
    kind: str
    locator: str
    sha256: str


@dataclass(frozen=True)
class AcquisitionRecipe:
    mode: str
    client: str
    configuration_revision: str
    raw_representation_sha256: str
    decoding: str
    safe_header_evidence: str
    extraction_policy_revision: str
    members: tuple[SourceMember, ...]


@dataclass(frozen=True)
class CompletionCheck:
    check_id: str
    status: str
    evidence_ref: str


@dataclass(frozen=True)
class CompletionEvidence:
    decision_id: str
    profile_revision: str
    confirmed_complete: bool
    represented_body_sha256: str
    declared_scope: str
    required_member_ids: tuple[str, ...]
    observed_member_ids: tuple[str, ...]
    checks: tuple[CompletionCheck, ...]


@dataclass(frozen=True)
class Passage:
    passage_id: str
    version_id: str
    block_id: str
    start: int
    end: int
    text: str
    source_locator: SourceLocator


@dataclass(frozen=True)
class SourceUnit:
    schema_version: int
    article_id: str
    source_kind: str
    unit_scope: str
    publication: PublicationProvenance
    acquisition: AcquisitionRecipe
    completion: CompletionEvidence
    blocks: tuple[SourceBlock, ...]
    normalized_sha256: str
    version_id: str
    passages: tuple[Passage, ...]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _nonempty(value, label):
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"invalid {label}")


def _digest(value, label):
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"invalid {label}")


def _validate_dates(publication, source_kind):
    if type(publication.events) is not tuple or not publication.events:
        raise ValueError("missing source date events")
    seen = set()
    for event in publication.events:
        if not isinstance(event, DateEvidence) or event.event_type not in {
                "publisher_publication", "original_media_release", "transcript_publication",
                "retrieval", "revision", "modification"} or event.event_type in seen:
            raise ValueError("invalid or duplicate date event")
        seen.add(event.event_type)
        if (type(event.raw_value) is not str or type(event.source_location) is not str
                or type(event.offset) is not str or type(event.owner) is not str
                or type(event.conflicting_values) is not tuple
                or type(event.explanation) is not str):
            raise ValueError("invalid date evidence fields")
        if event.status == "known":
            for field in (event.raw_value, event.source_location, event.owner):
                _nonempty(field, "known date provenance")
            if event.precision not in ("day", "instant") or event.conflicting_values:
                raise ValueError("invalid known date precision or conflict")
        elif event.status == "unknown":
            if (event.raw_value or event.precision != "unknown"
                    or event.conflicting_values or not event.explanation.strip()):
                raise ValueError("unknown date must remain unknown")
        elif event.status == "conflict":
            if (event.raw_value or event.precision != "unknown"
                    or len(event.conflicting_values) < 2
                    or any(type(item) is not str or not item.strip()
                           for item in event.conflicting_values)
                    or not event.explanation.strip()):
                raise ValueError("date conflict must retain candidates and reason")
        else:
            raise ValueError("invalid date status")
    required = "original_media_release" if source_kind == "publisher_transcript" else "publisher_publication"
    if required not in seen:
        raise ValueError("original publisher publication event missing")
    original = next(event for event in publication.events if event.event_type == required)
    if original.status == "known" and original.owner != publication.publisher:
        raise ValueError("original publication is not owned by the named publisher")


def _validate_table(block):
    grid = block.structure
    if (not isinstance(grid, TableGrid) or type(grid.rows) is not int
            or type(grid.columns) is not int or grid.rows < 1 or grid.columns < 1
            or type(grid.cells) is not tuple or not grid.cells):
        raise ValueError("missing table grid")
    occupied = set()
    headers = 0
    origins = []
    for cell in grid.cells:
        if (not isinstance(cell, TableCell)
                or any(type(v) is not int for v in (cell.row, cell.column, cell.row_span, cell.column_span))
                or cell.row < 0 or cell.column < 0 or cell.row_span < 1 or cell.column_span < 1
                or cell.row + cell.row_span > grid.rows or cell.column + cell.column_span > grid.columns
                or cell.role not in ("header", "data")):
            raise ValueError("invalid table cell coordinate or role")
        _nonempty(cell.text, "table cell text")
        headers += int(cell.role == "header")
        origins.append((cell.row, cell.column))
        for row in range(cell.row, cell.row + cell.row_span):
            for column in range(cell.column, cell.column + cell.column_span):
                if (row, column) in occupied:
                    raise ValueError("overlapping table cells")
                occupied.add((row, column))
    if (not headers or len(occupied) != grid.rows * grid.columns
            or origins != sorted(origins)):
        raise ValueError("table header, coverage or reading order missing")
    lines = [" | ".join(cell.text for cell in grid.cells if cell.row == row)
             for row in range(grid.rows)]
    if block.text != "\n".join(lines):
        raise ValueError("table text differs from ordered cells")


def _validate_structure(blocks):
    previous = {}
    list_ordinals = {}
    all_ids = {block.block_id for block in blocks}
    graph = {}
    for block in blocks:
        if block.kind == "list":
            position = block.structure
            if (not isinstance(position, ListPosition) or type(position.depth) is not int
                    or type(position.ordinal) is not int or position.depth < 0
                    or position.ordinal < 1 or type(position.parent_item_id) not in (str, type(None))
                    or type(position.list_id) is not str or not position.list_id.strip()):
                raise ValueError("invalid list position")
            parent = position.parent_item_id
            if (position.depth == 0 and parent is not None) or (position.depth > 0 and
                    (parent not in previous or previous[parent].kind != "list"
                     or previous[parent].structure.depth != position.depth - 1
                     or previous[parent].structure.list_id != position.list_id)):
                raise ValueError("nested list has no matching parent")
            key = (position.list_id, parent, position.depth)
            if position.ordinal != list_ordinals.get(key, 0) + 1:
                raise ValueError("list order has a gap or duplicate")
            list_ordinals[key] = position.ordinal
        elif block.kind == "table":
            _validate_table(block)
        elif block.structure is not None:
            raise ValueError("structure does not match block kind")
        if type(block.relations) is not tuple:
            raise ValueError("invalid block relations")
        relations = set()
        graph[block.block_id] = []
        for relation in block.relations:
            if (not isinstance(relation, BlockRelation)
                    or relation.relation not in ("qualifies", "caption_of", "explains")
                    or type(relation.target_block_id) is not str
                    or relation.target_block_id not in all_ids
                    or relation.target_block_id == block.block_id
                    or (relation.relation, relation.target_block_id) in relations):
                raise ValueError("dangling, cyclic or duplicate block relation")
            relations.add((relation.relation, relation.target_block_id))
            graph[block.block_id].append(relation.target_block_id)
        if block.kind == "qualification" and not any(r.relation == "qualifies" for r in block.relations):
            raise ValueError("qualification has no claim link")
        if block.kind == "caption" and not any(r.relation == "caption_of" for r in block.relations):
            raise ValueError("caption has no source link")
        previous[block.block_id] = block
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError("cyclic source block relations")
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for block in blocks:
        visit(block.block_id)


def original_publication_event(unit: SourceUnit) -> DateEvidence | None:
    """Never substitute transcript/retrieval/revision for original release."""
    validate_source_unit(unit)
    required = "original_media_release" if unit.source_kind == "publisher_transcript" else "publisher_publication"
    return next((event for event in unit.publication.events
                 if event.event_type == required and event.status == "known"), None)


def _validate_inputs(article_id, source_kind, unit_scope, publication,
                     acquisition, completion, blocks):
    if type(article_id) is not str or not _ID.fullmatch(article_id):
        raise ValueError("invalid independent article identity")
    if source_kind not in _SOURCE_KINDS:
        raise ValueError("unsupported source kind")
    _nonempty(unit_scope, "unit scope")
    if not isinstance(publication, PublicationProvenance):
        raise ValueError("missing publication provenance")
    for name in ("publisher", "source_url"):
        _nonempty(getattr(publication, name), name)
    _validate_dates(publication, source_kind)
    parsed = urlsplit(publication.source_url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(c.isspace() or ord(c) < 32 for c in publication.source_url)):
        raise ValueError("invalid original publisher URL")
    if not isinstance(acquisition, AcquisitionRecipe):
        raise ValueError("missing acquisition recipe")
    for name in ("mode", "client", "configuration_revision", "decoding",
                 "safe_header_evidence", "extraction_policy_revision"):
        _nonempty(getattr(acquisition, name), name)
    _digest(acquisition.raw_representation_sha256, "raw representation digest")
    if type(acquisition.members) is not tuple:
        raise ValueError("invalid source member collection")
    member_ids = []
    for member in acquisition.members:
        if not isinstance(member, SourceMember) or type(member.member_id) is not str or not _ID.fullmatch(member.member_id):
            raise ValueError("invalid source member identity")
        if member.kind not in ("page", "cursor", "continuation", "file", "asset", "context"):
            raise ValueError("invalid source member kind")
        _nonempty(member.locator, "member locator")
        _digest(member.sha256, "member digest")
        member_ids.append(member.member_id)
    if len(set(member_ids)) != len(member_ids):
        raise ValueError("duplicate source member identity")
    if not isinstance(completion, CompletionEvidence) or completion.confirmed_complete is not True:
        raise ValueError("source body has no complete admission decision")
    _nonempty(completion.decision_id, "completion decision")
    _nonempty(completion.profile_revision, "completion profile")
    _digest(completion.represented_body_sha256, "represented body digest")
    if completion.declared_scope != unit_scope:
        raise ValueError("completion decision scope mismatch")
    if (type(completion.required_member_ids) is not tuple
            or type(completion.observed_member_ids) is not tuple
            or completion.required_member_ids != tuple(member_ids)
            or completion.observed_member_ids != completion.required_member_ids):
        raise ValueError("required source member coverage incomplete")
    if type(completion.checks) is not tuple:
        raise ValueError("missing completion checks")
    check_ids = set()
    for check in completion.checks:
        if not isinstance(check, CompletionCheck) or type(check.check_id) is not str or not check.check_id:
            raise ValueError("invalid completion check")
        if check.check_id in check_ids or check.status != "passed":
            raise ValueError("duplicate or unfinished completion check")
        _nonempty(check.evidence_ref, "completion check evidence")
        check_ids.add(check.check_id)
    if not _REQUIRED_CHECKS[source_kind] <= check_ids:
        raise ValueError("type-specific completion checks missing")
    if type(blocks) is not tuple or not blocks:
        raise ValueError("raw-only or empty source is not complete evidence")
    ids = set()
    for block in blocks:
        if not isinstance(block, SourceBlock) or type(block.block_id) is not str or not _ID.fullmatch(block.block_id):
            raise ValueError("invalid source block identity")
        if block.block_id in ids or block.kind not in _BLOCK_KINDS:
            raise ValueError("duplicate or unsupported source block")
        ids.add(block.block_id)
        _nonempty(block.text, "source block text")
        locator = block.source_locator
        if not isinstance(locator, SourceLocator) or locator.kind not in _LOCATORS:
            raise ValueError("unsupported source locator")
        if locator.kind not in _SOURCE_LOCATORS[source_kind]:
            raise ValueError("source kind contradicts structural locator")
        required = _LOCATORS[locator.kind]
        optional = _OPTIONAL_LOCATOR_FIELDS.get(locator.kind, {})
        if (type(locator.details) is not dict or not set(required) <= set(locator.details)
                or not set(locator.details) <= set(required) | set(optional)):
            raise ValueError("incomplete source locator")
        for name, expected in {**required, **optional}.items():
            if name not in locator.details:
                continue
            value = locator.details[name]
            if type(value) is not expected or (expected is str and not value.strip()) or (expected is int and value < 0):
                raise ValueError("invalid source locator coordinate")
        if locator.kind == "repository" and locator.details["end_line"] < locator.details["start_line"]:
            raise ValueError("invalid repository line span")
    pdf_pages = {str(block.source_locator.details["page"]) for block in blocks
                 if block.source_locator.kind == "pdf"}
    declared_pages = {member.locator for member in acquisition.members if member.kind == "page"}
    if not pdf_pages <= declared_pages:
        raise ValueError("represented PDF page lacks a declared retained member")
    discussion_ids = {block.source_locator.details["member_id"] for block in blocks
                      if block.source_locator.kind == "discussion"}
    if any(block.source_locator.details.get("parent_id") not in discussion_ids
           for block in blocks if block.source_locator.kind == "discussion"
           and "parent_id" in block.source_locator.details):
        raise ValueError("discussion reply has no represented parent")
    _validate_structure(blocks)


def build_source_unit(article_id: str, source_kind: str, unit_scope: str,
                      publication: PublicationProvenance, acquisition: AcquisitionRecipe,
                      completion: CompletionEvidence,
                      blocks: tuple[SourceBlock, ...]) -> SourceUnit:
    """Construct one version from admitted structure; IDs never use the URL."""
    _validate_inputs(article_id, source_kind, unit_scope, publication,
                     acquisition, completion, blocks)
    body = "\n".join(block.text for block in blocks)
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != completion.represented_body_sha256:
        raise ValueError("admission decision does not bind represented body")
    normalized = _hash([asdict(block) for block in blocks])
    core = {"schema_version": SCHEMA_VERSION, "article_id": article_id,
            "source_kind": source_kind, "unit_scope": unit_scope,
            "publication": asdict(publication), "acquisition": asdict(acquisition),
            "completion": asdict(completion), "normalized_sha256": normalized}
    version = _hash(core)
    passages = tuple(Passage(f"{version}:{block.block_id}:0:{len(block.text)}",
                             version, block.block_id, 0, len(block.text), block.text,
                             block.source_locator) for block in blocks)
    return SourceUnit(SCHEMA_VERSION, article_id, source_kind, unit_scope,
                      publication, acquisition, completion, blocks, normalized,
                      version, passages)


def validate_source_unit(unit: SourceUnit) -> SourceUnit:
    """Rebuild derived digests and spans rather than trusting caller-made objects."""
    if not isinstance(unit, SourceUnit) or type(unit.schema_version) is not int or unit.schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported source unit schema")
    expected = build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                                 unit.publication, unit.acquisition, unit.completion,
                                 unit.blocks)
    if unit != expected:
        raise ValueError("source version or passage does not match admitted structure")
    return unit


def source_unit_to_dict(unit: SourceUnit) -> dict:
    return json.loads(_canonical(asdict(validate_source_unit(unit))))


def source_unit_from_dict(value: dict) -> SourceUnit:
    """Parse exact saved v1 fields, then recalculate every derived binding."""
    fields = set(SourceUnit.__dataclass_fields__)
    if type(value) is not dict or set(value) != fields:
        raise ValueError("invalid saved source fields")
    try:
        def exact(data, cls):
            if type(data) is not dict or set(data) != set(cls.__dataclass_fields__):
                raise ValueError("invalid saved nested source fields")
            return cls(**data)

        def sequence(data, label):
            if type(data) is not list:
                raise ValueError(f"invalid saved {label}")
            return data

        pub = exact(value["publication"], PublicationProvenance)
        events = tuple(exact(event, DateEvidence) for event in sequence(pub.events, "date events"))
        events = tuple(DateEvidence(**{**asdict(event),
                         "conflicting_values": tuple(sequence(event.conflicting_values, "date conflicts"))})
                       for event in events)
        publication = PublicationProvenance(pub.publisher, pub.source_url, events)
        raw_recipe = exact(value["acquisition"], AcquisitionRecipe)
        members = tuple(exact(member, SourceMember)
                        for member in sequence(raw_recipe.members, "source members"))
        recipe = AcquisitionRecipe(**{**asdict(raw_recipe), "members": members})
        raw_completion = exact(value["completion"], CompletionEvidence)
        checks = tuple(exact(check, CompletionCheck)
                       for check in sequence(raw_completion.checks, "completion checks"))
        completion = CompletionEvidence(**{**asdict(raw_completion),
                         "required_member_ids": tuple(sequence(raw_completion.required_member_ids, "required members")),
                         "observed_member_ids": tuple(sequence(raw_completion.observed_member_ids, "observed members")),
                         "checks": checks})
        blocks = []
        for data in sequence(value["blocks"], "source blocks"):
            block = exact(data, SourceBlock)
            structure = block.structure
            if block.kind == "list":
                structure = exact(structure, ListPosition)
            elif block.kind == "table":
                grid = exact(structure, TableGrid)
                cells = tuple(exact(cell, TableCell)
                              for cell in sequence(grid.cells, "table cells"))
                structure = TableGrid(grid.rows, grid.columns, cells)
            elif structure is not None:
                raise ValueError("unexpected saved block structure")
            relations = tuple(exact(relation, BlockRelation)
                              for relation in sequence(block.relations, "block relations"))
            blocks.append(SourceBlock(block.block_id, block.kind, block.text,
                                      exact(block.source_locator, SourceLocator),
                                      structure, relations))
        unit = build_source_unit(value["article_id"], value["source_kind"],
                                 value["unit_scope"], publication, recipe, completion, tuple(blocks))
        if source_unit_to_dict(unit) != value:
            raise ValueError("saved source version differs from exact derivation")
        return unit
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid saved source shape") from exc

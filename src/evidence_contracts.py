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
                          "caption", "transcript_cue"})
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


@dataclass(frozen=True)
class PublicationProvenance:
    publisher: str
    source_url: str
    published_at: str
    date_precision: str
    date_authority: str


@dataclass(frozen=True)
class AcquisitionRecipe:
    mode: str
    client: str
    configuration_revision: str
    raw_representation_sha256: str
    decoding: str
    safe_header_evidence: str
    extraction_policy_revision: str
    member_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class CompletionEvidence:
    decision_id: str
    profile_revision: str
    confirmed_complete: bool
    represented_body_sha256: str


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


def _validate_inputs(article_id, source_kind, unit_scope, publication,
                     acquisition, completion, blocks):
    if type(article_id) is not str or not _ID.fullmatch(article_id):
        raise ValueError("invalid independent article identity")
    if source_kind not in _SOURCE_KINDS:
        raise ValueError("unsupported source kind")
    _nonempty(unit_scope, "unit scope")
    if not isinstance(publication, PublicationProvenance):
        raise ValueError("missing publication provenance")
    for name in ("publisher", "source_url", "date_precision", "date_authority"):
        _nonempty(getattr(publication, name), name)
    if type(publication.published_at) is not str or publication.date_precision not in ("day", "instant", "unknown"):
        raise ValueError("invalid original publication date")
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
    if (type(acquisition.member_sha256s) is not tuple
            or any(type(d) is not str or not _SHA256.fullmatch(d)
                   for d in acquisition.member_sha256s)):
        raise ValueError("invalid member digests")
    if not isinstance(completion, CompletionEvidence) or completion.confirmed_complete is not True:
        raise ValueError("source body has no complete admission decision")
    _nonempty(completion.decision_id, "completion decision")
    _nonempty(completion.profile_revision, "completion profile")
    _digest(completion.represented_body_sha256, "represented body digest")
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

        publication = exact(value["publication"], PublicationProvenance)
        recipe = exact(value["acquisition"], AcquisitionRecipe)
        recipe = AcquisitionRecipe(**{**asdict(recipe), "member_sha256s": tuple(recipe.member_sha256s)
                                       if type(recipe.member_sha256s) is list else recipe.member_sha256s})
        completion = exact(value["completion"], CompletionEvidence)
        blocks = tuple(SourceBlock(block["block_id"], block["kind"], block["text"],
                                   exact(block["source_locator"], SourceLocator))
                       for block in value["blocks"] if type(block) is dict and
                       set(block) == set(SourceBlock.__dataclass_fields__))
        if len(blocks) != len(value["blocks"]):
            raise ValueError("invalid saved source blocks")
        unit = build_source_unit(value["article_id"], value["source_kind"],
                                 value["unit_scope"], publication, recipe, completion, blocks)
        if source_unit_to_dict(unit) != value:
            raise ValueError("saved source version differs from exact derivation")
        return unit
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid saved source shape") from exc

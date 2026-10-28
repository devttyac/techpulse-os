"""TP11 typed complete-source handoff; only reviewed HTML profiles are routed.

The returned unit is a candidate until EvidenceStore.save repeats admission.
No source profile ships enabled and no other source kind borrows the HTML path.
"""

from dataclasses import dataclass
import hashlib
import json
from urllib.parse import urlsplit

from src.acquisition import readiness
from src.acquisition.html import extract_html
from src.acquisition.publication import assess_publication
from src.evidence_contracts import (AcquisitionRecipe, CompletionCheck,
                                    CompletionEvidence, SourceUnit, build_source_unit)


_HTML_KINDS = frozenset({"publisher_article"})


@dataclass(frozen=True)
class CompleteUnitResult:
    ready: bool
    reason: str
    unit: SourceUnit | None = None
    raw_representation: bytes | None = None
    members: tuple[bytes, ...] = ()


def _unready(reason):
    return CompleteUnitResult(False, reason)


def prepare_complete_unit(candidate, profile_id, article_id, window, zone_rules):
    """Recheck actual bytes, rules, rights, structure and date before persistence."""
    if candidate is None or candidate.reason != "validated":
        return _unready(getattr(candidate, "reason", "invalid_candidate"))
    fetched = candidate.fetch
    extracted = candidate.extraction
    if (fetched is None or extracted is None or fetched.reason != "fetched"
            or fetched.status != 200
            or type(fetched.raw_bytes) is not bytes or not fetched.raw_bytes
            or hashlib.sha256(fetched.raw_bytes).hexdigest() != fetched.raw_sha256
            or type(fetched.final_url) is not str or type(fetched.safe_headers) is not dict
            or type(fetched.decoding) is not str or not fetched.decoding
            or not set(fetched.safe_headers) <= {"content-type", "content-length"}
            or any(type(value) is not str or len(value) > 16384
                   for value in fetched.safe_headers.values())
            or not fetched.safe_headers.get("content-type", "").lower().startswith("text/html")
            or candidate.publication is None or not candidate.publication.eligible):
        return _unready("incomplete_candidate")
    try:
        host = urlsplit(fetched.final_url).hostname
    except ValueError:
        return _unready("invalid_source_url")
    registry = readiness._load_registry()
    if registry is None:
        return _unready("registry_unavailable")
    matching = [record for record in registry["profiles"]
                if type(record) is dict and type(record.get("profile")) is dict
                and record["profile"].get("profile_id") == profile_id
                and record["profile"].get("publisher_host") == host
                and record["profile"].get("revision") == fetched.profile_revision]
    if len(matching) != 1:
        return _unready("unregistered_profile")
    record = matching[0]
    profile = record["profile"]
    if profile.get("source_kind") == "technical_documentation":
        # One HTML article cannot establish a documentation page set. A
        # separately reviewed HTML page-set/context validator is required.
        return _unready("page_set_validator_unavailable")
    if profile.get("source_kind") not in _HTML_KINDS:
        return _unready("unsupported_source_kind")
    fixture = readiness.assess_profile_fixture(profile, record.get("current_binding"),
                                               record.get("fixture_observation"))
    if not fixture.fixture_policy_passed:
        return _unready(fixture.reason)
    if (not readiness._text_matches(record.get("rights_document"),
                                    profile["rights"]["evidence_sha256"])
            or not readiness._text_matches(record.get("fixture_raw_text"),
                                           profile["completeness"]["raw_sha256"])
            or not readiness._text_matches(record.get("fixture_body_text"),
                                           profile["completeness"]["body_sha256"])):
        return _unready("bound_evidence_mismatch")
    connector = profile["connector"]
    if (connector["id"] != "controlled-http-html" or fetched.client != connector["id"]
            or fetched.client_revision != connector["revision"]
            or type(profile.get("html_rules")) is not dict
            or profile["html_rules"].get("revision") != profile["structural_rule_revision"]
            or type(profile.get("date_rules")) is not dict
            or profile.get("date_rule_revision") != profile["date_rules"].get("revision")
            or profile["date_rules"].get("article_xpath")
               != profile["html_rules"].get("article_xpath")):
        return _unready("unreviewed_rules")
    checked = extract_html(fetched.raw_bytes, fetched.final_url, profile["html_rules"])
    if checked.reason != "validated" or checked.blocks != extracted.blocks:
        return _unready("wrong_body")
    publication = assess_publication(fetched.raw_bytes, fetched.final_url,
                                     profile["date_rules"], window, zone_rules,
                                     source_kind=profile["source_kind"])
    if not publication.eligible or publication != candidate.publication:
        return _unready(publication.reason if not publication.eligible else "date_mismatch")
    raw_sha = hashlib.sha256(fetched.raw_bytes).hexdigest()
    body_sha = hashlib.sha256("\n".join(block.text for block in checked.blocks).encode()).hexdigest()
    header_evidence = json.dumps(fetched.safe_headers, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False)
    acquisition = AcquisitionRecipe("controlled_http", fetched.client,
        fetched.client_revision, raw_sha, fetched.decoding,
        header_evidence, connector["extraction_policy_revision"], ())
    checks = tuple(CompletionCheck(check, "passed", checked.source_structure_ref)
                   for check in ("identity", "body_boundary", "continuation", "structure"))
    completion = CompletionEvidence("tp11-html-structural-v1", profile["revision"], True,
        body_sha, profile["completeness"]["unit_scope"], (), (), checks)
    try:
        unit = build_source_unit(article_id, profile["source_kind"],
            profile["completeness"]["unit_scope"], publication.provenance,
            acquisition, completion, checked.blocks)
    except (ValueError, TypeError, AttributeError):
        return _unready("invalid_complete_unit")
    admission = readiness.admit_source_unit(unit, fetched.raw_bytes, ())
    if not admission.admitted or admission.version_id != unit.version_id:
        return _unready(admission.reason)
    return CompleteUnitResult(True, admission.reason, unit, fetched.raw_bytes)

"""TP44 versioned frozen-source-profile fixture policy.

This pure gate checks reviewed evidence bindings. It does not verify the
truth of a publisher grant, fetch a page, enable a connector, or replace
per-article SourceUnit validation. A passing fixture is not permission to
retrieve or process any publisher article. No production profile ships here.
"""

from dataclasses import dataclass
import hashlib
from importlib import import_module
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from src.evidence_contracts import validate_source_unit


PROFILE_SCHEMA_VERSION = 1
_SHIPPED_REGISTRY_PATH = Path(__file__).with_name("profiles.json")
_REGISTRY_PATH = _SHIPPED_REGISTRY_PATH
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED_ACTIONS = frozenset({"automated_discovery", "full_body_retrieval", "storage", "processing"})
_SOURCE_KINDS = frozenset({"publisher_article", "technical_documentation", "research_paper",
                           "repository", "discussion", "publisher_transcript"})
_CONTROLLED_HTTP_CONNECTOR = "controlled-http-html"


@dataclass(frozen=True)
class ProfileFixtureDecision:
    fixture_policy_passed: bool
    reason: str


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    reason: str
    version_id: str | None = None


@dataclass(frozen=True)
class FetchProfileDecision:
    ready: bool
    reason: str
    profile_revision: str | None = None
    client: str | None = None
    client_revision: str | None = None


def authorize_fetch(profile_id: str, host: str) -> FetchProfileDecision:
    """Resolve a publisher request against the fixed, reviewed registry.

    The caller cannot supply a rights assertion or a profile object. This
    authorizes only an HTTP attempt; extraction and admission remain separate.
    """
    registry = _load_registry()
    if registry is None:
        return FetchProfileDecision(False, "registry_unavailable")
    matches = [record for record in registry["profiles"]
               if type(record) is dict and type(record.get("profile")) is dict
               and record["profile"].get("profile_id") == profile_id
               and record["profile"].get("publisher_host") == host]
    if len(matches) != 1:
        return FetchProfileDecision(False, "unregistered_profile")
    record = matches[0]
    profile = record["profile"]
    decision = assess_profile_fixture(profile, record.get("current_binding"),
                                      record.get("fixture_observation"))
    if not decision.fixture_policy_passed:
        return FetchProfileDecision(False, decision.reason)
    if profile["connector"]["id"] != _CONTROLLED_HTTP_CONNECTOR:
        return FetchProfileDecision(False, "unsupported_transport")
    if (not _text_matches(record.get("rights_document"),
                          profile["rights"]["evidence_sha256"])
            or not _text_matches(record.get("fixture_raw_text"),
                                 profile["completeness"]["raw_sha256"])
            or not _text_matches(record.get("fixture_body_text"),
                                 profile["completeness"]["body_sha256"])):
        return FetchProfileDecision(False, "bound_evidence_mismatch")
    return FetchProfileDecision(True, "ready", profile["revision"],
                                profile["connector"]["id"],
                                profile["connector"]["revision"])


def _decision(reason: str) -> ProfileFixtureDecision:
    return ProfileFixtureDecision(reason == "fixture_policy_passed", reason)


def _filled(value) -> bool:
    return type(value) is str and bool(value.strip()) and "\x00" not in value


def _digest(value) -> bool:
    return type(value) is str and bool(_DIGEST.fullmatch(value))


def _fields(record, names) -> bool:
    return type(record) is dict and all(_filled(record.get(name)) for name in names)


def assess_profile_fixture(profile: dict, current_binding: dict,
                           observation: dict) -> ProfileFixtureDecision:
    """Check only one frozen fixture against externally reviewed bindings.

    ``current_binding`` must come from an independently controlled registry
    before use in an admission path. This helper alone cannot authenticate its
    caller's record or reviewer names. The observation is a frozen full-source
    fixture, not arbitrary live content.
    ``fixture_policy_passed`` supports later profile evaluation only. It is
    neither a legal-rights determination nor a production enablement or
    individual-article admission decision. Those need independent evidence,
    owner approval, connector checks and per-article body validation.
    """
    if (type(profile) is not dict or type(profile.get("schema_version")) is not int
            or profile["schema_version"] != PROFILE_SCHEMA_VERSION
            or not _fields(profile, ("profile_id", "revision", "publisher_host",
                                     "source_kind", "structural_rule_revision"))
            or profile["source_kind"] not in _SOURCE_KINDS
            or not _fields(profile.get("connector"), ("id", "revision",
                                                     "extraction_policy_revision"))):
        return _decision("unknown_profile")

    if not _fields(current_binding, ("profile_id", "profile_revision", "connector_id",
                                     "connector_revision", "extraction_policy_revision",
                                     "structural_rule_revision",
                                     "rights_revision", "oracle_revision")):
        return _decision("stale_binding")
    if (profile["profile_id"] != current_binding["profile_id"]
            or profile["revision"] != current_binding["profile_revision"]
            or profile["connector"]["id"] != current_binding["connector_id"]
            or profile["connector"]["revision"] != current_binding["connector_revision"]
            or profile["connector"]["extraction_policy_revision"]
               != current_binding["extraction_policy_revision"]
            or profile["structural_rule_revision"]
               != current_binding["structural_rule_revision"]):
        return _decision("stale_binding")

    rights = profile.get("rights")
    if (not _fields(rights, ("revision", "decision", "grantor", "reviewer",
                             "evidence_ref", "publisher_host", "profile_revision",
                             "connector_revision"))
            or rights["decision"] != "approved"
            or not _digest(rights.get("evidence_sha256"))
            or rights["publisher_host"] != profile["publisher_host"]
            or type(rights.get("actions")) is not list
            or any(not _filled(action) for action in rights["actions"])
            or not _REQUIRED_ACTIONS <= set(rights["actions"])):
        return _decision("rights_unverified")
    if (rights["revision"] != current_binding["rights_revision"]
            or rights["profile_revision"] != profile["revision"]
            or rights["connector_revision"] != profile["connector"]["revision"]):
        return _decision("stale_binding")

    oracle = profile.get("completeness")
    if (not _fields(oracle, ("revision", "decision", "prepared_by", "reviewer",
                             "oracle_ref", "publisher_host", "source_kind",
                             "profile_revision", "connector_revision", "unit_scope"))
            or oracle["decision"] != "passed"
            or oracle["prepared_by"] == oracle["reviewer"]
            or oracle["publisher_host"] != profile["publisher_host"]
            or oracle["source_kind"] != profile["source_kind"]
            or not _digest(oracle.get("raw_sha256"))
            or not _digest(oracle.get("body_sha256"))
            or type(oracle.get("required_members")) is not list
            or not oracle["required_members"]
            or any(not _filled(member) for member in oracle["required_members"])
            or len(set(oracle["required_members"])) != len(oracle["required_members"])):
        return _decision("fixture_unverified")
    if (oracle["revision"] != current_binding["oracle_revision"]
            or oracle["profile_revision"] != profile["revision"]
            or oracle["connector_revision"] != profile["connector"]["revision"]):
        return _decision("stale_binding")

    if (not _fields(observation, ("publisher_host", "source_kind", "profile_revision",
                                  "connector_id", "connector_revision",
                                  "extraction_policy_revision", "content_state",
                                  "unit_scope"))
            or not _digest(observation.get("raw_sha256"))
            or not _digest(observation.get("body_sha256"))
            or type(observation.get("observed_members")) is not list
            or any(not _filled(member) for member in observation["observed_members"])):
        return _decision("invalid_evidence")
    if (observation["publisher_host"] != profile["publisher_host"]
            or observation["source_kind"] != profile["source_kind"]
            or observation["profile_revision"] != profile["revision"]
            or observation["connector_id"] != profile["connector"]["id"]
            or observation["connector_revision"] != profile["connector"]["revision"]
            or observation["extraction_policy_revision"]
               != profile["connector"]["extraction_policy_revision"]):
        return _decision("connector_mismatch")
    if observation["content_state"] == "preview":
        return _decision("preview")
    if observation["content_state"] != "full_body":
        return _decision("incomplete_unit")
    if (observation["unit_scope"] != oracle["unit_scope"]
            or observation["observed_members"] != oracle["required_members"]):
        return _decision("incomplete_unit")
    if observation["raw_sha256"] != oracle["raw_sha256"]:
        return _decision("fixture_unverified")
    if observation["body_sha256"] != oracle["body_sha256"]:
        return _decision("wrong_body")
    return _decision("fixture_policy_passed")


def _admission(reason, unit=None):
    return AdmissionDecision(reason in ("admitted", "admitted_synthetic_fixture"), reason,
                             unit.version_id if reason in ("admitted", "admitted_synthetic_fixture") else None)


def _load_registry():
    """Read the fixed repository registry; no caller-supplied trust record."""
    try:
        registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (type(registry) is not dict or type(registry.get("schema_version")) is not int
            or registry["schema_version"] != 1
            or type(registry.get("profiles")) is not list
            or (registry.get("synthetic_test_only") is True
                and _REGISTRY_PATH == _SHIPPED_REGISTRY_PATH)):
        return None
    return registry


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_matches(value, digest) -> bool:
    return type(value) is str and _digest(digest) and _sha(value.encode("utf-8")) == digest


def admit_source_unit(unit, raw_representation, members=()) -> AdmissionDecision:
    """Require repository-bound evidence and exact received bytes before save.

    A profile/rights/fixture record must be independently reviewed before it
    is placed in source control. This function binds the saved bytes and
    revisions; it cannot decide whether legal terms truly grant permission.
    Daily article text is never a registry oracle. TP09 must supply an
    independently authored structural validator that checks each actual raw
    source against its current DOM/body rules. Until it exists, dynamic
    articles are denied rather than trusting SourceUnit's own completion flag.
    The shipped registry has no profiles, so production currently admits none.
    """
    try:
        validate_source_unit(unit)
    except (ValueError, TypeError, AttributeError):
        return _admission("invalid_source_unit")
    if (type(raw_representation) is not bytes
            or _sha(raw_representation) != unit.acquisition.raw_representation_sha256):
        return _admission("raw_mismatch")
    if (type(members) is not tuple or len(members) != len(unit.acquisition.members)
            or any(type(body) is not bytes or _sha(body) != member.sha256
                   for body, member in zip(members, unit.acquisition.members))):
        return _admission("incomplete_unit")
    if unit.source_kind == "technical_documentation":
        # The HTML structural slot checks one page only. A page-set member and
        # context validator is required before this kind can enter storage.
        return _admission("page_set_validator_unavailable")

    registry = _load_registry()
    if registry is None:
        return _admission("registry_unavailable")
    try:
        host = urlsplit(unit.publication.source_url).hostname
    except ValueError:
        return _admission("invalid_source_unit")
    matching = [record for record in registry["profiles"]
                if type(record) is dict and type(record.get("profile")) is dict
                and record["profile"].get("revision") == unit.completion.profile_revision
                and record["profile"].get("publisher_host") == host
                and record["profile"].get("source_kind") == unit.source_kind]
    if len(matching) != 1:
        return _admission("unregistered_profile")
    record = matching[0]
    profile = record["profile"]
    decision = assess_profile_fixture(profile, record.get("current_binding"),
                                      record.get("fixture_observation"))
    if not decision.fixture_policy_passed:
        return _admission(decision.reason)
    if (not _text_matches(record.get("rights_document"),
                          profile["rights"]["evidence_sha256"])
            or not _text_matches(record.get("fixture_raw_text"),
                                 profile["completeness"]["raw_sha256"])
            or not _text_matches(record.get("fixture_body_text"),
                                 profile["completeness"]["body_sha256"])):
        return _admission("bound_evidence_mismatch")
    if (unit.source_kind == "publisher_article"
            and profile["connector"]["id"] == _CONTROLLED_HTTP_CONNECTOR
            and unit.acquisition.mode != "controlled_http"):
        return _admission("unsupported_transport")
    if (unit.acquisition.client != profile["connector"]["id"]
            or unit.acquisition.configuration_revision != profile["connector"]["revision"]
            or unit.acquisition.extraction_policy_revision
               != profile["connector"]["extraction_policy_revision"]):
        return _admission("connector_mismatch")
    validator = _load_structural_validator(unit.source_kind)
    if validator is None:
        return _admission("structural_validator_unavailable")
    try:
        structural = validator(profile, unit, raw_representation, members)
    except Exception:
        return _admission("structural_validation_failed")
    if (not _fields(structural, ("decision", "rule_revision", "source_structure_ref",
                                 "source_url", "unit_scope"))
            or structural["decision"] != "complete"
            or structural["rule_revision"] != profile["structural_rule_revision"]
            or structural["source_url"] != unit.publication.source_url
            or structural["unit_scope"] != unit.unit_scope
            or not _digest(structural.get("raw_sha256"))
            or not _digest(structural.get("body_sha256"))
            or type(structural.get("member_ids")) is not list
            or type(structural.get("member_sha256s")) is not list
            or type(structural.get("checks")) is not list
            or not structural["checks"]
            or any(not _filled(check) for check in structural["checks"])):
        return _admission("structural_validation_failed")
    if (structural["member_ids"] != [member.member_id for member in unit.acquisition.members]
            or structural["member_sha256s"] != [_sha(body) for body in members]):
        return _admission("incomplete_unit")
    if structural["raw_sha256"] != _sha(raw_representation):
        return _admission("raw_mismatch")
    body = "\n".join(block.text for block in unit.blocks).encode("utf-8")
    if structural["body_sha256"] != _sha(body):
        return _admission("wrong_body")
    return _admission("admitted_synthetic_fixture" if registry.get("synthetic_test_only") is True
                      else "admitted", unit)


def _load_structural_validator(source_kind):
    """TP09 adapter slot; no built-in fallback or extractor-self oracle.

    The future module must inspect the actual raw source and return a
    decision, source-structure location, rule revision and byte bindings.
    Reviewed development fixtures validate its rules, not daily prose.
    """
    if source_kind not in {"publisher_article", "technical_documentation"}:
        return None
    try:
        module = import_module("src.acquisition.html_validation")
    except ImportError:
        return None
    validator = getattr(module, "validate_source_structure", None)
    return validator if callable(validator) else None

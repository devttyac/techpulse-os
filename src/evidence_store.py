"""TP03 exact complete-source retrieval through TP02 immutable StateStore."""

import hashlib
import json
import re

from src.evidence_contracts import source_unit_from_dict, source_unit_to_dict
from src.acquisition.readiness import admit_source_unit


_VERSION = re.compile(r"[0-9a-f]{64}\Z")
_KIND = "complete-source-unit-v1"


class EvidenceUnavailable(ValueError):
    """The requested complete source version or supported passage is absent."""


class EvidenceStore:
    def __init__(self, state_store):
        self._state = state_store

    def _representation(self, artifact_id, version_id, digest, ordinal, identity):
        result = self._state.read_artifact(artifact_id)
        if result is None:
            raise EvidenceUnavailable("required source representation unavailable")
        body, metadata = result
        if (metadata != {"kind": "source-representation-v1", "version_id": version_id,
                         "ordinal": ordinal, "identity": identity, "digest": digest}
                or hashlib.sha256(body).hexdigest() != digest):
            raise EvidenceUnavailable("source representation binding mismatch")
        return body

    def _save_representation(self, artifact_id, version_id, digest, ordinal, identity, body):
        existing = self._state.read_artifact(artifact_id)
        if existing is not None:
            if self._representation(artifact_id, version_id, digest, ordinal, identity) != body:
                raise EvidenceUnavailable("existing source representation differs")
            return
        self._state.put_artifact(artifact_id, "source-representation-v1", body,
                                 {"kind": "source-representation-v1", "version_id": version_id,
                                  "ordinal": ordinal, "identity": identity, "digest": digest})

    def _load_representations(self, unit):
        version_id = unit.version_id
        raw = self._representation("source-raw:" + version_id, version_id,
                                   unit.acquisition.raw_representation_sha256, "raw", "raw")
        members = tuple(self._representation(f"source-member:{version_id}:{index}",
                                             version_id, member.sha256, index, member.member_id)
                        for index, member in enumerate(unit.acquisition.members))
        return raw, members

    def save(self, unit, raw_representation, members=()):
        """Flush verified raw/member bytes before exposing the normalized unit.

        The fixed profile registry and independent structural validator must
        admit this exact unit and received bytes before any artifact write.
        A later failed write can leave unreferenced representation artifacts.
        It can never leave an admitted unit with unavailable source bytes.
        """
        value = source_unit_to_dict(unit)
        if (type(raw_representation) is not bytes
                or hashlib.sha256(raw_representation).hexdigest()
                != unit.acquisition.raw_representation_sha256):
            raise ValueError("raw representation digest mismatch")
        if (type(members) is not tuple or len(members) != len(unit.acquisition.members)
                or any(type(body) is not bytes or hashlib.sha256(body).hexdigest() != member.sha256
                       for body, member in zip(members, unit.acquisition.members))):
            raise ValueError("member representation digest mismatch")
        admission = admit_source_unit(unit, raw_representation, members)
        if not admission.admitted or admission.version_id != unit.version_id:
            raise ValueError("source admission denied: " + admission.reason)
        artifact_id = "source-unit:" + unit.version_id
        if self._state.read_artifact(artifact_id) is not None:
            raise ValueError("source version already exists")
        self._save_representation("source-raw:" + unit.version_id, unit.version_id,
                                  unit.acquisition.raw_representation_sha256, "raw", "raw",
                                  raw_representation)
        for index, (member, body) in enumerate(zip(unit.acquisition.members, members)):
            self._save_representation(f"source-member:{unit.version_id}:{index}",
                                      unit.version_id, member.sha256, index, member.member_id, body)
        body = json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        return self._state.put_artifact(artifact_id, _KIND, body,
                                        {"schema_version": 1, "kind": _KIND,
                                         "version_id": unit.version_id,
                                         "article_id": unit.article_id})

    def read(self, version_id):
        if type(version_id) is not str or not _VERSION.fullmatch(version_id):
            raise EvidenceUnavailable("invalid source version")
        result = self._state.read_artifact("source-unit:" + version_id)
        if result is None:
            raise EvidenceUnavailable("complete source version unavailable")
        body, metadata = result
        try:
            value = json.loads(body)
            unit = source_unit_from_dict(value)
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            raise EvidenceUnavailable("saved source unit invalid") from exc
        if (metadata != {"schema_version": 1, "kind": _KIND,
                         "version_id": version_id, "article_id": unit.article_id}
                or unit.version_id != version_id):
            raise EvidenceUnavailable("saved source identity mismatch")
        self._load_representations(unit)
        return unit

    def representations(self, version_id):
        """Return the actual bound received bytes, including all members."""
        return self._load_representations(self.read(version_id))

    def passage(self, passage_id):
        if type(passage_id) is not str or ":" not in passage_id:
            raise EvidenceUnavailable("invalid passage identity")
        version_id = passage_id.split(":", 1)[0]
        unit = self.read(version_id)
        for passage in unit.passages:
            if passage.passage_id == passage_id:
                return passage
        raise EvidenceUnavailable("passage not in saved source version")

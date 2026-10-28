"""TP03 immutable typed source storage and legacy/new-story boundary."""

from dataclasses import replace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evidence_contracts import DateEvidence, SourceBlock, SourceLocator, SourceMember, build_source_unit
from src.evidence_store import EvidenceStore, EvidenceUnavailable
from src.state_store import StateStore, StoreIntegrityError
from src.story_manifest import require_complete_story_sources
from test_evidence_contracts import complete_unit, completion_for
from test_source_readiness import FIXTURE


RAW_BYTES = b"publisher response"


class CompleteSourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="techpulse-evidence-")
        self.root = Path(self.temp.name) / "retained"

    def tearDown(self):
        self.temp.cleanup()

    def save_synthetic(self, evidence, unit, raw, members=(), mutate_registry=None):
        """Storage regression setup only; review/admission oracles live elsewhere."""
        profile = deepcopy(FIXTURE["profile"])
        binding = deepcopy(FIXTURE["current_binding"])
        observation = deepcopy(FIXTURE["observation"])
        profile["revision"] = unit.completion.profile_revision
        profile["source_kind"] = unit.source_kind
        profile["structural_rule_revision"] = "synthetic-storage-structure-v1"
        profile["connector"] = {"id": unit.acquisition.client,
                                "revision": unit.acquisition.configuration_revision,
                                "extraction_policy_revision": unit.acquisition.extraction_policy_revision}
        profile["rights"].update(
            evidence_sha256="638fdebf06f65e788c9727d2df2eca2dd842396dac7243cf2bb79620e13d1853",
            profile_revision=unit.completion.profile_revision,
            connector_revision=unit.acquisition.configuration_revision)
        profile["completeness"].update(source_kind=unit.source_kind,
                                       profile_revision=unit.completion.profile_revision,
                                       connector_revision=unit.acquisition.configuration_revision)
        binding.update(profile_revision=unit.completion.profile_revision,
                       connector_id=unit.acquisition.client,
                       connector_revision=unit.acquisition.configuration_revision,
                       extraction_policy_revision=unit.acquisition.extraction_policy_revision,
                       structural_rule_revision="synthetic-storage-structure-v1")
        observation.update(source_kind=unit.source_kind,
                           profile_revision=unit.completion.profile_revision,
                           connector_id=unit.acquisition.client,
                           connector_revision=unit.acquisition.configuration_revision,
                           extraction_policy_revision=unit.acquisition.extraction_policy_revision)
        record = {"current_binding": binding, "profile": profile,
                  "rights_document": FIXTURE["synthetic_rights_document"],
                  "fixture_raw_text": FIXTURE["raw_fixture_text"],
                  "fixture_body_text": FIXTURE["expected_complete_body"],
                  "fixture_observation": observation}
        registry = {"schema_version": 1, "synthetic_test_only": True, "profiles": [record]}
        if mutate_registry is not None:
            mutate_registry(registry)
        path = Path(self.temp.name) / "synthetic-profiles.json"
        path.write_text(json.dumps(registry))
        # This stub tests storage after an admission; it is not a TP09 oracle.
        receipt = {"decision": "complete", "rule_revision": "synthetic-storage-structure-v1",
                   "source_structure_ref": "synthetic://storage-regression-only",
                   "source_url": unit.publication.source_url, "unit_scope": unit.unit_scope,
                   "raw_sha256": hashlib.sha256(raw).hexdigest(),
                   "body_sha256": unit.completion.represented_body_sha256,
                   "member_ids": [item.member_id for item in unit.acquisition.members],
                   "member_sha256s": [item.sha256 for item in unit.acquisition.members],
                   "checks": ["synthetic-storage-only"]}
        with patch("src.acquisition.readiness._REGISTRY_PATH", path), patch(
                "src.acquisition.readiness._load_structural_validator",
                return_value=lambda *args: receipt):
            return evidence.save(unit, raw, members)

    def test_shipped_empty_registry_denies_save_before_any_artifact(self):
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            with self.assertRaisesRegex(ValueError, "unregistered_profile"):
                EvidenceStore(state).save(unit, RAW_BYTES)
            self.assertEqual(state.list_artifact_ids(), [])

    def test_typed_unready_candidate_cannot_create_a_source_artifact(self):
        from src.acquisition.publication import unavailable
        from src.ingestion import FetchPublicationCandidate, admit_assessed_candidate
        candidate = FetchPublicationCandidate("preview", None, None,
                                              unavailable("preview"))
        with StateStore.create(self.root) as state:
            result = admit_assessed_candidate(candidate, "alder-article", "article-1",
                                              None, None, EvidenceStore(state))
            self.assertFalse(result.admitted)
            self.assertEqual(result.reason, "preview")
            self.assertEqual(state.list_artifact_ids(), [])

    def test_tampered_rights_evidence_denies_save_before_any_artifact(self):
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            with self.assertRaisesRegex(ValueError, "bound_evidence_mismatch"):
                self.save_synthetic(EvidenceStore(state), unit, RAW_BYTES,
                    mutate_registry=lambda r: r["profiles"][0].update(
                        rights_document="tampered synthetic grant"))
            self.assertEqual(state.list_artifact_ids(), [])

    def test_exact_version_and_passage_survive_restart_and_publisher_edit(self):
        first = complete_unit()
        edited = complete_unit(text="Alder updated PineDB. Migration is stable.")
        with StateStore.create(self.root) as state:
            evidence = EvidenceStore(state)
            self.save_synthetic(evidence, first, RAW_BYTES)
            self.save_synthetic(evidence, edited, RAW_BYTES)
        with StateStore.open(self.root) as state:
            evidence = EvidenceStore(state)
            self.assertEqual(evidence.read(first.version_id), first)
            self.assertEqual(evidence.read(edited.version_id), edited)
            self.assertEqual(evidence.passage(first.passages[0].passage_id), first.passages[0])
            self.assertNotEqual(evidence.passage(first.passages[0].passage_id).text,
                                edited.passages[0].text)

    def test_absent_raw_only_and_corrupt_units_fail_closed(self):
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            evidence = EvidenceStore(state)
            with self.assertRaises(EvidenceUnavailable):
                evidence.read(unit.version_id)
            state.put_artifact("source-unit:" + unit.version_id, "raw-html", b"<p>preview</p>",
                               {"schema_version": 1, "version_id": unit.version_id})
            with self.assertRaises(EvidenceUnavailable):
                evidence.read(unit.version_id)
        with StateStore.open(self.root) as state:
            with self.assertRaises(EvidenceUnavailable):
                EvidenceStore(state).read(unit.version_id)

    def test_missing_or_changed_saved_blob_never_resolves_complete_evidence(self):
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            digest = self.save_synthetic(EvidenceStore(state), unit, RAW_BYTES)
        blob = self.root / "objects" / digest
        blob.write_bytes(b"corrupt")
        with StateStore.open(self.root) as state:
            with self.assertRaises(StoreIntegrityError):
                EvidenceStore(state).read(unit.version_id)
        blob.unlink()
        with StateStore.open(self.root) as state:
            with self.assertRaises(StoreIntegrityError):
                EvidenceStore(state).read(unit.version_id)

    def test_legacy_excerpt_or_url_only_cannot_enter_complete_story_consumer(self):
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            evidence = EvidenceStore(state)
            self.save_synthetic(evidence, unit, RAW_BYTES)
            with self.assertRaises(ValueError):
                require_complete_story_sources({"source_url": unit.publication.source_url,
                                                "source_summary": "RSS excerpt"}, evidence)
            with self.assertRaises(ValueError):
                require_complete_story_sources({"schema_version": 1, "stories": []}, evidence)
            refs = {"schema_version": 2, "story_id": "story-1", "sources": [
                {"version_id": unit.version_id,
                 "passage_ids": [unit.passages[0].passage_id]}]}
            self.assertEqual(require_complete_story_sources(refs, evidence).sources[0].unit, unit)
            wrong = json.loads(json.dumps(refs))
            wrong["sources"][0]["passage_ids"] = ["not-a-passage"]
            with self.assertRaises(EvidenceUnavailable):
                require_complete_story_sources(wrong, evidence)

    def test_saved_raw_and_member_bytes_are_retrievable_and_required_after_restart(self):
        import hashlib
        unit = complete_unit()
        member = b"figure caption source bytes"
        recipe = replace(unit.acquisition, members=(SourceMember("asset-1", "asset",
                                                               "figure-caption", hashlib.sha256(member).hexdigest()),))
        completion = replace(unit.completion, required_member_ids=("asset-1",),
                             observed_member_ids=("asset-1",))
        unit = build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
                                 unit.publication, recipe, completion, unit.blocks)
        with StateStore.create(self.root) as state:
            self.save_synthetic(EvidenceStore(state), unit, RAW_BYTES, (member,))
        with StateStore.open(self.root) as state:
            evidence = EvidenceStore(state)
            self.assertEqual(evidence.read(unit.version_id), unit)
            self.assertEqual(evidence.representations(unit.version_id), (RAW_BYTES, (member,)))
        (self.root / "objects" / hashlib.sha256(member).hexdigest()).unlink()
        with StateStore.open(self.root) as state:
            with self.assertRaises(StoreIntegrityError):
                EvidenceStore(state).read(unit.version_id)

    def test_missing_raw_representation_blocks_complete_source_read(self):
        import hashlib
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            self.save_synthetic(EvidenceStore(state), unit, RAW_BYTES)
        (self.root / "objects" / hashlib.sha256(RAW_BYTES).hexdigest()).unlink()
        with StateStore.open(self.root) as state:
            with self.assertRaises(StoreIntegrityError):
                EvidenceStore(state).read(unit.version_id)

    def test_failed_raw_install_leaves_no_admitted_complete_unit(self):
        from unittest.mock import patch
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            with patch("src.state_store.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    self.save_synthetic(EvidenceStore(state), unit, RAW_BYTES)
            with self.assertRaises(EvidenceUnavailable):
                EvidenceStore(state).read(unit.version_id)

    def test_raw_digest_mismatch_cannot_create_complete_artifact(self):
        unit = complete_unit()
        with StateStore.create(self.root) as state:
            evidence = EvidenceStore(state)
            with self.assertRaises(ValueError):
                evidence.save(unit, b"wrong received bytes")
            self.assertEqual(state.list_artifact_ids(), [])
            with self.assertRaises(EvidenceUnavailable):
                evidence.read(unit.version_id)

    def test_complete_story_result_retains_selected_passages_and_locators(self):
        unit = complete_unit()
        other = complete_unit(text="A later correction to the original story.")
        with StateStore.create(self.root) as state:
            evidence = EvidenceStore(state)
            self.save_synthetic(evidence, unit, RAW_BYTES)
            self.save_synthetic(evidence, other, RAW_BYTES)
            refs = {"schema_version": 2, "story_id": "story-1", "sources": [
                {"version_id": unit.version_id,
                 "passage_ids": [unit.passages[0].passage_id]}]}
            bound = require_complete_story_sources(refs, evidence)
            self.assertEqual(bound.story_id, "story-1")
            self.assertEqual(bound.sources[0].version_id, unit.version_id)
            self.assertEqual(bound.sources[0].passages, unit.passages)
            self.assertEqual(bound.sources[0].passages[0].source_locator,
                             unit.passages[0].source_locator)
            refs["sources"][0]["passage_ids"] = [other.passages[0].passage_id]
            with self.assertRaises(ValueError):
                require_complete_story_sources(refs, evidence)

    def test_unknown_media_release_cannot_be_replaced_by_new_transcript_date_for_story(self):
        base = complete_unit()
        events = (
            DateEvidence("original_media_release", "unknown", "", "", "unknown", "",
                         "unknown", (), "release not established"),
            DateEvidence("transcript_publication", "known", "2026-10-14", "transcript-page",
                         "day", "", "Alder Labs", (), ""),
        )
        unit = build_source_unit(base.article_id, "publisher_transcript", "whole_transcript",
                                 replace(base.publication, events=events), base.acquisition,
                                 completion_for(base, "publisher_transcript", "whole_transcript"),
                                 (SourceBlock("cue-1", "transcript_cue", base.blocks[0].text,
                                              SourceLocator("transcript", {})),))
        with StateStore.create(self.root) as state:
            evidence = EvidenceStore(state)
            self.save_synthetic(evidence, unit, RAW_BYTES)
            self.assertEqual(evidence.read(unit.version_id).publication.events[0].status, "unknown")
            with self.assertRaises(ValueError):
                require_complete_story_sources({"schema_version": 2, "story_id": "story-1",
                    "sources": [{"version_id": unit.version_id,
                                 "passage_ids": [unit.passages[0].passage_id]}]}, evidence)


if __name__ == "__main__":
    unittest.main()

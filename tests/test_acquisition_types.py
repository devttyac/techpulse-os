"""TP11 typed source admission uses only invented offline publisher evidence."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.http import FetchResult
from src.run_coordinator import capture_zone_rules, frozen_window
from src.state_store import StateStore
from src.evidence_store import EvidenceStore
from test_publication import date_rules
from test_source_readiness import FIXTURE


HTML = json.loads((Path(__file__).parent / "fixtures" /
                   "html_extraction_cases.json").read_text())


class TypedAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tp11-synthetic-")
        self.addCleanup(self.temp.cleanup)
        profile = deepcopy(FIXTURE["profile"])
        profile["rights"]["evidence_sha256"] = hashlib.sha256(
            FIXTURE["synthetic_rights_document"].encode()).hexdigest()
        self.registry = {"schema_version": 1, "synthetic_test_only": True, "profiles": [{
            "profile": profile,
            "current_binding": deepcopy(FIXTURE["current_binding"]),
            "fixture_observation": deepcopy(FIXTURE["observation"]),
            "rights_document": FIXTURE["synthetic_rights_document"],
            "fixture_raw_text": FIXTURE["raw_fixture_text"],
            "fixture_body_text": FIXTURE["expected_complete_body"],
        }]}
        profile["html_rules"] = HTML["rules"]
        profile["date_rule_revision"] = "date-v1"
        profile["date_rules"] = date_rules()
        self.raw = HTML["short_complete"].replace(
            "<div class='story-body'>",
            "<header><time data-event='publisher_publication' data-owner='alder.example' "
            "datetime='2026-10-06'></time></header><div class='story-body'>").encode()
        self.rules = capture_zone_rules("Asia/Singapore")
        self.window = frozen_window(datetime.fromisoformat("2026-10-08T10:00:00+00:00"),
                                    self.rules)

    def candidate(self, raw=None, registry=None):
        from src.ingestion import fetch_and_assess_candidate
        raw = self.raw if raw is None else raw
        fetched = FetchResult("fetched", raw, hashlib.sha256(raw).hexdigest(),
                              HTML["source_url"], 200, {"content-type": "text/html"},
                              "controlled-http-html", "connector-2", "profile-3",
                              "utf-8", "203.0.113.10")
        with patch("src.ingestion.fetch_candidate", return_value=fetched), patch(
                "src.ingestion._load_registry", return_value=registry or self.registry):
            return asyncio.run(fetch_and_assess_candidate(
                HTML["source_url"], "alder-article", self.window, self.rules))

    def test_html_candidate_builds_typed_complete_unit_and_store_admits(self):
        from src.acquisition.types import prepare_complete_unit
        from src.ingestion import admit_assessed_candidate
        candidate = self.candidate()
        self.assertEqual(candidate.reason, "validated")
        self.assertTrue(candidate.publication.eligible)
        with patch("src.acquisition.readiness._load_registry", return_value=self.registry):
            prepared = prepare_complete_unit(candidate, "alder-article", "alder-story",
                                             self.window, self.rules)
            self.assertTrue(prepared.ready, prepared.reason)
            self.assertEqual(prepared.unit.acquisition.raw_representation_sha256,
                             hashlib.sha256(self.raw).hexdigest())
            self.assertEqual(prepared.unit.publication.events,
                             candidate.publication.provenance.events)
            self.assertEqual(json.loads(prepared.unit.acquisition.safe_header_evidence),
                             {"content-type": "text/html"})
            with StateStore.create(Path(self.temp.name) / "retained") as state:
                result = admit_assessed_candidate(candidate, "alder-article", "alder-story",
                                                  self.window, self.rules, EvidenceStore(state))
                self.assertTrue(result.admitted, result.reason)
                self.assertEqual(result.version_id, prepared.unit.version_id)
                self.assertEqual(EvidenceStore(state).representations(result.version_id)[0],
                                 self.raw)

    def test_unknown_kind_is_unready_without_html_fallback(self):
        from src.acquisition.types import prepare_complete_unit
        candidate = self.candidate()
        registry = deepcopy(self.registry)
        registry["profiles"][0]["profile"]["source_kind"] = "repository"
        with patch("src.acquisition.readiness._load_registry", return_value=registry):
            result = prepare_complete_unit(candidate, "alder-article", "alder-story",
                                           self.window, self.rules)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "unsupported_source_kind")

    def test_documentation_page_set_remains_unready_and_saves_no_artifacts(self):
        from src.acquisition.types import prepare_complete_unit
        from src.ingestion import admit_assessed_candidate
        registry = deepcopy(self.registry)
        record = registry["profiles"][0]
        record["profile"]["source_kind"] = "technical_documentation"
        record["profile"]["completeness"]["source_kind"] = "technical_documentation"
        record["fixture_observation"]["source_kind"] = "technical_documentation"
        candidate = self.candidate(registry=registry)
        self.assertEqual(candidate.reason, "validated")
        self.assertTrue(candidate.publication.eligible)
        with patch("src.acquisition.readiness._load_registry", return_value=registry):
            prepared = prepare_complete_unit(candidate, "alder-article", "alder-doc",
                                             self.window, self.rules)
            self.assertFalse(prepared.ready)
            self.assertEqual(prepared.reason, "page_set_validator_unavailable")
            self.assertIsNone(prepared.unit)
            with StateStore.create(Path(self.temp.name) / "retained") as state:
                result = admit_assessed_candidate(candidate, "alder-article", "alder-doc",
                    self.window, self.rules, EvidenceStore(state))
                self.assertFalse(result.admitted)
                self.assertEqual(result.reason, "page_set_validator_unavailable")
                self.assertEqual(state.list_artifact_ids(), [])

    def test_shipped_empty_registry_denies_before_store_write(self):
        from src.ingestion import admit_assessed_candidate
        candidate = self.candidate()
        with StateStore.create(Path(self.temp.name) / "retained") as state:
            result = admit_assessed_candidate(candidate, "alder-article", "alder-story",
                                              self.window, self.rules, EvidenceStore(state))
            self.assertFalse(result.admitted)
            self.assertEqual(result.reason, "unregistered_profile")
            self.assertEqual(state.list_artifact_ids(), [])

    def test_preview_and_ineligible_date_never_reach_store(self):
        from src.ingestion import admit_assessed_candidate
        preview = self.candidate(HTML["preview"].encode())
        old = self.raw.replace(b"2026-10-06", b"2026-09-06")
        dated_out = self.candidate(old)
        with patch("src.acquisition.readiness._load_registry", return_value=self.registry):
            with StateStore.create(Path(self.temp.name) / "retained") as state:
                for candidate in (preview, dated_out):
                    result = admit_assessed_candidate(candidate, "alder-article",
                        "alder-story", self.window, self.rules, EvidenceStore(state))
                    self.assertFalse(result.admitted)
                self.assertEqual(state.list_artifact_ids(), [])

    def test_changed_bytes_body_or_publication_cannot_reuse_a_valid_candidate(self):
        from src.acquisition.types import prepare_complete_unit
        candidate = self.candidate()
        cases = (
            replace(candidate, fetch=replace(candidate.fetch, raw_bytes=b"different article")),
            replace(candidate, fetch=replace(candidate.fetch, status=404)),
            replace(candidate, fetch=replace(candidate.fetch, decoding=None)),
            replace(candidate, extraction=replace(candidate.extraction, blocks=())),
            replace(candidate, publication=replace(candidate.publication, reason="forged")),
        )
        with patch("src.acquisition.readiness._load_registry", return_value=self.registry):
            for changed in cases:
                with self.subTest(changed=changed):
                    result = prepare_complete_unit(changed, "alder-article", "alder-story",
                                                   self.window, self.rules)
                    self.assertFalse(result.ready)

    def test_missing_rights_evidence_denies_actual_html_even_when_structure_passes(self):
        from src.acquisition.types import prepare_complete_unit
        candidate = self.candidate()
        registry = deepcopy(self.registry)
        registry["profiles"][0]["rights_document"] = "changed grant"
        with patch("src.acquisition.readiness._load_registry", return_value=registry):
            result = prepare_complete_unit(candidate, "alder-article", "alder-story",
                                           self.window, self.rules)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "bound_evidence_mismatch")


if __name__ == "__main__":
    unittest.main()

"""TP44 source-profile policy cases use a frozen, wholly synthetic oracle."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.readiness import assess_profile_fixture, admit_source_unit, authorize_fetch
from src.acquisition.html import extract_html
from src.evidence_contracts import CompletionCheck, SourceLocator, build_source_unit
from test_evidence_contracts import complete_unit


FIXTURE = json.loads((Path(__file__).parent / "fixtures" /
                      "source_readiness_synthetic.json").read_text())


class SourceReadinessTests(unittest.TestCase):
    def case(self):
        return (deepcopy(FIXTURE["profile"]), deepcopy(FIXTURE["current_binding"]),
                deepcopy(FIXTURE["observation"]))

    def assert_reason(self, expected, profile, binding, observation):
        decision = assess_profile_fixture(profile, binding, observation)
        self.assertEqual(decision.reason, expected)
        self.assertEqual(decision.fixture_policy_passed, expected == "fixture_policy_passed")

    def test_invented_fixture_passes_structural_policy_only(self):
        self.assertIn("Invented", FIXTURE["fixture_notice"])
        self.assertEqual(FIXTURE["expected_complete_body"],
                         "Alder Labs introduced PineDB 2.0. Migration remains beta.")
        for text_key, expected in (("raw_fixture_text", "942aeeae5284bbadf65b905c1e97b9c12ec5d341e5446aabcd2c21cb346a4061"),
                                   ("expected_complete_body", "8c088e3d5fbb86b7dffbf9e9faf2c2222269e360ea3ce4267cbd682671489734")):
            self.assertEqual(hashlib.sha256(FIXTURE[text_key].encode()).hexdigest(), expected)
        self.assert_reason("fixture_policy_passed", *self.case())

    def test_missing_or_unapproved_rights_fail_closed(self):
        for change in (lambda p: p.pop("rights"),
                       lambda p: p["rights"].update(decision="unknown"),
                       lambda p: p["rights"].update(evidence_ref=""),
                       lambda p: p["rights"].update(actions=["automated_discovery"]),
                       lambda p: p["rights"].update(publisher_host="elsewhere.example")):
            with self.subTest(change=change):
                profile, binding, observation = self.case()
                change(profile)
                self.assert_reason("rights_unverified", profile, binding, observation)

    def test_preview_and_wrong_body_are_distinct_failures(self):
        profile, binding, observation = self.case()
        observation["content_state"] = "preview"
        observation["body_sha256"] = hashlib.sha256(FIXTURE["preview_body"].encode()).hexdigest()
        self.assert_reason("preview", profile, binding, observation)
        observation["content_state"] = "full_body"
        observation["body_sha256"] = hashlib.sha256(FIXTURE["wrong_article_body"].encode()).hexdigest()
        self.assert_reason("wrong_body", profile, binding, observation)

    def test_missing_continuation_cannot_pass_as_complete_unit(self):
        profile, binding, observation = self.case()
        observation["observed_members"] = ["main"]
        self.assert_reason("incomplete_unit", profile, binding, observation)
        profile, binding, observation = self.case()
        observation["unit_scope"] = "article abstract"
        self.assert_reason("incomplete_unit", profile, binding, observation)

    def test_missing_or_unreviewed_complete_body_oracle_fails_closed(self):
        for change in (lambda p: p.pop("completeness"),
                       lambda p: p["completeness"].update(decision="unknown"),
                       lambda p: p["completeness"].update(oracle_ref=""),
                       lambda p: p["completeness"].update(reviewer=""),
                       lambda p: p["completeness"].update(reviewer="fixture-extractor-author")):
            with self.subTest(change=change):
                profile, binding, observation = self.case()
                change(profile)
                self.assert_reason("fixture_unverified", profile, binding, observation)

    def test_stale_profile_rights_oracle_and_connector_bindings_reject(self):
        for field in ("profile_revision", "connector_revision", "extraction_policy_revision",
                      "structural_rule_revision",
                      "rights_revision", "oracle_revision"):
            with self.subTest(field=field):
                profile, binding, observation = self.case()
                binding[field] = "new-revision"
                self.assert_reason("stale_binding", profile, binding, observation)
        profile, binding, observation = self.case()
        profile["rights"]["profile_revision"] = "prior-profile"
        self.assert_reason("stale_binding", profile, binding, observation)

    def test_wrong_publisher_connector_or_kind_rejects(self):
        for field, value in (("publisher_host", "other.example"),
                             ("connector_id", "unreviewed-browser"),
                             ("source_kind", "discussion")):
            with self.subTest(field=field):
                profile, binding, observation = self.case()
                observation[field] = value
                self.assert_reason("connector_mismatch", profile, binding, observation)

    def test_unknown_profile_version_and_malformed_observation_reject(self):
        profile, binding, observation = self.case()
        profile["schema_version"] = 2
        self.assert_reason("unknown_profile", profile, binding, observation)
        profile, binding, observation = self.case()
        observation["body_sha256"] = "not-a-digest"
        self.assert_reason("invalid_evidence", profile, binding, observation)


class BoundAdmissionTests(unittest.TestCase):
    """Only an explicitly patched test registry contains invented permission."""

    def unit(self):
        base = complete_unit()
        recipe = replace(base.acquisition, client="controlled-http-html",
                         configuration_revision="connector-2")
        completion = replace(base.completion, profile_revision="profile-3")
        return build_source_unit(base.article_id, base.source_kind, base.unit_scope,
                                 base.publication, recipe, completion, base.blocks)

    def registry(self):
        profile = deepcopy(FIXTURE["profile"])
        profile["rights"]["evidence_sha256"] = "638fdebf06f65e788c9727d2df2eca2dd842396dac7243cf2bb79620e13d1853"
        record = {
            "current_binding": deepcopy(FIXTURE["current_binding"]),
            "profile": profile,
            "rights_document": FIXTURE["synthetic_rights_document"],
            "fixture_raw_text": FIXTURE["raw_fixture_text"],
            "fixture_body_text": FIXTURE["expected_complete_body"],
            "fixture_observation": deepcopy(FIXTURE["observation"])
        }
        return {"schema_version": 1, "synthetic_test_only": True, "profiles": [record]}

    def test_fetch_authorization_uses_bound_registry_and_exact_host(self):
        with tempfile.TemporaryDirectory(prefix="tp44-synthetic-") as folder:
            path = Path(folder) / "profiles.json"
            path.write_text(json.dumps(self.registry()))
            with patch("src.acquisition.readiness._REGISTRY_PATH", path):
                allowed = authorize_fetch("alder-article", "alder.example")
                wrong_host = authorize_fetch("alder-article", "other.example")
        self.assertTrue(allowed.ready)
        self.assertEqual(allowed.profile_revision, "profile-3")
        self.assertEqual(allowed.client, "controlled-http-html")
        self.assertFalse(wrong_host.ready)
        self.assertEqual(wrong_host.reason, "unregistered_profile")

    def test_fetch_authorization_rejects_non_http_connector_even_with_consistent_bindings(self):
        registry = self.registry()
        record = registry["profiles"][0]
        record["profile"]["connector"]["id"] = "reviewed-browser"
        record["current_binding"]["connector_id"] = "reviewed-browser"
        record["fixture_observation"]["connector_id"] = "reviewed-browser"
        with tempfile.TemporaryDirectory(prefix="tp44-synthetic-") as folder:
            path = Path(folder) / "profiles.json"
            path.write_text(json.dumps(registry))
            with patch("src.acquisition.readiness._REGISTRY_PATH", path):
                result = authorize_fetch("alder-article", "alder.example")
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "unsupported_transport")

    def structural_result(self):
        article = FIXTURE["admission_article"]
        return {"decision": "complete", "rule_revision": "html-structure-v1",
                "source_structure_ref": "synthetic://dom-inventory",
                "source_url": article["source_url"], "unit_scope": article["unit_scope"],
                "raw_sha256": article["raw_sha256"],
                "body_sha256": article["body_sha256"],
                "member_ids": article["member_ids"],
                "member_sha256s": article["member_sha256s"],
                "checks": ["identity", "body_boundary", "continuation", "structure"]}

    def assess(self, registry, unit=None, raw=None, structural_result=None):
        unit = unit or self.unit()
        raw = raw if raw is not None else FIXTURE["admission_article"]["raw_text"].encode()
        receipt = self.structural_result() if structural_result is None else structural_result
        with tempfile.TemporaryDirectory(prefix="tp44-synthetic-") as folder:
            path = Path(folder) / "profiles.json"
            path.write_text(json.dumps(registry))
            with patch("src.acquisition.readiness._REGISTRY_PATH", path), patch(
                    "src.acquisition.readiness._load_structural_validator",
                    return_value=lambda *args: receipt):
                return admit_source_unit(unit, raw, ())

    def test_shipped_registry_is_empty_and_denies_all_units(self):
        from src.acquisition import readiness
        shipped = json.loads(readiness._REGISTRY_PATH.read_text())
        self.assertEqual(shipped, {"schema_version": 1, "profiles": []})
        result = admit_source_unit(self.unit(), b"publisher response", ())
        self.assertFalse(result.admitted)
        self.assertEqual(result.reason, "unregistered_profile")

    def test_synthetic_structural_validator_and_actual_bytes_admit_example(self):
        result = self.assess(self.registry())
        self.assertTrue(result.admitted)
        self.assertEqual(result.reason, "admitted_synthetic_fixture")

    def test_changed_article_text_can_pass_same_profile_without_daily_hash_allowlist(self):
        text = "Alder changed PineDB migration support. New details are in the same complete article structure."
        raw = b"publisher response edited"
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                         "5cc1cfc09049e43ee412de5627c96f8cb3142dd7f7a1a1eec460366073f3784b")
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "0dc2ea5ec7190989cac84fb81bcf179a6f56eb3f5b397e6ca9549d0572d55baf")
        base = complete_unit(text=text)
        recipe = replace(base.acquisition, client="controlled-http-html",
                         configuration_revision="connector-2",
                         raw_representation_sha256="0dc2ea5ec7190989cac84fb81bcf179a6f56eb3f5b397e6ca9549d0572d55baf")
        completion = replace(base.completion, profile_revision="profile-3")
        changed = build_source_unit(base.article_id, base.source_kind, base.unit_scope,
                                    base.publication, recipe, completion, base.blocks)
        receipt = self.structural_result()
        receipt["raw_sha256"] = "0dc2ea5ec7190989cac84fb81bcf179a6f56eb3f5b397e6ca9549d0572d55baf"
        receipt["body_sha256"] = "5cc1cfc09049e43ee412de5627c96f8cb3142dd7f7a1a1eec460366073f3784b"
        result = self.assess(self.registry(), unit=changed, raw=raw, structural_result=receipt)
        self.assertEqual(result.reason, "admitted_synthetic_fixture")

    def test_tampered_grant_and_oracle_are_denied(self):
        for change in (lambda r: r["profiles"][0].update(rights_document="changed grant"),
                       lambda r: r["profiles"][0].update(fixture_body_text="wrong body")):
            with self.subTest(change=change):
                registry = self.registry()
                change(registry)
                self.assertFalse(self.assess(registry).admitted)

    def test_wrong_actual_body_or_raw_bytes_are_denied(self):
        wrong = complete_unit(text="A different story body.")
        recipe = replace(wrong.acquisition, client="controlled-http-html",
                         configuration_revision="connector-2")
        completion = replace(wrong.completion, profile_revision="profile-3")
        wrong = build_source_unit(wrong.article_id, wrong.source_kind, wrong.unit_scope,
                                  wrong.publication, recipe, completion, wrong.blocks)
        self.assertEqual(self.assess(self.registry(), unit=wrong).reason, "wrong_body")
        self.assertEqual(self.assess(self.registry(), raw=b"different response").reason,
                         "raw_mismatch")

    def test_html_admission_rejects_non_http_acquisition_mode(self):
        unit = self.unit()
        altered = build_source_unit(unit.article_id, unit.source_kind, unit.unit_scope,
            unit.publication, replace(unit.acquisition, mode="browser"),
            unit.completion, unit.blocks)
        self.assertEqual(self.assess(self.registry(), unit=altered).reason,
                         "unsupported_transport")

    def test_direct_documentation_unit_cannot_claim_unverified_page_set(self):
        base = self.unit()
        checks = (CompletionCheck("identity", "passed", "fixture-identity"),
                  CompletionCheck("page_set", "passed", "invented-page-set"),
                  CompletionCheck("structure", "passed", "fixture-structure"))
        completion = replace(base.completion, checks=checks)
        unit = build_source_unit(base.article_id, "technical_documentation",
            base.unit_scope, base.publication, base.acquisition, completion, base.blocks)
        registry = self.registry()
        record = registry["profiles"][0]
        record["profile"]["source_kind"] = "technical_documentation"
        record["profile"]["completeness"]["source_kind"] = "technical_documentation"
        record["fixture_observation"]["source_kind"] = "technical_documentation"
        self.assertEqual(self.assess(registry, unit=unit).reason,
                         "page_set_validator_unavailable")

    def test_unavailable_production_structural_validator_denies_dynamic_article(self):
        registry = self.registry()
        with tempfile.TemporaryDirectory(prefix="tp44-synthetic-") as folder:
            path = Path(folder) / "profiles.json"
            path.write_text(json.dumps(registry))
            with patch("src.acquisition.readiness._REGISTRY_PATH", path):
                result = admit_source_unit(self.unit(), b"publisher response", ())
        self.assertEqual(result.reason, "structural_validation_failed")

    def test_actual_html_validator_accepts_only_complete_invented_dom(self):
        cases = json.loads((Path(__file__).parent / "fixtures" /
                            "html_extraction_cases.json").read_text())
        raw = cases["complete"].encode()
        extracted = extract_html(raw, cases["source_url"], cases["rules"])
        self.assertEqual(extracted.reason, "validated")
        base = complete_unit()
        recipe = replace(base.acquisition, client="controlled-http-html",
                         configuration_revision="connector-2",
                         raw_representation_sha256=hashlib.sha256(raw).hexdigest())
        body = "\n".join(block.text for block in extracted.blocks).encode()
        completion = replace(base.completion, profile_revision="profile-3",
                             represented_body_sha256=hashlib.sha256(body).hexdigest())
        publication = replace(base.publication, source_url=cases["source_url"])
        unit = build_source_unit(base.article_id, base.source_kind, base.unit_scope,
                                 publication, recipe, completion, extracted.blocks)
        registry = self.registry()
        registry["profiles"][0]["profile"]["html_rules"] = cases["rules"]
        with tempfile.TemporaryDirectory(prefix="tp09-synthetic-") as folder:
            path = Path(folder) / "profiles.json"
            path.write_text(json.dumps(registry))
            with patch("src.acquisition.readiness._REGISTRY_PATH", path):
                accepted = admit_source_unit(unit, raw, ())
                denied = admit_source_unit(unit, cases["preview"].encode(), ())
        self.assertEqual(accepted.reason, "admitted_synthetic_fixture")
        self.assertEqual(denied.reason, "raw_mismatch")

        bad_locator = list(extracted.blocks)
        bad_locator[0] = replace(bad_locator[0], source_locator=SourceLocator(
            "html", {"selector": "/html/body/article/div/p[99]"}))
        bad_list = list(extracted.blocks)
        for index in (4, 5):
            bad_list[index] = replace(bad_list[index], structure=replace(
                bad_list[index].structure, list_id="wrong-list"))
        bad_table = list(extracted.blocks)
        grid = bad_table[7].structure
        cells = list(grid.cells)
        cells[0] = replace(cells[0], role="data")
        bad_table[7] = replace(bad_table[7], structure=replace(grid, cells=tuple(cells)))
        for name, changed in (("locator", bad_locator), ("list", bad_list),
                              ("table", bad_table)):
            with self.subTest(name=name):
                altered = build_source_unit(base.article_id, base.source_kind,
                                            base.unit_scope, publication, recipe,
                                            completion, tuple(changed))
                with tempfile.TemporaryDirectory(prefix="tp09-synthetic-") as folder:
                    path = Path(folder) / "profiles.json"
                    path.write_text(json.dumps(registry))
                    with patch("src.acquisition.readiness._REGISTRY_PATH", path):
                        decision = admit_source_unit(altered, raw, ())
                self.assertEqual(decision.reason, "structural_validation_failed")

    def test_stale_binding_is_denied(self):
        registry = self.registry()
        registry["profiles"][0]["current_binding"]["rights_revision"] = "later-rights"
        self.assertFalse(self.assess(registry).admitted)

    def test_structural_result_must_bind_member_digests(self):
        receipt = self.structural_result()
        receipt.pop("member_sha256s")
        self.assertEqual(self.assess(self.registry(), structural_result=receipt).reason,
                         "structural_validation_failed")


if __name__ == "__main__":
    unittest.main()

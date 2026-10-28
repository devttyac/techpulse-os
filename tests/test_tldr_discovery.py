"""TP07's offline discovery contract; no real newsletter or publisher requests."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.catalogue import CATALOGUE_REVISION, EDITION_IDS, edition
from src.acquisition.tldr import discover_from_fixtures, discover_live
from src import ingestion
from src import content_availability as availability
from src.provenance import discovery_record


def issue(edition_id, links, issue_id="2026-10-28"):
    return {
        "edition_id": edition_id,
        "issue_id": issue_id,
        "issue_url": f"https://tldr.tech/{edition_id}/issues/{issue_id}",
        "links": links,
    }


def link(url, title="A publisher article", placement="editorial"):
    return {"url": url, "title": title, "placement": placement}


class CatalogueTests(unittest.TestCase):
    def test_exact_six_editions_and_deferred_rejection(self):
        self.assertEqual(CATALOGUE_REVISION, 1)
        self.assertEqual(EDITION_IDS, ("ai", "infosec", "data", "devops", "it", "dev"))
        for selected in EDITION_IDS:
            self.assertEqual(edition(selected)["id"], selected)
        for unselected in ("tech", "fintech", "cloud", "AI", ""):
            with self.assertRaises(ValueError):
                edition(unselected)


class FixtureDiscoveryTests(unittest.TestCase):
    def test_six_editions_deduplicate_exact_url_and_keep_all_associations(self):
        fixtures = {selected: issue(selected, [link(f"https://publisher.example/{selected}")])
                    for selected in EDITION_IDS}
        fixtures["ai"]["links"].append(link("https://publisher.example/shared"))
        fixtures["data"]["links"].append(link("https://publisher.example/shared"))
        result = discover_from_fixtures(fixtures)
        self.assertEqual(result["status"], "fixture_only")
        self.assertEqual(len(result["candidates"]), 7)
        shared = next(c for c in result["candidates"] if c["url"].endswith("/shared"))
        self.assertEqual([a["edition_id"] for a in shared["associations"]], ["ai", "data"])
        self.assertEqual(shared["evidence_status"], "discovery_only")
        self.assertEqual([e["outcome"] for e in result["editions"]], ["candidate"] * 6)

    def test_sponsor_unsafe_and_malformed_links_never_become_candidates(self):
        fixtures = {"ai": issue("ai", [
            link("https://publisher.example/editorial"),
            link("https://publisher.example/sponsored", placement="sponsor"),
            link("https://publisher.example/case", placement=" Sponsored "),
            link("http://127.0.0.1/private"),
            link("https://user:pass@publisher.example/private"),
            {"title": "No URL", "placement": "editorial"},
            {"url": "https://publisher.example/no-placement", "title": "Unknown placement"},
        ])}
        result = discover_from_fixtures(fixtures)
        self.assertEqual([c["url"] for c in result["candidates"]],
                         ["https://publisher.example/editorial"])
        self.assertEqual(result["editions"][0]["received"], 7)
        self.assertEqual(result["editions"][0]["rejected"], 6)

    def test_sponsor_and_unresolved_placement_have_distinct_dispositions_without_editorial(self):
        result = discover_from_fixtures({"ai": issue("ai", [
            link("https://publisher.example/sponsor", placement="sponsor"),
            link("https://publisher.example/unknown", placement="partner"),
            {"url": "https://publisher.example/missing", "title": "Missing placement"},
        ])})
        row = result["editions"][0]
        self.assertEqual(row["outcome"], "placement_unresolved")
        self.assertEqual((row["sponsor_excluded"], row["placement_unresolved"],
                          row["rejected"], row["candidates"]), (1, 2, 3, 0))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status"], "partial")
        safe = discovery_record(result)
        self.assertEqual((safe["editions"][0]["sponsor_excluded"],
                          safe["editions"][0]["placement_unresolved"]), (1, 2))
        health = availability.build_source_health({"discovery": safe}, "run-1", "2026-10-28T10:00:00+00:00")
        self.assertEqual(health["sources"][0]["outcome"], "placement_unresolved")
        self.assertEqual((health["sources"][0]["sponsor_excluded"],
                          health["sources"][0]["placement_unresolved"]), (1, 2))
        self.assertEqual(availability.sanitize_source_health(health), health)

    def test_editorial_candidate_keeps_unresolved_placement_visible(self):
        result = discover_from_fixtures({"ai": issue("ai", [
            link("https://publisher.example/editorial"),
            link("https://publisher.example/sponsor", placement=" Sponsored "),
            link("https://publisher.example/unresolved", placement=""),
        ])})
        row = result["editions"][0]
        self.assertEqual(row["outcome"], "candidate_partial")
        self.assertEqual((row["sponsor_excluded"], row["placement_unresolved"],
                          row["candidates"]), (1, 1, 1))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["url"], "https://publisher.example/editorial")

    def test_sponsor_only_issue_is_empty_with_explicit_sponsor_count(self):
        result = discover_from_fixtures({"ai": issue("ai", [
            link("https://publisher.example/sponsor", placement="Sponsored")])})
        row = result["editions"][0]
        self.assertEqual(row["outcome"], "valid_empty")
        self.assertEqual((row["sponsor_excluded"], row["placement_unresolved"]), (1, 0))
        self.assertEqual(result["candidates"], [])

    def test_conflicting_valid_empty_counts_never_become_healthy(self):
        result = discover_from_fixtures({selected: issue(selected, []) for selected in EDITION_IDS})
        result["editions"][0]["placement_unresolved"] = 1
        safe = discovery_record(result)
        self.assertEqual(safe["reason"], "invalid_discovery_state")
        self.assertEqual(safe["editions"][0]["outcome"], "not_checked")
        self.assertEqual(safe["editions"][0]["reason"], "inconsistent_outcome_counts")
        health = availability.build_source_health({"discovery": safe}, "run-bad", "2026-10-28T10:00:00+00:00")
        self.assertNotEqual(health["status"], "healthy")
        self.assertEqual(health["reason"], "invalid_discovery_state")

    def test_disabled_claim_requires_six_valid_disabled_rows(self):
        actual = asyncio.run(discover_live())
        self.assertEqual(availability.build_source_health({"discovery": discovery_record(actual)},
                                                          "run-disabled", "2026-10-28T10:00:00+00:00")["status"], "disabled")
        malformed = {**actual, "editions": actual["editions"][:-1]}
        safe = discovery_record(malformed)
        self.assertNotEqual(safe["status"], "disabled")
        self.assertEqual(safe["reason"], "invalid_discovery_state")
        direct = availability.build_source_health({"discovery": malformed}, "run-bad", "2026-10-28T10:00:00+00:00")
        self.assertEqual(direct["status"], "unknown")
        self.assertEqual(direct["reason"], "invalid_disabled_diagnostic")
        counted = {**actual, "editions": [dict(row) for row in actual["editions"]]}
        counted["editions"][0]["received"] = 1
        counted_health = availability.build_source_health({"discovery": counted}, "run-counted", "2026-10-28T10:00:00+00:00")
        self.assertEqual(counted_health["status"], "unknown")

    def test_six_clean_valid_empty_issues_still_make_complete_fixture_health(self):
        result = discover_from_fixtures({selected: issue(selected, []) for selected in EDITION_IDS})
        safe = discovery_record(result)
        self.assertEqual(safe["status"], "fixture_only")
        health = availability.build_source_health({"discovery": safe}, "run-empty", "2026-10-28T10:00:00+00:00")
        self.assertEqual(health["status"], "healthy")

    def test_checked_issue_identity_is_required_at_diagnostic_and_health_boundaries(self):
        original = discover_from_fixtures({selected: issue(selected, []) for selected in EDITION_IDS})
        for bad in (None, "other-issue"):
            result = {**original, "editions": [dict(row) for row in original["editions"]]}
            result["editions"][0]["issue_id"] = bad
            safe = discovery_record(result)
            self.assertNotEqual(safe["status"], "fixture_only")
            self.assertEqual(safe["reason"], "invalid_discovery_state")
            health = availability.build_source_health({"discovery": result},
                "run-identity", "2026-10-28T10:00:00+00:00")
            self.assertEqual((health["status"], health["reason"]), ("unknown", "invalid_issue_identity"))
        result = {**original, "editions": [dict(row) for row in original["editions"]]}
        for row in result["editions"]:
            row["issue_id"] = None
            row["issue_url"] = None
        self.assertNotEqual(discovery_record(result)["status"], "fixture_only")
        health = availability.build_source_health({"discovery": result},
            "run-url", "2026-10-28T10:00:00+00:00")
        self.assertEqual((health["status"], health["reason"]), ("unknown", "invalid_issue_identity"))

    def test_recovered_checked_health_requires_bound_issue_identity(self):
        original = discover_from_fixtures({selected: issue(selected, []) for selected in EDITION_IDS})
        health = availability.build_source_health({"discovery": discovery_record(original)},
                                                   "run-valid", "2026-10-28T10:00:00+00:00")
        self.assertEqual(health["status"], "healthy")
        tampered = {**health, "sources": [dict(row) for row in health["sources"]]}
        tampered["sources"][0]["issue_id"] = None
        self.assertEqual(availability.sanitize_source_health(tampered)["status"], "unknown")

    def test_missing_raw_counts_and_wrong_catalogue_revision_do_not_throw_or_pass(self):
        result = discover_from_fixtures({selected: issue(selected, []) for selected in EDITION_IDS})
        del result["editions"][0]["received"]
        result["catalogue_revision"] = 99
        safe = discovery_record(result)
        self.assertEqual(safe["reason"], "invalid_discovery_state")
        self.assertEqual(safe["editions"][0]["outcome"], "not_checked")
        health = availability.build_source_health({"discovery": safe}, "run-invalid", "2026-10-28T10:00:00+00:00")
        self.assertEqual(health["status"], "unknown")

    def test_missing_and_invalid_issues_are_not_quiet_success(self):
        result = discover_from_fixtures({"ai": issue("ai", []),
                                         "data": issue("data", "not-links")})
        outcomes = {e["edition_id"]: e["outcome"] for e in result["editions"]}
        self.assertEqual(outcomes["ai"], "valid_empty")
        self.assertEqual(outcomes["data"], "parse_error")
        self.assertEqual(outcomes["dev"], "not_checked")
        self.assertEqual(result["status"], "partial")

    def test_issue_identity_must_match_edition_issue_route(self):
        contradictory = issue("ai", [link("https://publisher.example/story")])
        contradictory["issue_url"] = "https://tldr.tech/ai/issues/another-issue"
        result = discover_from_fixtures({"ai": contradictory})
        self.assertEqual(result["editions"][0]["outcome"], "parse_error")
        self.assertEqual(result["candidates"], [])

    def test_unselected_edition_rejects_entire_request(self):
        with self.assertRaises(ValueError):
            discover_from_fixtures({"tech": issue("tech", [link("https://example.org/a")])})

    def test_all_six_fail_without_fallback(self):
        result = discover_from_fixtures({selected: issue(selected, None) for selected in EDITION_IDS})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["candidates"], [])
        self.assertTrue(all(e["outcome"] == "parse_error" for e in result["editions"]))

    def test_more_than_five_links_are_preserved_in_discovery_and_health(self):
        result = discover_from_fixtures({"ai": issue("ai", [
            link(f"https://publisher.example/story-{index}") for index in range(8)])})
        self.assertEqual(len(result["candidates"]), 8)
        self.assertEqual(result["editions"][0]["candidates"], 8)
        snapshot = availability.build_source_health({"discovery": discovery_record(result)},
                                                     "run-8", "2026-10-28T10:00:00+00:00")
        self.assertEqual(snapshot["sources"][0]["accepted"], 8)

    def test_repeated_exact_url_in_one_issue_has_one_association(self):
        url = "https://publisher.example/story?edition=one"
        result = discover_from_fixtures({"ai": issue("ai", [link(url), link(url)])})
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["candidates"][0]["associations"]), 1)
        self.assertEqual(result["editions"][0]["candidates"], 1)

    def test_live_discovery_is_disabled_without_invoking_a_client(self):
        class ForbiddenClient:
            def __getattr__(self, name):
                raise AssertionError("No client operation is allowed")

        result = asyncio.run(discover_live(client=ForbiddenClient()))
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["editions"]), 6)


class RuntimeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_discovery_is_disabled_without_http_or_article_corpus(self):
        diagnostics = {}
        with patch("httpx.AsyncClient", side_effect=AssertionError("No live request allowed")):
            corpus = await ingestion.ingest_all_domains(diagnostics=diagnostics)
        self.assertEqual(set(corpus), {"ai", "cloud", "data", "sec", "devops", "arch", "finops", "gov"})
        self.assertTrue(all(not articles for articles in corpus.values()))
        self.assertEqual(diagnostics["discovery"]["status"], "disabled")
        self.assertEqual(diagnostics["discovery"]["reason"], "tldr_interface_and_discovery_use_unverified")
        self.assertEqual(len(diagnostics["discovery"]["editions"]), 6)
        self.assertNotIn("feeds", diagnostics)

    async def test_disabled_discovery_health_is_six_editions_not_a_news_check(self):
        diagnostics = {}
        await ingestion.ingest_all_domains(diagnostics=diagnostics)
        snapshot = availability.build_source_health(diagnostics, "run-123", "2026-10-28T10:00:00+00:00")
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["status"], "disabled")
        self.assertIsNone(snapshot["checked_at"])
        self.assertEqual(snapshot["total_sources"], 6)
        self.assertEqual(snapshot["available_sources"], 0)
        self.assertEqual([s["edition_id"] for s in snapshot["sources"]], list(EDITION_IDS))
        self.assertTrue(all(s["outcome"] == "disabled" for s in snapshot["sources"]))
        self.assertEqual(availability.sanitize_source_health(snapshot), snapshot)

    async def test_malformed_discovery_diagnostics_fail_closed(self):
        from src.provenance import discovery_record
        malformed = {"status": [], "editions": None, "candidates": "invalid"}
        safe = discovery_record(malformed)
        self.assertEqual(safe["status"], "failed")
        self.assertEqual(safe["candidate_count"], 0)
        snapshot = availability.build_source_health({"discovery": safe},
                                                    "run-bad", "2026-10-28T10:00:00+00:00")
        self.assertEqual(snapshot["status"], "unknown")

    async def test_legacy_snapshot_cannot_claim_new_disabled_status(self):
        legacy = availability.build_source_health({"feeds": []}, "run-old", "2026-10-28T10:00:00+00:00")
        legacy["status"] = "disabled"
        self.assertEqual(availability.sanitize_source_health(legacy)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()

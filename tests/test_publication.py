"""Independent offline expectations for TP10 publication-date handoff."""

import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.http import FetchResult
from src.run_coordinator import capture_zone_rules, frozen_window


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "publication_cases.json").read_text())


def publication_module():
    try:
        return importlib.import_module("src.acquisition.publication")
    except ModuleNotFoundError:
        return None


def source_html(case):
    parts = ["<html><body><article id='story'><h1>PineDB</h1><header>"]
    for date in case["dates"]:
        event = date["event"]
        value = date["value"]
        owner = date.get("owner", "alder.example")
        tz = date.get("publisher_timezone")
        tz_attrs = (f" data-timezone='{tz}' data-timezone-owner='"
                    f"{date.get('timezone_owner', owner)}'" if tz else "")
        parts.append(f"<time data-event='{event}' data-owner='{owner}'"
                     f" datetime='{value}'{tz_attrs}></time>")
    parts.append("</header></article><aside><time data-event='publisher_publication' "
                 "data-owner='alder.example' datetime='2026-10-08'></time></aside></body></html>")
    return "".join(parts).encode()


def date_rules():
    return {"revision": "date-v1", "publisher": "alder.example",
            "article_xpath": "//article[@id='story']",
            "header_xpath": ".//header",
            "fields": [{"event_type": event,
                        "xpath": f".//time[@data-event='{event}']",
                        "value_attribute": "datetime", "scope": "article_header",
                        "owner_attribute": "data-owner",
                        "semantic_attribute": "data-event", "semantic_value": event,
                        "timezone_attribute": "data-timezone",
                        "timezone_owner_attribute": "data-timezone-owner"}
                       for event in ("publisher_publication", "original_media_release",
                                     "transcript_publication", "retrieval", "revision",
                                     "modification")]}


class PublicationFixtureTests(unittest.TestCase):
    def test_independent_cases(self):
        module = publication_module()
        self.assertIsNotNone(module, "TP10 publication module is missing")
        rules = capture_zone_rules(FIXTURE["timezone"])
        from datetime import datetime
        window = frozen_window(datetime.fromisoformat(FIXTURE["run_start_utc"]), rules)
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                actual = module.assess_publication(source_html(case), FIXTURE["source_url"],
                    date_rules(), window, rules, source_kind=case.get("source_kind", "publisher_article"))
                self.assertEqual(actual.eligible, case["eligible"])
                self.assertEqual(actual.reason, case["reason"])
                self.assertEqual(actual.local_date.isoformat() if actual.local_date else None,
                                 case.get("local_date"))
                self.assertEqual(actual.provenance.publisher, "alder.example")
                if "precision" in case:
                    required = ("original_media_release" if case.get("source_kind") == "publisher_transcript"
                                else "publisher_publication")
                    event = next(e for e in actual.provenance.events if e.event_type == required)
                    self.assertEqual(event.precision, case["precision"])
                    if "offset" in case:
                        self.assertEqual(event.offset, case["offset"])
                    if "timezone_rule_sha256" in case:
                        self.assertTrue(actual.observations[0].timezone_rule_sha256)
                        self.assertIn("/@data-timezone", event.source_location)
                    if "publisher_source_date" in case:
                        selected_observation = next(item for item in actual.observations
                            if item.raw_value == event.raw_value
                            and item.source_location == event.source_location)
                        self.assertEqual(selected_observation.source_date.isoformat(),
                                         case["publisher_source_date"])
                if "day_zone_vs_utc_instant" in case["name"]:
                    self.assertEqual(actual.observations[0].publisher_timezone,
                                     "America/New_York")
                    self.assertEqual(actual.observations[0].timezone_owner,
                                     "alder.example")
                    self.assertTrue(actual.observations[0].timezone_rule_sha256)
                    self.assertIn("/@data-timezone", actual.observations[0].source_location)
                    self.assertEqual(actual.observations[1].publisher_timezone, "UTC")
                    self.assertEqual(actual.observations[1].timezone_owner,
                                     "alder.example")
                if case["name"] == "unowned_day_zone_cannot_reconcile_utc_instant":
                    self.assertEqual(actual.observations[0].timezone_owner,
                                     "other.example")
                    self.assertEqual(actual.observations[0].timezone_rule_sha256, "")
                if "conflicting_values" in case:
                    self.assertEqual(list(actual.provenance.events[0].conflicting_values),
                                     case["conflicting_values"])
                self.assertEqual(len(actual.observations), len(case["dates"]))

    def test_dst_window_uses_frozen_zone_rules(self):
        module = publication_module()
        self.assertIsNotNone(module, "TP10 publication module is missing")
        from datetime import datetime
        rules = capture_zone_rules("America/New_York")
        window = frozen_window(datetime.fromisoformat("2026-03-15T10:00:00+00:00"), rules)
        for value, expected in (("2026-03-09", True), ("2026-03-08", False),
                                ("2026-03-15T06:01:00-04:00", False)):
            with self.subTest(value=value):
                case = {"dates": [{"event": "publisher_publication", "value": value}]}
                actual = module.assess_publication(source_html(case), FIXTURE["source_url"],
                    date_rules(), window, rules)
                self.assertEqual(actual.eligible, expected)

    def test_source_ownership_and_rule_failures_do_not_admit_a_date(self):
        module = publication_module()
        self.assertIsNotNone(module, "TP10 publication module is missing")
        from datetime import datetime
        rules = capture_zone_rules(FIXTURE["timezone"])
        window = frozen_window(datetime.fromisoformat(FIXTURE["run_start_utc"]), rules)
        case = {"dates": [{"event": "publisher_publication", "value": "2026-10-06"}]}
        for changed in ({"article_xpath": "//aside"},
                        {"article_xpath": "//article/@id"}, {"revision": ""},
                        {"publisher": "other.example"},
                        {"fields": [{"event_type": "publisher_publication",
                                     "xpath": ".//time[@data-event='publisher_publication'] | //aside//time",
                                     "value_attribute": "datetime", "scope": "article_header",
                                     "owner_attribute": "data-owner",
                                     "semantic_attribute": "data-event",
                                     "semantic_value": "publisher_publication"}]},
                        {"fields": [{"event_type": ["publisher_publication"],
                                     "xpath": ".//time", "value_attribute": "datetime",
                                     "scope": "article_header",
                                     "owner_attribute": "data-owner"}]}):
            with self.subTest(changed=changed):
                profile = date_rules() | changed
                actual = module.assess_publication(source_html(case), FIXTURE["source_url"],
                    profile, window, rules)
                self.assertFalse(actual.eligible)

    def test_profile_event_mapping_cannot_promote_modification_or_unowned_head(self):
        module = publication_module()
        self.assertIsNotNone(module, "TP10 publication module is missing")
        from datetime import datetime
        rules = capture_zone_rules(FIXTURE["timezone"])
        window = frozen_window(datetime.fromisoformat(FIXTURE["run_start_utc"]), rules)
        raw = source_html({"dates": [{"event": "publisher_publication",
                                      "value": "2026-10-06"}]})
        profile = date_rules()
        profile["fields"] = [{"event_type": "modification",
                              "xpath": ".//time[@data-event='publisher_publication']",
                              "value_attribute": "datetime", "scope": "article_header",
                              "owner_attribute": "data-owner",
                              "semantic_attribute": "data-event",
                              "semantic_value": "modification"}]
        actual = module.assess_publication(raw, FIXTURE["source_url"], profile, window, rules)
        self.assertFalse(actual.eligible)
        self.assertEqual(actual.reason, "unreviewed_rules")
        profile["fields"] = [{"event_type": "publisher_publication",
                              "xpath": ".//time[@data-event='modification']",
                              "value_attribute": "datetime", "scope": "article_header",
                              "owner_attribute": "data-owner",
                              "semantic_attribute": "data-event",
                              "semantic_value": "modification"}]
        modification = source_html({"dates": [{"event": "modification",
                                               "value": "2026-10-06"}]})
        actual = module.assess_publication(modification, FIXTURE["source_url"],
                                           profile, window, rules)
        self.assertFalse(actual.eligible)
        self.assertEqual(actual.reason, "unreviewed_rules")
        head = ("<html><head><link rel='canonical' href='https://other.example/story'>"
                "<meta property='article:published_time' content='2026-10-06'></head>"
                "<body><article id='story'></article></body></html>").encode()
        profile["fields"] = [{"event_type": "publisher_publication",
                              "xpath": ".//meta[@property='article:published_time']",
                              "value_attribute": "content", "scope": "head",
                              "semantic_attribute": "property",
                              "semantic_value": "article:published_time"}]
        actual = module.assess_publication(head, FIXTURE["source_url"], profile, window, rules)
        self.assertFalse(actual.eligible)
        self.assertEqual(actual.reason, "unowned_source")

    def test_related_card_inside_article_cannot_supply_publication(self):
        module = publication_module()
        from datetime import datetime
        rules = capture_zone_rules(FIXTURE["timezone"])
        window = frozen_window(datetime.fromisoformat(FIXTURE["run_start_utc"]), rules)
        raw = ("<html><body><article id='story'><header></header><aside>"
               "<time data-event='publisher_publication' data-owner='alder.example' "
               "datetime='2026-10-06'></time></aside></article></body></html>").encode()
        actual = module.assess_publication(raw, FIXTURE["source_url"],
                                           date_rules(), window, rules)
        self.assertFalse(actual.eligible)
        self.assertEqual(actual.reason, "unknown_publication")

    def test_ingestion_date_handoff_reuses_fetched_bytes_without_admission(self):
        from src import ingestion
        self.assertTrue(hasattr(ingestion, "fetch_and_assess_candidate"),
                        "TP10 ingestion handoff is missing")
        from datetime import datetime
        rules = capture_zone_rules(FIXTURE["timezone"])
        window = frozen_window(datetime.fromisoformat(FIXTURE["run_start_utc"]), rules)
        case = {"dates": [{"event": "publisher_publication", "value": "2026-10-06"}]}
        raw = source_html(case)
        fetched = FetchResult("fetched", raw, hashlib.sha256(raw).hexdigest(),
            FIXTURE["source_url"], 200, {"content-type": "text/html"},
            "controlled-http-html", "connector-2", "profile-3", "utf-8", "203.0.113.10")
        with patch("src.ingestion.fetch_candidate", return_value=fetched), patch(
                "src.ingestion._load_registry", return_value={"schema_version": 1, "profiles": []}):
            denied = asyncio.run(ingestion.fetch_and_assess_candidate(
                FIXTURE["source_url"], "alder-article", window, rules))
        self.assertFalse(denied.publication.eligible)
        self.assertEqual(denied.publication.reason, "unreviewed_rules")
        self.assertEqual(denied.fetch.raw_bytes, raw)

    def test_validated_html_and_date_share_one_fetched_representation(self):
        from src.ingestion import fetch_and_assess_candidate
        from datetime import datetime
        rules = capture_zone_rules(FIXTURE["timezone"])
        window = frozen_window(datetime.fromisoformat(FIXTURE["run_start_utc"]), rules)
        html_cases = json.loads((Path(__file__).parent / "fixtures" /
                                 "html_extraction_cases.json").read_text())
        raw = html_cases["short_complete"].replace(
            "<div class='story-body'>",
            "<header><time data-event='publisher_publication' data-owner='alder.example' "
            "datetime='2026-10-06'></time></header><div class='story-body'>").encode()
        fetched = FetchResult("fetched", raw, hashlib.sha256(raw).hexdigest(),
            html_cases["source_url"], 200, {"content-type": "text/html"},
            "controlled-http-html", "connector-2", "profile-3", "utf-8", "203.0.113.10")
        profile = {"profile_id": "alder-article", "revision": "profile-3",
            "publisher_host": "alder.example", "source_kind": "publisher_article",
            "connector": {"id": "controlled-http-html", "revision": "connector-2"},
            "structural_rule_revision": "html-structure-v1",
            "html_rules": html_cases["rules"], "date_rule_revision": "date-v1",
            "date_rules": date_rules()}
        for change, expected in (({}, True),
                                 ({"date_rule_revision": "stale"}, False),
                                 ({"date_rules": None}, False),
                                 ({"date_rules": date_rules() | {"article_xpath": "//article[@id='other']"}}, False)):
            with self.subTest(change=change):
                candidate_profile = profile | change
                registry = {"schema_version": 1, "profiles": [{"profile": candidate_profile}]}
                with patch("src.ingestion.fetch_candidate", return_value=fetched), patch(
                        "src.ingestion._load_registry", return_value=registry):
                    result = asyncio.run(fetch_and_assess_candidate(
                        html_cases["source_url"], "alder-article", window, rules))
                self.assertEqual(result.fetch.raw_bytes, raw)
                self.assertEqual(result.reason, "validated")
                self.assertEqual(result.publication.eligible, expected)
                if expected:
                    self.assertEqual(result.publication.local_date.isoformat(), "2026-10-06")
                else:
                    self.assertEqual(result.publication.reason, "unreviewed_rules")


if __name__ == "__main__":
    unittest.main()

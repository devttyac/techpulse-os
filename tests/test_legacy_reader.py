"""TP04 retained-episode compatibility contracts, using synthetic records."""

import copy
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from src.legacy_reader import classify_retained_episode, project_retained_episode
from src.story_manifest import freeze_story_manifest, manifest_to_dict
from src.content_availability import build_content_availability


class RetainedReaderTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "id": "ep-198", "guid": "retained-guid-198",
            "date": "Oct 15, 2026", "zone": "Asia/Singapore",
            "hosts": "Host A & Host B", "source_url": "https://example.test/article",
            "summary": "Saved summary", "audio_url": "",
        }

    def test_affirmative_rss_basis_labels_summary_without_full_article_features(self):
        episode = {**self.original, "content_basis": "rss_summaries"}
        before = copy.deepcopy(episode)
        view = classify_retained_episode(episode)
        self.assertEqual(view.provenance, "confirmed_rss")
        self.assertEqual(view.evidence, "never_captured")
        self.assertEqual(view.label, "Legacy episode — RSS summaries")
        self.assertEqual(view.full_article_features, "unavailable")
        self.assertEqual(episode, before)

    def test_missing_basis_does_not_infer_rss_or_capture_from_summary(self):
        view = classify_retained_episode(self.original)
        self.assertEqual(view.provenance, "unknown")
        self.assertEqual(view.evidence, "unknown")
        self.assertEqual(view.label, "Source provenance unknown")
        self.assertEqual(view.full_article_features, "unavailable")

    def test_explicit_capture_without_verified_body_is_integrity_failure(self):
        episode = {**self.original, "legacy_evidence": {
            "schema_version": 1, "capture_status": "captured",
            "version_ids": ["a" * 64],
        }}
        view = classify_retained_episode(episode)
        self.assertEqual(view.provenance, "captured")
        self.assertEqual(view.evidence, "captured_unavailable")
        self.assertEqual(view.full_article_features, "unavailable")

    def test_unverified_full_articles_field_is_not_admitted_body_evidence(self):
        episode = {**self.original, "full_articles": {"ai": "Unverified seed prose"}}
        self.assertEqual(classify_retained_episode(episode).provenance, "unknown")

    def test_valid_v1_manifest_affirms_rss_and_malformed_present_fails_closed(self):
        received = {"ai": [{"title": "Fixture", "summary": "RSS summary.",
                            "source_name": "Publisher", "url": "https://example.test/one"}]}
        episode = {**self.original, "story_manifest": manifest_to_dict(
            freeze_story_manifest(received, build_content_availability(received)))}
        self.assertEqual(classify_retained_episode(episode).provenance, "confirmed_rss")
        episode["story_manifest"]["schema_version"] = True
        with self.assertRaises(ValueError):
            classify_retained_episode(episode)

    def test_projection_keeps_identity_hosts_dates_and_links_without_saving(self):
        episode = {**self.original, "content_basis": "rss_summaries",
                   "audio_url": "/audio/ep-198.mp3",
                   "audio_availability": {"podcast": {"status": "available"}}}
        before = copy.deepcopy(episode)
        projected = project_retained_episode(episode, audio_available=False)
        for key in ("id", "guid", "date", "zone", "hosts", "source_url", "summary"):
            self.assertEqual(projected[key], episode[key])
        self.assertEqual(projected["audio_url"], "")
        self.assertEqual(projected["audio_availability"]["podcast"]["status"], "unavailable")
        self.assertEqual(projected["legacy_compatibility"]["provenance"], "confirmed_rss")
        self.assertEqual(episode, before)

    def test_missing_audio_url_cannot_leave_a_false_available_status(self):
        episode = {**self.original, "audio_availability": {"podcast": {"status": "available"}}}
        projected = project_retained_episode(episode, audio_available=False)
        self.assertEqual(projected["audio_availability"]["podcast"]["status"], "unavailable")
        self.assertEqual(episode["audio_availability"]["podcast"]["status"], "available")

    def test_unverified_domain_clip_is_not_advertised(self):
        episode = {**self.original, "domain_audio": {"ai": "/audio/ep-198-ai.mp3"},
                   "audio_availability": {"domains": {"ai": {"status": "available"}}}}
        before = copy.deepcopy(episode)
        projected = project_retained_episode(episode, audio_available=False)
        self.assertFalse(projected["domain_audio"].get("ai"))
        self.assertEqual(projected["audio_availability"]["domains"]["ai"]["status"], "unavailable")
        self.assertEqual(episode, before)

    def test_verified_domain_clip_keeps_declared_link_without_requiring_main_audio(self):
        episode = {**self.original, "domain_audio": {"ai": "/audio/ep-198-ai.mp3"},
                   "audio_availability": {"domains": {"ai": {"status": "available"}}}}
        projected = project_retained_episode(episode, audio_available=False,
                                             available_domains=frozenset({"ai"}))
        self.assertEqual(projected["domain_audio"]["ai"], "/audio/ep-198-ai.mp3")
        self.assertEqual(projected["audio_availability"]["domains"]["ai"]["status"], "available")


if __name__ == "__main__":
    unittest.main()

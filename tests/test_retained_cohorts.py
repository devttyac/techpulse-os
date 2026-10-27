"""TP43: dated cohort observations and synthetic retained-reader regressions.

The #183–203 episode JSON and audio bytes are not repository fixtures. These
tests use the observation matrix for IDs and reported fields, then build
separate synthetic inputs to test the current reader's fail-closed behavior.
"""

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ["API_SECRET_KEY"] = "synthetic-retained-cohort-test-key"
_STORAGE = tempfile.TemporaryDirectory(prefix="techpulse-tp43-import-")
os.environ["STORAGE_DIR"] = _STORAGE.name

from src import main as app_main  # noqa: E402
MATRIX = json.loads((ROOT / "tests" / "fixtures" / "retained_cohorts.json").read_text())


def cohort(name):
    return next(item for item in MATRIX["cohorts"] if item["name"] == name)


class RetainedCohortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="techpulse-tp43-")
        self.audio = Path(self.tmp.name) / "audio"
        self.episodes = Path(self.tmp.name) / "episodes"
        self.audio.mkdir()
        self.episodes.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def view(self, episode):
        with patch.object(app_main, "AUDIO_DIR", str(self.audio)):
            return app_main._retained_episode_view(episode)

    def test_matrix_separates_historical_snapshot_from_current_state(self):
        self.assertEqual([c["name"] for c in MATRIX["cohorts"]],
                         ["ep-183–188", "ep-189", "ep-190–196", "ep-197", "ep-198–203"])
        ids = [ep["id"] for group in MATRIX["cohorts"] for ep in group["episodes"]]
        self.assertEqual(ids, [f"ep-{n}" for n in range(183, 204)])
        hashes = [ep["record_sha256"] for group in MATRIX["cohorts"]
                  for ep in group["episodes"] if ep["record_sha256"] is not None]
        self.assertEqual(len(hashes), 20)
        self.assertEqual(len(set(hashes)), 20)
        self.assertIsNone(cohort("ep-197")["episodes"][0]["record_sha256"])
        self.assertEqual(MATRIX["historical_inventory"]["declared_audio_regular_files_present"], 72)
        self.assertEqual(MATRIX["historical_inventory"]["unclassified_direct_audio_files"], 210)
        self.assertIsNone(MATRIX["historical_inventory"]["unclassified_files_are_orphans"])
        self.assertIsNone(MATRIX["historical_inventory"]["current_installation_state"])
        self.assertTrue(MATRIX["synthetic_reader_inputs_are_not_operator_records"])

    def test_old_full_articles_and_fingerprint_shaped_fields_do_not_admit_body(self):
        # The record shape mirrors only observed *field presence*, not bodies.
        for name in ("ep-183–188", "ep-189", "ep-190–196"):
            entry = cohort(name)["episodes"][0]
            synthetic = {"id": entry["id"], "title": "Synthetic retained title"}
            if name != "ep-190–196":
                synthetic["full_articles"] = {"ai": "synthetic, unverified text"}
            else:
                synthetic["episode_fingerprint"] = "a" * 64
                synthetic["audio_recipe_fingerprint"] = "b" * 64
            before = copy.deepcopy(synthetic)
            view = self.view(synthetic)
            self.assertEqual(view["legacy_compatibility"]["provenance"], "unknown")
            self.assertEqual(view["legacy_compatibility"]["evidence"], "unknown")
            self.assertEqual(view["legacy_compatibility"]["full_article_features"], "unavailable")
            self.assertNotIn("date", view)
            self.assertNotIn("hosts", view)
            self.assertNotIn("zone", view)
            self.assertEqual(synthetic, before)

    def test_reported_audio_declaration_without_matching_file_stays_unplayable(self):
        for name in ("ep-189", "ep-190–196", "ep-197"):
            entry = cohort(name)["episodes"][0]
            episode_id = entry["id"]
            synthetic = {
                "id": episode_id, "audio_url": f"/audio/{episode_id}.mp3",
                "audio_availability": {"podcast": {"status": "available"}},
                "hosts": "Synthetic Host A & Host B", "zone": "Etc/UTC",
                "source_url": "https://example.test/synthetic-article",
            }
            if name == "ep-197":
                synthetic["date"] = cohort(name)["observed_structure"]["date"]
            before = copy.deepcopy(synthetic)
            view = self.view(synthetic)
            self.assertEqual(view["id"], episode_id)
            self.assertEqual(view["hosts"], synthetic["hosts"])
            self.assertEqual(view["zone"], synthetic["zone"])
            self.assertEqual(view["source_url"], synthetic["source_url"])
            self.assertEqual(view.get("date"), synthetic.get("date"))
            self.assertEqual(view["audio_url"], "")
            self.assertEqual(view["audio_availability"]["podcast"]["status"], "unavailable")
            self.assertEqual(synthetic, before)

    def test_missing_main_audio_cohort_keeps_dates_and_cannot_gain_cues(self):
        for entry in cohort("ep-198–203")["episodes"]:
            synthetic = {
                "id": entry["id"], "date": entry["date"], "duration": "00:00",
                "audio_url": "", "audio_availability": {"podcast": {"status": "unavailable"}},
                "chapters": [{"story_id": f"synthetic-{n}"} for n in range(24)],
                "hosts": "Synthetic Host A & Host B", "zone": "Etc/UTC",
                "source_url": "https://example.test/synthetic-article",
            }
            before = copy.deepcopy(synthetic)
            view = self.view(synthetic)
            self.assertEqual((view["id"], view["date"]), (entry["id"], entry["date"]))
            self.assertEqual(view["hosts"], synthetic["hosts"])
            self.assertEqual(view["zone"], synthetic["zone"])
            self.assertEqual(view["source_url"], synthetic["source_url"])
            self.assertEqual(view["audio_url"], "")
            self.assertEqual(view["audio_availability"]["podcast"]["status"], "unavailable")
            self.assertTrue(all("seconds" not in chapter for chapter in view["chapters"]))
            self.assertEqual(view["legacy_compatibility"]["evidence"], "unknown")
            self.assertEqual(synthetic, before)
            self.assertEqual(list(self.audio.iterdir()), [])

    def test_only_a_real_decodable_synthetic_audio_fixture_enables_main_audio(self):
        # The byte source is repository seed ep-139, renamed solely for this test.
        # It says nothing about the operator's ep-189 file or current playback.
        target = self.audio / "ep-189.mp3"
        shutil.copyfile(ROOT / "seed_data" / "audio" / "ep-139.mp3", target)
        synthetic = {"id": "ep-189", "audio_url": "/audio/ep-189.mp3",
                     "audio_availability": {"podcast": {"status": "available"}}}
        before = target.read_bytes()
        view = self.view(synthetic)
        self.assertEqual(view["audio_url"], "/audio/ep-189.mp3")
        self.assertEqual(view["audio_availability"]["podcast"]["status"], "available")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(synthetic["audio_url"], "/audio/ep-189.mp3")

    def test_mismatched_saved_identity_is_rejected_without_rewriting_record(self):
        record = self.episodes / "ep-198.json"
        record.write_text(json.dumps({"id": "ep-199", "date": "Oct 15, 2026"}))
        before = record.read_bytes()
        with patch.object(app_main, "EPISODES_DIR", str(self.episodes)):
            with self.assertRaises(app_main.HTTPException) as raised:
                asyncio.run(app_main.get_episode_detail("ep-198", True))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(list(self.audio.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

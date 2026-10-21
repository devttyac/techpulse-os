"""Non-destructive, versioned compatibility view of retained episodes.

This module does not infer full-article capture from a title, summary, an
unverified ``full_articles`` field, or the absence of a saved file. It never
writes an episode or performs acquisition, generation, or migration.
"""

from dataclasses import dataclass
from copy import deepcopy
import re

from src.story_manifest import retained_manifest_kind


_VERSION = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RetainedEpisodeClassification:
    provenance: str
    evidence: str
    label: str
    full_article_features: str
    reason: str


def classify_retained_episode(episode: dict) -> RetainedEpisodeClassification:
    """Classify only affirmative saved provenance; unknown stays unknown."""
    if not isinstance(episode, dict):
        raise ValueError("retained episode must be an object")
    manifest_kind = retained_manifest_kind(episode)
    capture = episode.get("legacy_evidence")
    if capture is not None:
        if (type(capture) is not dict or set(capture) !=
                {"schema_version", "capture_status", "version_ids"}
                or type(capture["schema_version"]) is not int
                or capture["schema_version"] != 1
                or capture["capture_status"] != "captured"
                or type(capture["version_ids"]) is not list
                or not capture["version_ids"]
                or any(type(item) is not str or not _VERSION.fullmatch(item)
                       for item in capture["version_ids"])
                or len(set(capture["version_ids"])) != len(capture["version_ids"])):
            raise ValueError("invalid retained capture claim")
        if manifest_kind == "rss_v1" or episode.get("content_basis") == "rss_summaries":
            raise ValueError("contradictory retained evidence provenance")
        # A capture claim without separately verified saved unit/index pairs
        # never authorizes full-article features. Later joined consumers may
        # resolve the exact versions through EvidenceStore.
        return RetainedEpisodeClassification(
            "captured", "captured_unavailable", "Captured evidence unavailable",
            "unavailable", "Saved captured source/index not verified")
    if manifest_kind == "rss_v1" or episode.get("content_basis") == "rss_summaries":
        return RetainedEpisodeClassification(
            "confirmed_rss", "never_captured", "Legacy episode — RSS summaries",
            "unavailable", "No admitted saved full-article evidence")
    return RetainedEpisodeClassification(
        "unknown", "unknown", "Source provenance unknown", "unavailable",
        "No admitted saved full-article evidence; capture history unknown")


def project_retained_episode(episode: dict, *, audio_available: bool) -> dict:
    """Add truthful feature status to a detached response, leaving bytes intact."""
    classification = classify_retained_episode(episode)
    projected = deepcopy(episode)
    projected["legacy_compatibility"] = {
        "schema_version": 1,
        "provenance": classification.provenance,
        "evidence": classification.evidence,
        "label": classification.label,
        "full_article_features": classification.full_article_features,
        "reason": classification.reason,
    }
    if not audio_available:
        if "audio_url" in projected:
            projected["audio_url"] = ""
        availability = projected.setdefault("audio_availability", {})
        if not isinstance(availability, dict):
            availability = projected["audio_availability"] = {}
        podcast = availability.setdefault("podcast", {})
        if not isinstance(podcast, dict):
            podcast = availability["podcast"] = {}
        podcast.update(status="unavailable", reason="Retained audio not verified")
    return projected

"""TP07/TP08 boundaries: discovery disabled; controlled fetch is explicit.

Publisher-body acquisition and admission belong to TP08–TP11. This module
cannot supply a new article corpus to the legacy synthesis pipeline.
"""

from dataclasses import dataclass
import hashlib
from urllib.parse import urlsplit

from src.acquisition.tldr import discover_live
from src.acquisition.http import fetch_publisher
from src.acquisition.html import HtmlExtractionResult, extract_html
from src.acquisition.readiness import _load_registry
from src.acquisition.publication import PublicationAssessment, assess_publication, unavailable
from src.content_availability import DOMAIN_ORDER, LEGACY_SOURCE_NAMES
from src.provenance import discovery_record

# Retained legacy tests and records may still refer to this name. It has no
# active feed entries and cannot launch an old source or fallback request.
DOMAIN_FEEDS = {domain: [{"name": name} for name in names]
                for domain, names in LEGACY_SOURCE_NAMES.items()}


async def fetch_candidate(url, profile_id, **transport_options):
    """Expose guarded acquisition without attaching it to publication.

    TP09–TP11 must validate, type and admit fetched bytes before any source
    can appear in a product corpus. The shipped profile registry is empty.
    """
    return await fetch_publisher(url, profile_id, **transport_options)


@dataclass(frozen=True)
class FetchExtractionCandidate:
    reason: str
    fetch: object | None = None
    extraction: HtmlExtractionResult | None = None

    @property
    def blocks(self):
        return self.extraction.blocks if self.extraction is not None else ()


async def fetch_and_extract_candidate(url, profile_id, **transport_options):
    """Return an unadmitted candidate from fixed profile rules only.

    The shipped registry is empty. This path never attaches its output to
    ingest_all_domains, SourceUnit storage or a published episode.
    """
    fetched = await fetch_candidate(url, profile_id, **transport_options)
    if fetched.reason != "fetched":
        return FetchExtractionCandidate(fetched.reason, fetched)
    if (type(fetched.raw_bytes) is not bytes or not fetched.raw_bytes
            or hashlib.sha256(fetched.raw_bytes).hexdigest() != fetched.raw_sha256
            or type(fetched.final_url) is not str
            or type(fetched.safe_headers) is not dict
            or not fetched.safe_headers.get("content-type", "").lower().startswith("text/html")):
        return FetchExtractionCandidate("invalid_fetch", fetched)
    registry = _load_registry()
    try:
        host = urlsplit(fetched.final_url).hostname
    except ValueError:
        host = None
    matches = [] if registry is None else [record["profile"] for record in registry["profiles"]
            if type(record) is dict and type(record.get("profile")) is dict
            and record["profile"].get("profile_id") == profile_id
            and record["profile"].get("publisher_host") == host
            and record["profile"].get("revision") == fetched.profile_revision
            and type(record["profile"].get("connector")) is dict
            and record["profile"]["connector"].get("id") == fetched.client
            and record["profile"]["connector"].get("revision") == fetched.client_revision]
    if (len(matches) != 1 or type(matches[0].get("html_rules")) is not dict
            or matches[0]["html_rules"].get("revision")
               != matches[0].get("structural_rule_revision")):
        return FetchExtractionCandidate("unreviewed_rules", fetched)
    extracted = extract_html(fetched.raw_bytes, fetched.final_url, matches[0]["html_rules"])
    return FetchExtractionCandidate(extracted.reason, fetched, extracted)


@dataclass(frozen=True)
class FetchPublicationCandidate:
    """Same-byte extraction and date evidence, still outside source admission."""

    reason: str
    fetch: object | None
    extraction: HtmlExtractionResult | None
    publication: PublicationAssessment


async def fetch_and_assess_candidate(url, profile_id, window, zone_rules,
                                     **transport_options):
    """Resolve date only after TP09 validated the exact TP08 response bytes.

    The profile comes from the fixed shipped registry, not caller supplied
    rules. This result never enters ingest_all_domains or evidence storage.
    """
    extracted = await fetch_and_extract_candidate(url, profile_id,
                                                   **transport_options)
    if extracted.reason != "validated":
        return FetchPublicationCandidate(extracted.reason, extracted.fetch,
                                         extracted.extraction,
                                         unavailable(extracted.reason, source_url=url))
    fetched = extracted.fetch
    registry = _load_registry()
    try:
        host = urlsplit(fetched.final_url).hostname
    except (ValueError, TypeError):
        host = None
    matches = [] if registry is None else [record["profile"] for record in registry["profiles"]
        if type(record) is dict and type(record.get("profile")) is dict
        and record["profile"].get("profile_id") == profile_id
        and record["profile"].get("publisher_host") == host
        and record["profile"].get("revision") == fetched.profile_revision
        and type(record["profile"].get("connector")) is dict
        and type(record["profile"].get("html_rules")) is dict
        and type(record["profile"].get("date_rules")) is dict
        and record["profile"]["connector"].get("id") == fetched.client
        and record["profile"]["connector"].get("revision") == fetched.client_revision
        and type(record["profile"].get("date_rule_revision")) is str
        and bool(record["profile"]["date_rule_revision"])
        and record["profile"]["date_rule_revision"]
            == record["profile"]["date_rules"].get("revision")
        and record["profile"]["date_rules"].get("article_xpath")
            == record["profile"]["html_rules"].get("article_xpath")]
    if len(matches) != 1:
        publication = unavailable("unreviewed_rules", source_url=fetched.final_url)
    else:
        profile = matches[0]
        publication = assess_publication(fetched.raw_bytes, fetched.final_url,
            profile["date_rules"], window, zone_rules,
            source_kind=profile.get("source_kind", "publisher_article"))
    return FetchPublicationCandidate(extracted.reason, fetched,
                                     extracted.extraction, publication)


async def ingest_all_domains(*, diagnostics=None):
    """Return no article evidence while discovery interface/use is unverified."""
    result = await discover_live()
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics["discovery"] = discovery_record(result)
        diagnostics["per_domain"] = {domain: 0 for domain in DOMAIN_ORDER}
        diagnostics["feeds_failed"] = 0
        diagnostics["links_rejected"] = 0
    return {domain: [] for domain in DOMAIN_ORDER}

"""TP07/TP08 boundaries: discovery disabled; controlled fetch is explicit.

Publisher-body acquisition and admission belong to TP08–TP11. This module
cannot supply a new article corpus to the legacy synthesis pipeline.
"""

from src.acquisition.tldr import discover_live
from src.acquisition.http import fetch_publisher
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

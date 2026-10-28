"""TP07 discovery boundary: only approved TLDR editions, live use disabled.

Publisher-body acquisition and admission belong to TP08–TP11. This module
cannot supply a new article corpus to the legacy synthesis pipeline.
"""

from src.acquisition.tldr import discover_live
from src.content_availability import DOMAIN_ORDER, LEGACY_SOURCE_NAMES
from src.provenance import discovery_record

# Retained legacy tests and records may still refer to this name. It has no
# active feed entries and cannot launch an old source or fallback request.
DOMAIN_FEEDS = {domain: [{"name": name} for name in names]
                for domain, names in LEGACY_SOURCE_NAMES.items()}


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

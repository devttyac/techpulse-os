"""The approved discovery membership, independent of product domains."""

CATALOGUE_REVISION = 1
EDITION_IDS = ("ai", "infosec", "data", "devops", "it", "dev")

_EDITIONS = {
    selected: {"id": selected, "name": f"TLDR {selected.upper() if selected in ('ai', 'it') else selected.title()}",
               "landing": f"https://tldr.tech/{selected}",
               "archive": f"https://tldr.tech/{selected}/archives"}
    for selected in EDITION_IDS
}
_EDITIONS["infosec"]["name"] = "TLDR InfoSec"
_EDITIONS["devops"]["name"] = "TLDR DevOps"


def edition(edition_id):
    """Return a fixed, copy-safe edition descriptor or reject the request."""
    if edition_id not in _EDITIONS:
        raise ValueError("Edition is not in the approved catalogue")
    return dict(_EDITIONS[edition_id])


def catalogue():
    return tuple(edition(selected) for selected in EDITION_IDS)

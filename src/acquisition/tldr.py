"""Offline TP07 discovery prototype. Live use requires a separately proven interface."""

import ipaddress
import re
from urllib.parse import urlsplit

from src.acquisition.catalogue import CATALOGUE_REVISION, EDITION_IDS, edition

MAX_URL_LENGTH = 2048
MAX_LINKS_PER_ISSUE = 500
SPONSOR_PLACEMENTS = frozenset({"sponsor", "sponsored", "advertisement", "advertorial", "paid"})


def _public_url(value):
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            return False
        if host in ("localhost", "localhost.localdomain") or host.endswith((".localhost", ".local", ".internal")):
            return False
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "." in host and not any(ch.isspace() for ch in host)
        return address.is_global
    except ValueError:
        return False


def _issue_ok(value, selected):
    if not isinstance(value, dict) or value.get("edition_id") != selected:
        return False
    issue_id = value.get("issue_id")
    issue_url = value.get("issue_url")
    if not isinstance(issue_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", issue_id):
        return False
    if not _public_url(issue_url):
        return False
    parts = urlsplit(issue_url)
    return (parts.scheme == "https" and parts.hostname == "tldr.tech" and
            parts.port is None and parts.path == f"/{selected}/issues/{issue_id}" and
            not parts.query and not parts.fragment)


def _empty_result(status):
    return {"schema_version": 1, "catalogue_revision": CATALOGUE_REVISION,
            "status": status, "candidates": [], "editions": []}


def discover_from_fixtures(fixtures):
    """Parse normalized synthetic issues; this is not an adopted live TLDR adapter."""
    if not isinstance(fixtures, dict):
        raise ValueError("Fixture map is invalid")
    for selected in fixtures:
        edition(selected)
    result = _empty_result("fixture_only")
    seen = {}
    for selected in EDITION_IDS:
        item = {"edition_id": selected, "name": edition(selected)["name"],
                "outcome": "not_checked", "issue_id": None, "issue_url": None,
                "received": 0, "rejected": 0, "sponsor_excluded": 0,
                "placement_unresolved": 0, "candidates": 0}
        result["editions"].append(item)
        if selected not in fixtures:
            continue
        issue = fixtures[selected]
        if not _issue_ok(issue, selected) or not isinstance(issue.get("links"), list) or len(issue["links"]) > MAX_LINKS_PER_ISSUE:
            item["outcome"] = "parse_error"
            continue
        item["issue_id"] = issue["issue_id"]
        item["issue_url"] = issue["issue_url"]
        for raw in issue["links"]:
            item["received"] += 1
            placement = raw.get("placement") if isinstance(raw, dict) else None
            placement = placement.strip().lower() if isinstance(placement, str) else None
            if placement in SPONSOR_PLACEMENTS:
                item["sponsor_excluded"] += 1
                item["rejected"] += 1
                continue
            if placement != "editorial":
                item["placement_unresolved"] += 1
                item["rejected"] += 1
                continue
            url, title = raw.get("url"), raw.get("title")
            if not _public_url(url) or not isinstance(title, str) or not title.strip():
                item["rejected"] += 1
                continue
            association = {"edition_id": selected, "issue_id": issue["issue_id"],
                           "issue_url": issue["issue_url"]}
            if url in seen:
                if association in seen[url]["associations"]:
                    continue
                seen[url]["associations"].append(association)
            else:
                candidate = {"url": url, "title": title.strip(), "evidence_status": "discovery_only",
                             "sponsor": False, "catalogue_revision": CATALOGUE_REVISION,
                             "associations": [association]}
                seen[url] = candidate
                result["candidates"].append(candidate)
            item["candidates"] += 1
        if item["placement_unresolved"]:
            item["outcome"] = "candidate_partial" if item["candidates"] else "placement_unresolved"
        else:
            item["outcome"] = "candidate" if item["candidates"] else "valid_empty"
    if all(item["outcome"] == "parse_error" for item in result["editions"]):
        result["status"] = "failed"
    elif any(item["outcome"] in ("parse_error", "not_checked", "candidate_partial", "placement_unresolved") for item in result["editions"]):
        result["status"] = "partial"
    return result


async def discover_live(*, client=None):
    """No live request exists until documented interface and use gates are met."""
    result = _empty_result("disabled")
    result["reason"] = "tldr_interface_and_discovery_use_unverified"
    result["editions"] = [{"edition_id": selected, "name": edition(selected)["name"],
                           "outcome": "disabled", "issue_id": None, "issue_url": None,
                           "received": 0, "rejected": 0, "sponsor_excluded": 0,
                           "placement_unresolved": 0, "candidates": 0}
                          for selected in EDITION_IDS]
    return result

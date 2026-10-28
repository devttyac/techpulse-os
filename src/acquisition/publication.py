"""Unadmitted, profile-bound publisher-date assessment for a frozen run.

This module does not grant source rights, completeness or admission. It only
interprets source-owned date evidence from the bytes already fetched by TP08.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from urllib.parse import urlsplit

from lxml import etree, html

from src.evidence_contracts import DateEvidence, PublicationProvenance
from src.run_coordinator import FrozenWindow, ZoneRules, capture_zone_rules


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_OFFSETLESS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\Z")
_EVENTS = frozenset({"publisher_publication", "original_media_release",
                     "transcript_publication", "retrieval", "revision", "modification"})
_SEMANTICS = {("data-event", event): event for event in _EVENTS}
_SEMANTICS.update({
    ("property", "article:published_time"): "publisher_publication",
    ("property", "article:modified_time"): "modification",
    ("itemprop", "datePublished"): "publisher_publication",
    ("itemprop", "dateModified"): "modification",
})


@dataclass(frozen=True)
class DateObservation:
    event_type: str
    raw_value: str
    source_location: str
    owner: str
    precision: str
    offset: str
    local_date: date | None
    utc_instant: datetime | None
    explanation: str
    source_date: date | None = None
    publisher_timezone: str = ""
    timezone_owner: str = ""
    timezone_source_location: str = ""
    timezone_rule_sha256: str = ""


@dataclass(frozen=True)
class PublicationAssessment:
    eligible: bool
    reason: str
    provenance: PublicationProvenance
    observations: tuple[DateObservation, ...] = ()
    local_date: date | None = None
    utc_instant: datetime | None = None


def unavailable(reason, publisher="", source_url=""):
    """Return a bounded non-admission result when upstream evidence is absent."""
    return PublicationAssessment(False, reason, PublicationProvenance(
        publisher, source_url, ()))


def _parse(raw, zone):
    if _DAY.fullmatch(raw):
        try:
            return "day", "", date.fromisoformat(raw), None, ""
        except ValueError:
            pass
    if _INSTANT.fullmatch(raw):
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if instant.utcoffset() is not None:
                utc = instant.astimezone(timezone.utc)
                return "instant", raw[-1] if raw.endswith("Z") else raw[-6:], \
                    utc.astimezone(zone).date(), utc, ""
        except ValueError:
            pass
    return "unknown", "", None, None, ("offsetless_clock" if _OFFSETLESS.fullmatch(raw)
                                         else "unsupported_date")


def _declared_instant(raw, zone_id, run_zone):
    """Resolve a publisher-declared IANA zone only when the wall time is unique."""
    if not _OFFSETLESS.fullmatch(raw):
        return None
    try:
        nominal = datetime.fromisoformat(raw)
        publisher_rules = capture_zone_rules(zone_id)
        publisher_zone = publisher_rules.zone()
    except (ValueError, TypeError):
        return None
    candidates = []
    for fold in (0, 1):
        local = nominal.replace(tzinfo=publisher_zone, fold=fold)
        instant = local.astimezone(timezone.utc)
        if (instant.astimezone(publisher_zone).replace(tzinfo=None) == nominal
                and instant not in candidates):
            candidates.append(instant)
    if len(candidates) != 1:
        return None
    instant = candidates[0]
    offset = nominal.replace(tzinfo=publisher_zone).utcoffset()
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    offset_text = f"{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"
    return offset_text, instant.astimezone(run_zone).date(), instant, publisher_rules.sha256


def _event(e_type, observations, publisher):
    owned = [item for item in observations if item.event_type == e_type
             and item.owner == publisher]
    known = [item for item in owned if item.precision != "unknown"]
    unknown_clocks = [item for item in owned if item.explanation == "offsetless_clock"]
    if not known:
        return DateEvidence(e_type, "unknown", "", "", "unknown", "", publisher,
                            (), "no_supported_source_owned_date")
    instants = {item.utc_instant for item in known if item.utc_instant is not None}
    day_items = [item for item in known if item.precision == "day"]
    instant_items = [item for item in known if item.utc_instant is not None]
    stated_days = {item.source_date for item in day_items}
    day_instant_disagreement = False
    for day_item in day_items:
        for instant_item in instant_items:
            comparison_day = instant_item.source_date
            if day_item.timezone_rule_sha256:
                try:
                    day_rules = capture_zone_rules(day_item.publisher_timezone)
                    if day_rules.sha256 != day_item.timezone_rule_sha256:
                        raise ValueError("publisher timezone rules changed")
                    comparison_day = instant_item.utc_instant.astimezone(day_rules.zone()).date()
                except ValueError:
                    day_instant_disagreement = True
                    continue
            if day_item.source_date != comparison_day:
                day_instant_disagreement = True
    clock_days = set()
    for item in unknown_clocks:
        try:
            clock_days.add(date.fromisoformat(item.raw_value[:10]))
        except ValueError:
            pass
    if (len(instants) > 1 or len(stated_days) > 1 or day_instant_disagreement
            or (clock_days and stated_days and not clock_days <= stated_days)):
        values = tuple(dict.fromkeys(item.raw_value for item in known + unknown_clocks))
        return DateEvidence(e_type, "conflict", "", "", "unknown", "", publisher,
                            values, "source_owned_publication_disagreement")
    # A supported exact instant dominates a matching day. An offsetless clock
    # never becomes an instant and cannot outrank a separately stated day.
    selected = next((item for item in known if item.utc_instant is not None), known[0])
    if unknown_clocks and selected.utc_instant is None:
        selected = next(item for item in known if item.precision == "day")
    return DateEvidence(e_type, "known", selected.raw_value, selected.source_location,
                        selected.precision, selected.offset, publisher, (), "")


def assess_publication(raw_bytes, source_url, date_rules, window, zone_rules,
                       *, source_kind="publisher_article"):
    """Compare only source-owned events under fixed, versioned profile rules.

    A profile's article XPath identifies the same document whose body TP09
    selected. Relative date XPath cannot select an unrelated page card.
    """
    publisher = date_rules.get("publisher", "") if type(date_rules) is dict else ""
    empty = lambda reason: unavailable(reason, publisher, source_url)
    if (type(raw_bytes) is not bytes or not raw_bytes or type(source_url) is not str
            or type(date_rules) is not dict or type(date_rules.get("revision")) is not str
            or not date_rules["revision"] or type(publisher) is not str or not publisher
            or source_kind not in ("publisher_article", "publisher_transcript",
                                   "technical_documentation", "research_paper", "repository", "discussion")):
        return empty("unreviewed_rules")
    try:
        host = urlsplit(source_url).hostname
    except ValueError:
        host = None
    if host != publisher:
        return empty("unowned_source")
    if (not isinstance(window, FrozenWindow) or not isinstance(zone_rules, ZoneRules)
            or window.timezone_id != zone_rules.zone_id
            or window.rule_sha256 != zone_rules.sha256
            or window.upper_utc.tzinfo is None):
        return empty("invalid_window")
    try:
        zone = zone_rules.zone()
    except ValueError:
        return empty("invalid_window")
    article_xpath = date_rules.get("article_xpath")
    header_xpath = date_rules.get("header_xpath")
    fields = date_rules.get("fields")
    if (not isinstance(article_xpath, str) or not article_xpath.startswith("//article")
            or not isinstance(header_xpath, str) or not header_xpath.startswith(".//")
            or type(fields) is not list or not fields):
        return empty("unreviewed_rules")
    for field in fields:
        if (type(field) is not dict or type(field.get("event_type")) is not str
                or field["event_type"] not in _EVENTS
                or type(field.get("semantic_attribute")) is not str
                or type(field.get("semantic_value")) is not str
                or _SEMANTICS.get((field["semantic_attribute"],
                                   field["semantic_value"])) != field["event_type"]
                or field.get("scope") not in ("article_header", "head")
                or field.get("value_attribute") not in ("datetime", "content")
                or not isinstance(field.get("xpath"), str)
                or not field["xpath"].startswith(".//")
                or (field["scope"] == "article_header" and
                    (not isinstance(field.get("owner_attribute"), str)
                     or not field["owner_attribute"].startswith("data-")
                     or field.get("timezone_attribute", "data-timezone")
                        != "data-timezone"
                     or field.get("timezone_owner_attribute", "data-timezone-owner")
                        != "data-timezone-owner"))):
            return empty("unreviewed_rules")
    try:
        document = html.fromstring(raw_bytes, parser=html.HTMLParser(
            no_network=True, recover=True, huge_tree=False))
        articles = document.xpath(article_xpath)
        if (len(articles) != 1 or not isinstance(articles[0], etree._Element)
                or articles[0].tag != "article"):
            return empty("unowned_source")
        headers = articles[0].xpath(header_xpath)
        if (any(field["scope"] == "article_header" for field in fields)
                and (len(headers) != 1 or not isinstance(headers[0], etree._Element)
                     or headers[0].tag != "header"
                     or articles[0] not in headers[0].iterancestors())):
            return empty("unreviewed_rules")
        heads = document.xpath("/html/head")
    except (etree.LxmlError, ValueError, TypeError, OverflowError):
        return empty("invalid_source")
    observations = []
    seen_locations = set()
    for field in fields:
        scope = field["scope"]
        if scope == "head":
            if len(heads) != 1:
                return empty("unowned_source")
            canonical = heads[0].xpath(".//link[@rel='canonical']/@href")
            if canonical != [source_url]:
                return empty("unowned_source")
            container = heads[0]
            expected_tag = "meta"
        else:
            container = headers[0]
            expected_tag = "time"
        try:
            nodes = container.xpath(field["xpath"])
        except (etree.LxmlError, ValueError, TypeError, OverflowError):
            return empty("unreviewed_rules")
        for node in nodes:
            if (not isinstance(node, etree._Element) or node.tag != expected_tag
                    or container not in node.iterancestors()):
                return empty("unreviewed_rules")
            if node.get(field["semantic_attribute"]) != field["semantic_value"]:
                return empty("unreviewed_rules")
            location = document.getroottree().getpath(node)
            if location in seen_locations:
                return empty("unreviewed_rules")
            seen_locations.add(location)
            value_attr = field["value_attribute"]
            raw = node.get(value_attr, "").strip()
            owner = (node.get(field["owner_attribute"], "") if scope == "article_header"
                     else publisher)
            precision, offset, local_day, utc, explanation = _parse(raw, zone)
            source_day = (date.fromisoformat(raw[:10]) if precision != "unknown"
                          else None)
            declared_zone = ""
            timezone_owner = ""
            declared_location = ""
            declared_rule_sha = ""
            if scope == "article_header":
                declared_zone = node.get("data-timezone", "")
                timezone_owner = (node.get("data-timezone-owner", "")
                                  if declared_zone else "")
                declared_location = (f"{location}/@data-timezone" if declared_zone else "")
                if (declared_zone and owner == publisher
                        and timezone_owner == publisher):
                    if precision == "unknown" and explanation == "offsetless_clock":
                        resolved = _declared_instant(raw, declared_zone, zone)
                        if resolved is not None:
                            offset, local_day, utc, declared_rule_sha = resolved
                            precision, explanation = "instant", ""
                            source_day = date.fromisoformat(raw[:10])
                    elif precision in ("day", "instant"):
                        try:
                            publisher_rules = capture_zone_rules(declared_zone)
                            publisher_zone = publisher_rules.zone()
                        except ValueError:
                            pass
                        else:
                            declared_rule_sha = publisher_rules.sha256
                            if utc is not None:
                                source_day = utc.astimezone(publisher_zone).date()
                    if declared_rule_sha:
                        location = f"{location}/@{value_attr};{declared_location}"
            source_location = (location if declared_rule_sha else f"{location}/@{value_attr}")
            observations.append(DateObservation(field["event_type"], raw,
                source_location, owner, precision, offset, local_day, utc,
                explanation, source_day, declared_zone, timezone_owner,
                declared_location,
                declared_rule_sha))
    required = ("original_media_release" if source_kind == "publisher_transcript"
                else "publisher_publication")
    event_types = list(dict.fromkeys([required] + [item.event_type for item in observations]))
    events = tuple(_event(name, observations, publisher) for name in event_types)
    provenance = PublicationProvenance(publisher, source_url, events)
    selected = events[0]
    if selected.status == "unknown":
        return PublicationAssessment(False, "unknown_publication", provenance,
                                     tuple(observations))
    if selected.status == "conflict":
        return PublicationAssessment(False, "conflicting_publication", provenance,
                                     tuple(observations))
    selected_observation = next((item for item in observations
        if item.event_type == selected.event_type and item.owner == publisher
        and item.raw_value == selected.raw_value
        and item.source_location == selected.source_location), None)
    local_day = selected_observation.local_date if selected_observation else None
    utc = selected_observation.utc_instant if selected_observation else None
    if local_day is None:
        return PublicationAssessment(False, "unknown_publication", provenance,
                                     tuple(observations))
    if local_day < window.lower_date or local_day > window.local_date:
        reason = "future_publication" if local_day > window.local_date else "outside_window"
    elif utc is not None and utc > window.upper_utc:
        reason = "future_publication"
    elif utc is not None and utc < window.lower_utc:
        reason = "outside_window"
    else:
        reason = "eligible"
    return PublicationAssessment(reason == "eligible", reason, provenance,
                                 tuple(observations), local_day, utc)

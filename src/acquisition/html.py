"""Two offline HTML body selectors; output is a candidate, never admission."""

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
import trafilatura

from src.acquisition.html_validation import (_inventory, _nodes, _text,
                                             inspect_source, validate_extracted_body)
from src.evidence_contracts import SourceBlock


@dataclass(frozen=True)
class HtmlExtractionResult:
    reason: str
    path: str | None = None
    blocks: tuple[SourceBlock, ...] = ()
    source_structure_ref: str | None = None
    path_a_reason: str | None = None
    path_b_reason: str | None = None


def _xml_text(node, source_url):
    pieces = [node.text or ""]
    for child in node:
        if child.tag == "ref" and child.get("target"):
            target = urljoin(source_url, child.get("target"))
            label = _text("".join(child.itertext()))
            if urlsplit(target).scheme in ("http", "https"):
                pieces.append(f"[{label}]({target})")
            else:
                pieces.append(label)
        else:
            pieces.append(_xml_text(child, source_url))
        pieces.append(child.tail or "")
    return _text("".join(pieces))


def _path_a(raw_bytes, source_url, rules):
    """Trafilatura XML path; it cannot borrow the DOM path's selected body."""
    root = html.fromstring(raw_bytes, parser=html.HTMLParser(
        no_network=True, recover=True, huge_tree=False))
    article = _nodes(root, rules["article_xpath"])[0]
    title = _text("".join(_nodes(article, rules["title_xpath"])[0].itertext()))
    body = _nodes(article, rules["body_xpath"])[0]
    title_outside_body = _nodes(body, rules["title_xpath"]) == []
    xml = trafilatura.extract(raw_bytes, output_format="xml", fast=True,
                             include_comments=False, include_links=True,
                             include_tables=True, include_formatting=True)
    if not xml:
        return ()
    try:
        document = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(
            resolve_entities=False, no_network=True, huge_tree=False))
    except (etree.XMLSyntaxError, ValueError):
        return ()
    main = document.find("main")
    if main is None:
        return ()
    selected = []
    for index, node in enumerate(main):
        if (index == 0 and node.tag == "head" and title_outside_body
                and _xml_text(node, source_url) == title):
            continue
        if node.tag in ("p", "head", "code"):
            selected.append(({"p": "paragraph", "head": "heading", "code": "code"}[node.tag],
                             _xml_text(node, source_url)))
        elif node.tag == "list":
            for item in node.findall("item"):
                selected.append(("list", _xml_text(item, source_url)))
        elif node.tag == "table":
            lines = []
            for row in node.findall("row"):
                lines.append(" | ".join(_xml_text(cell, source_url)
                                        for cell in row.findall("cell")))
            selected.append(("table", "\n".join(lines)))
        else:
            return ()
    return tuple(selected)


def _path_b(raw_bytes, source_url, rules):
    """Independently select the declared lxml DOM article body."""
    try:
        root = html.fromstring(raw_bytes, parser=html.HTMLParser(
            no_network=True, recover=True, huge_tree=False))
        articles = _nodes(root, rules["article_xpath"])
        if len(articles) != 1:
            return ()
        bodies = _nodes(articles[0], rules["body_xpath"])
        if len(bodies) != 1:
            return ()
        return _inventory(bodies[0], source_url)
    except (etree.ParserError, ValueError, KeyError, OverflowError, UnicodeError):
        return ()


def extract_html(raw_bytes, source_url, rules):
    """Prefer validated A, then validated B; never join results or fetch again.

    A candidate is only a fixture-scale structural result. Per-publisher
    rights, independent real-article gold and SourceUnit admission remain
    separate gates. No caller-supplied text changes the two path order.
    """
    source = inspect_source(raw_bytes, source_url, rules)
    if source.reason != "source_checked":
        return HtmlExtractionResult(source.reason)
    try:
        candidate_a = _path_a(raw_bytes, source_url, rules)
    except (ValueError, TypeError, etree.LxmlError):
        candidate_a = ()
    selected_a = validate_extracted_body(raw_bytes, source_url, rules, candidate_a)
    if selected_a.reason == "validated":
        return HtmlExtractionResult("validated", "trafilatura", selected_a.blocks,
                                    selected_a.source_structure_ref)
    candidate_b = _path_b(raw_bytes, source_url, rules)
    selected_b = validate_extracted_body(raw_bytes, source_url, rules, candidate_b)
    if selected_b.reason == "validated":
        return HtmlExtractionResult("validated", "dom", selected_b.blocks,
                                    selected_b.source_structure_ref,
                                    path_a_reason=selected_a.reason)
    return HtmlExtractionResult("no_valid_body", path_a_reason=selected_a.reason,
                                path_b_reason=selected_b.reason)

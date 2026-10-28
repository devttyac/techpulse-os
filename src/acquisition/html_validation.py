"""Source-DOM checks for TP09 fixture extraction; no publisher is enabled."""

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from src.evidence_contracts import (ListPosition, SourceBlock, SourceLocator,
                                    TableCell, TableGrid)


_SPACE = re.compile(r"\s+")
_REQUIRED_RULES = ("revision", "article_xpath", "title_xpath", "body_xpath",
                   "end_xpath", "access_xpath", "continuation_xpath", "excluded_xpath")


@dataclass(frozen=True)
class BodyDecision:
    reason: str
    blocks: tuple[SourceBlock, ...] = ()
    source_structure_ref: str | None = None


def _text(value):
    return _SPACE.sub(" ", value or "").strip()


def _link_text(node, source_url):
    pieces = []
    if node.text:
        pieces.append(node.text)
    for child in node:
        if child.tag == "a" and child.get("href"):
            href = urljoin(source_url, child.get("href"))
            if urlsplit(href).scheme in ("http", "https"):
                pieces.append(f"[{_text(''.join(child.itertext()))}]({href})")
            else:
                pieces.append(_text("".join(child.itertext())))
        else:
            pieces.append(_link_text(child, source_url))
        if child.tail:
            pieces.append(child.tail)
    return _text("".join(pieces))


def _nodes(root, expression):
    try:
        result = root.xpath(expression)
    except (etree.XPathError, TypeError, ValueError):
        raise ValueError("invalid_rule") from None
    if type(result) is not list or any(not isinstance(node, etree._Element) for node in result):
        raise ValueError("invalid_rule")
    return result


def _table(node, source_url):
    rows = node.xpath(".//tr")
    if not rows or any(not row.xpath("./th|./td") for row in rows):
        return None
    width = len(rows[0].xpath("./th|./td"))
    if width < 1 or any(len(row.xpath("./th|./td")) != width for row in rows):
        return None
    cells = []
    for r, row in enumerate(rows):
        for c, cell in enumerate(row.xpath("./th|./td")):
            if cell.get("rowspan") or cell.get("colspan"):
                return None
            value = _link_text(cell, source_url)
            if not value:
                return None
            cells.append(TableCell(r, c, 1, 1, value,
                                   "header" if cell.tag == "th" else "data"))
    if not any(cell.role == "header" for cell in cells):
        return None
    grid = TableGrid(len(rows), width, tuple(cells))
    text = "\n".join(" | ".join(cell.text for cell in cells if cell.row == row)
                     for row in range(len(rows)))
    return grid, text


def _inventory(body, source_url):
    if body.xpath(".//img|.//svg|.//figure|.//blockquote|.//video|.//audio|.//canvas|.//iframe"):
        raise ValueError("unsupported_structure")
    blocks = []
    tree = body.getroottree()

    def append(kind, value, node, structure=None):
        if not value:
            raise ValueError("empty_block")
        blocks.append(SourceBlock(f"b{len(blocks) + 1}", kind, value,
                                  SourceLocator("html", {"selector": tree.getpath(node)}),
                                  structure))

    def walk(node):
        tag = node.tag.lower() if type(node.tag) is str else ""
        if tag in ("script", "style", "noscript"):
            return
        if tag in ("svg", "nav", "aside", "form", "iframe", "figure", "img",
                   "blockquote", "video", "audio", "canvas"):
            raise ValueError("unsupported_structure")
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            append("heading" if tag.startswith("h") else "paragraph",
                   _link_text(node, source_url), node)
            return
        if tag in ("pre", "code"):
            code = "".join(node.itertext()).replace("\r\n", "\n").replace("\r", "\n")
            append("code", code.strip("\n"), node)
            return
        if tag in ("ul", "ol"):
            list_id = f"list-{len(blocks) + 1}"
            for ordinal, item in enumerate(node.xpath("./li"), 1):
                if item.xpath(".//ul|.//ol"):
                    raise ValueError("unsupported_structure")
                append("list", _link_text(item, source_url), item,
                       ListPosition(0, ordinal, None, list_id))
            return
        if tag == "table":
            table = _table(node, source_url)
            if table is None:
                raise ValueError("unsupported_table")
            append("table", table[1], node, table[0])
            return
        if _text(node.text):
            raise ValueError("unsupported_structure")
        for child in node:
            walk(child)
            if _text(child.tail):
                raise ValueError("unsupported_structure")

    walk(body)
    return tuple(blocks)


def inspect_source(raw_bytes, source_url, rules):
    """Inventory the declared article DOM independently of either body picker."""
    if (type(raw_bytes) is not bytes or not raw_bytes or type(source_url) is not str
            or type(rules) is not dict
            or any(type(rules.get(key)) is not str or not rules[key].strip()
                   for key in _REQUIRED_RULES)):
        return BodyDecision("invalid_input")
    try:
        root = html.fromstring(raw_bytes, parser=html.HTMLParser(
            no_network=True, recover=True, huge_tree=False))
        canonicals = _nodes(root, "//head/link[translate(@rel, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='canonical']")
        if len(canonicals) != 1 or canonicals[0].get("href") != source_url:
            return BodyDecision("identity_mismatch")
        articles = _nodes(root, rules["article_xpath"])
        if len(articles) != 1:
            return BodyDecision("identity_mismatch")
        article = articles[0]
        titles = _nodes(article, rules["title_xpath"])
        if len(titles) != 1 or not _text("".join(titles[0].itertext())):
            return BodyDecision("identity_mismatch")
        if _nodes(root, rules["access_xpath"]):
            return BodyDecision("access_gate")
        if _nodes(article, rules["continuation_xpath"]):
            return BodyDecision("unresolved_continuation")
        bodies = _nodes(article, rules["body_xpath"])
        if len(bodies) != 1:
            return BodyDecision("missing_body_boundary")
        ends = _nodes(article, rules["end_xpath"])
        if len(ends) != 1:
            return BodyDecision("missing_end_boundary")
        article_order = list(article.iter())
        if (article_order.index(ends[0]) <= article_order.index(bodies[0])
                or ends[0] in bodies[0].iterdescendants()):
            return BodyDecision("missing_end_boundary")
        if _nodes(bodies[0], rules["excluded_xpath"]):
            return BodyDecision("related_inside_body")
        blocks = _inventory(bodies[0], source_url)
        if not blocks:
            return BodyDecision("empty_body")
        return BodyDecision("source_checked", blocks, bodies[0].getroottree().getpath(bodies[0]))
    except ValueError as exc:
        return BodyDecision(str(exc) if str(exc) in ("unsupported_structure", "invalid_rule")
                            else "invalid_source")
    except (etree.ParserError, OverflowError, UnicodeError):
        return BodyDecision("invalid_source")


def validate_extracted_body(raw_bytes, source_url, rules, candidate):
    """Reject picker output that omits or substitutes any declared body block."""
    source = inspect_source(raw_bytes, source_url, rules)
    if source.reason != "source_checked":
        return source
    if type(candidate) not in (tuple, list):
        return BodyDecision("wrong_body")
    if all(isinstance(item, SourceBlock) for item in candidate):
        matched = tuple(candidate) == source.blocks
    else:
        expected = tuple((block.kind, block.text) for block in source.blocks)
        matched = tuple(candidate) == expected
    if not matched:
        return BodyDecision("wrong_body")
    return BodyDecision("validated", source.blocks, source.source_structure_ref)


def validate_source_structure(profile, unit, raw_representation, members):
    """Admission slot: compare saved blocks with the actual declared source DOM."""
    rules = profile.get("html_rules") if type(profile) is dict else None
    if (type(rules) is not dict or rules.get("revision") != profile.get("structural_rule_revision")
            or members):
        return {"decision": "rejected", "reason": "unreviewed_rules"}
    decision = validate_extracted_body(raw_representation, unit.publication.source_url,
                                       rules, unit.blocks)
    if decision.reason != "validated":
        return {"decision": "rejected", "reason": decision.reason}
    body = "\n".join(block.text for block in unit.blocks).encode("utf-8")
    return {"decision": "complete", "rule_revision": rules["revision"],
            "source_structure_ref": decision.source_structure_ref,
            "source_url": unit.publication.source_url, "unit_scope": unit.unit_scope,
            "raw_sha256": hashlib.sha256(raw_representation).hexdigest(),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "member_ids": [], "member_sha256s": [],
            "checks": ["identity", "body_boundary", "continuation", "structure"]}

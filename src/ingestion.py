import hashlib
import json
import logging
import os
import re
import time
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Any
from urllib.parse import urlparse
from xml.sax import SAXParseException
import feedparser
import httpx
from bs4 import BeautifulSoup
from src.provenance import error_category, article_identity

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.ingestion")

DOMAIN_FEEDS: Dict[str, List[Dict[str, str]]] = {
    "ai": [
        {"name": "Simon Willison (AI Architecture)", "url": "https://simonwillison.net/atom/everything/"},
        {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml"},
        {"name": "Hugging Face", "url": "https://huggingface.co/blog/feed.xml"}
    ],
    "cloud": [
        {"name": "Microsoft Azure Architecture", "url": "https://techcommunity.microsoft.com/t5/s/rss/board?board.id=AzureArchitectureBlog"},
        {"name": "AWS Architecture Blog", "url": "https://aws.amazon.com/blogs/architecture/feed/"},
        {"name": "Google Cloud Blog", "url": "https://cloudblog.withgoogle.com/rss/"}
    ],
    "data": [
        {"name": "Microsoft Fabric Blog", "url": "https://community.fabric.microsoft.com/t5/s/rss/board?board.id=fbc_fabricupdatesblogs"},
        {"name": "Databricks Blog", "url": "https://www.databricks.com/blog/feed.xml"}
    ],
    "sec": [
        {"name": "Cloudflare Engineering", "url": "https://blog.cloudflare.com/rss/"},
        {"name": "CNCF Security", "url": "https://www.cncf.io/blog/feed/"},
        {"name": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"}
    ],
    "devops": [
        {"name": "Kubernetes Official Blog", "url": "https://kubernetes.io/feed.xml"},
        {"name": "SRE Weekly", "url": "https://sreweekly.com/feed/"}
    ],
    "arch": [
        {"name": "Martin Fowler", "url": "https://martinfowler.com/feed.atom"},
        {"name": "InfoQ Architecture", "url": "https://feed.infoq.com/"}
    ],
    "finops": [
        {"name": "FinOps Foundation", "url": "https://www.finops.org/feed/"},
        {"name": "AWS Compute Blog", "url": "https://aws.amazon.com/blogs/compute/feed/"}
    ],
    "gov": [
        {"name": "NIST AI & Cybersecurity", "url": "https://www.nist.gov/news-events/news/rss.xml"}
    ]
}

# Feed links become "Read" links in the SPA and chapter links in podcast apps, so
# only absolute http(s) URLs with a host, at a sane length, may enter the corpus.
MAX_LINK_LENGTH = 2048


def is_safe_link(link: str) -> bool:
    if not isinstance(link, str) or len(link) > MAX_LINK_LENGTH:
        return False
    try:
        parsed = urlparse(link)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def clean_html_summary(html_content: str, max_chars: int = 500) -> str:
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text

async def fetch_feed_items(client: httpx.AsyncClient, domain: str, feed_meta: Dict[str, str], *, diagnostics=None) -> List[Dict[str, Any]]:
    name = feed_meta["name"]
    url = feed_meta["url"]
    items = []
    stats = diagnostics if diagnostics is not None else {}
    stats.update(outcome="running", http_status=None, entries_considered=0, accepted=0,
                 rejected_links=0, missing_fields=0, parse_warning=False)
    started = time.monotonic()

    try:
        response = await client.get(url, timeout=6.0, headers={"User-Agent": "TechPulseOS/1.0 (Automated Ingest)"})
        stats["http_status"] = response.status_code
        if response.status_code != 200:
            stats["outcome"] = "http_error"
            logger.warning(f"Feed [{name}] returned status {response.status_code}")
            return items

        parsed = feedparser.parse(response.text)
        stats["parse_warning"] = bool(parsed.get("bozo"))
        # A valid empty RSS/Atom feed is quiet. An HTML page or unusable parse
        # is unavailable, even when feedparser returned an empty entries list.
        unusable_xml = isinstance(parsed.get("bozo_exception"), SAXParseException) and not parsed.entries
        if (not parsed.get("version") and not parsed.entries) or unusable_xml:
            stats["outcome"] = "parse_error"
            return items
        for entry in parsed.entries[:5]:  # Top 5 most recent entries per feed
            stats["entries_considered"] += 1
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                stats["missing_fields"] += 1
                continue
            if not is_safe_link(link):
                stats["rejected_links"] += 1
                # Same handling as a missing link: the article is skipped.
                logger.warning(f"Feed [{name}] entry skipped: unsafe or oversized link")
                continue

            summary_raw = entry.get("summary", "") or entry.get("description", "")
            summary = clean_html_summary(summary_raw)

            pub_date = entry.get("published", "") or entry.get("updated", "") or datetime.now(timezone.utc).isoformat()
            guid = entry.get("id", link)
            entry_id = hashlib.sha256(guid.encode("utf-8")).hexdigest()[:12]

            items.append({
                "id": entry_id,
                "domain": domain,
                "source_name": name,
                "title": title,
                "url": link,
                "summary": summary,
                "published_at": pub_date
            })
        stats["outcome"] = "success"
    except asyncio.CancelledError:
        stats["outcome"] = "cancelled"
        raise
    except Exception as e:
        stats["outcome"] = "failed"
        stats["error_category"] = error_category(e)
        logger.error("Feed fetch failed (category=%s)", stats["error_category"])
    finally:
        stats["accepted"] = len(items)
        stats["duration_ms"] = round((time.monotonic() - started) * 1000, 3)

    return items

async def ingest_all_domains(*, diagnostics=None) -> Dict[str, List[Dict[str, Any]]]:
    logger.info("Starting ingestion across all 8 technology domains...")
    results: Dict[str, List[Dict[str, Any]]] = {d: [] for d in DOMAIN_FEEDS.keys()}
    stats = diagnostics if diagnostics is not None else {}
    stats.update(feeds=[], per_domain={}, feeds_failed=0, links_rejected=0)

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            tasks = []
            task_meta = []
            for domain, feeds in DOMAIN_FEEDS.items():
                for index, feed in enumerate(feeds):
                    name = feed["name"]
                    feed_stats = {"feed_id": f"{domain}:{index}", "domain": domain,
                                  "name": name if re.fullmatch(r"[A-Za-z0-9 ()&._-]{1,120}", name) else "unrecognized_feed",
                                  "source": article_identity(feed["url"])}
                    stats["feeds"].append(feed_stats)
                    tasks.append(fetch_feed_items(client, domain, feed, diagnostics=feed_stats))
                    task_meta.append((domain, feed["name"]))

            fetched_lists = await asyncio.gather(*tasks, return_exceptions=True)

            for (domain, name), res in zip(task_meta, fetched_lists):
                if isinstance(res, list):
                    results[domain].extend(res)
                    logger.info(f"Domain [{domain}] - Ingested {len(res)} items from [{name}]")
                else:
                    logger.warning("Domain [%s] - Feed task did not return articles", domain)
    finally:
        # Counts mean accepted during this run, including partial cancelled
        # fetches; a feed which never reached its collector remains unknown.
        stats["per_domain"] = {}
        for domain in DOMAIN_FEEDS:
            feeds = [f for f in stats["feeds"] if f["domain"] == domain]
            stats["per_domain"][domain] = (sum(f["accepted"] for f in feeds)
                                          if feeds and all("accepted" in f for f in feeds) else None)
        stats["feeds_failed"] = sum(f.get("outcome") in ("http_error", "failed", "parse_error") for f in stats["feeds"])
        stats["feeds_unfinished"] = sum(f.get("outcome") not in ("http_error", "failed", "parse_error", "success", "cancelled") for f in stats["feeds"])
        stats["links_rejected"] = sum(f.get("rejected_links", 0) for f in stats["feeds"])

    total_articles = sum(len(v) for v in results.values())
    logger.info(f"Ingestion completed. Total articles fetched: {total_articles}")
    return results

if __name__ == "__main__":
    data = asyncio.run(ingest_all_domains())
    print(f"Total domains with articles: {len(data)}")

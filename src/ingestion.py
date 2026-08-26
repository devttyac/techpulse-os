import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Any
import feedparser
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.ingestion")

DOMAIN_FEEDS: Dict[str, List[Dict[str, str]]] = {
    "ai": [
        {"name": "Anthropic Engineering", "url": "https://simonwillison.net/atom/everything/"},
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

def clean_html_summary(html_content: str, max_chars: int = 500) -> str:
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text

async def fetch_feed_items(client: httpx.AsyncClient, domain: str, feed_meta: Dict[str, str]) -> List[Dict[str, Any]]:
    name = feed_meta["name"]
    url = feed_meta["url"]
    items = []

    try:
        response = await client.get(url, timeout=12.0, headers={"User-Agent": "TechPulseOS/1.0 (Automated Ingest)"})
        if response.status_code != 200:
            logger.warning(f"Feed [{name}] returned status {response.status_code}")
            return items

        parsed = feedparser.parse(response.text)
        for entry in parsed.entries[:5]:  # Top 5 most recent entries per feed
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title or not link:
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
    except Exception as e:
        logger.error(f"Error fetching feed [{name}] from {url}: {e}")

    return items

async def ingest_all_domains() -> Dict[str, List[Dict[str, Any]]]:
    logger.info("Starting ingestion across all 8 technology domains...")
    results: Dict[str, List[Dict[str, Any]]] = {d: [] for d in DOMAIN_FEEDS.keys()}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = []
        task_meta = []
        for domain, feeds in DOMAIN_FEEDS.items():
            for feed in feeds:
                tasks.append(fetch_feed_items(client, domain, feed))
                task_meta.append((domain, feed["name"]))

        fetched_lists = await asyncio.gather(*tasks, return_exceptions=True)

        for (domain, name), res in zip(task_meta, fetched_lists):
            if isinstance(res, list):
                results[domain].extend(res)
                logger.info(f"Domain [{domain}] - Ingested {len(res)} items from [{name}]")
            else:
                logger.warning(f"Domain [{domain}] - Failed to ingest from [{name}]: {res}")

    total_articles = sum(len(v) for v in results.values())
    logger.info(f"Ingestion completed. Total articles fetched: {total_articles}")
    return results

if __name__ == "__main__":
    data = asyncio.run(ingest_all_domains())
    print(f"Total domains with articles: {len(data)}")
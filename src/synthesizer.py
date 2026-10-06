import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Optional

from pydantic import BaseModel
from src.provenance import error_category, model_identifier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.synthesizer")

# Fixed domain ordering used throughout synthesis: prompt instructions, structural
# validation, the deterministic fallback, and the takeaways schema all key off this list.
DOMAIN_ORDER: List[str] = ["ai", "cloud", "data", "sec", "devops", "arch", "finops", "gov"]

# --- Timezone helper -------------------------------------------------------
# Cron runs at 07:00 Asia/Singapore == 23:00 UTC the previous day, so any
# UTC-based "today" stamp was labeling every episode with yesterday's date.
# created_at intentionally stays UTC (audit timestamps) — only display dates
# (the "date" field) should use local wall-clock time.
APP_TZ = ZoneInfo(os.getenv("TZ", "Asia/Singapore"))


def local_now() -> datetime:
    return datetime.now(APP_TZ)


# --- Pydantic response schema ------------------------------------------------
# Used both as documentation of the required shape and, where accepted by the
# google-genai client, as the structured response_schema passed to
# generate_content so the model is constrained server-side rather than by a
# worked example in the prompt (the worked example is exactly what caused the
# original bug: Gemini copied the specimen's values instead of generating new
# ones). See BRIEFING_JSON_SCHEMA below for the fallback path if the nested
# Takeaways model is rejected (googleapis/python-genai issue #60).

class Source(BaseModel):
    title: str
    url: str


class Chapter(BaseModel):
    title: str
    source_name: str
    source_url: str


class ScriptSegment(BaseModel):
    speaker: str
    text: str
    chapter_title: str


class Takeaway(BaseModel):
    badge: str
    release_date: str
    title: str
    bullets: List[str]
    interview_framing: str
    sources: List[Source]


class Takeaways(BaseModel):
    # Deliberately eight REQUIRED fields (not a Dict[str, Takeaway]) so the
    # 1-of-8 collapse that caused the original bug is structurally impossible:
    # a response missing any domain fails schema validation outright.
    ai: Takeaway
    cloud: Takeaway
    data: Takeaway
    sec: Takeaway
    devops: Takeaway
    arch: Takeaway
    finops: Takeaway
    gov: Takeaway


class Flashcard(BaseModel):
    domain: str
    question: str
    answer: str
    cite: str
    color_class: str


class Briefing(BaseModel):
    title: str
    summary: str
    hosts: str
    script_segments: List[ScriptSegment]
    chapters: List[Chapter]
    takeaways: Takeaways
    flashcards: List[Flashcard]


# Explicit JSON-schema fallback expressing the same "8 required takeaway keys"
# constraint as the Takeaways model above, for use if google-genai rejects the
# nested Pydantic model as a response_schema (see googleapis/python-genai #60).
def _domain_takeaway_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "badge": {"type": "string"},
            "release_date": {"type": "string"},
            "title": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "interview_framing": {"type": "string"},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["title", "url"],
                },
            },
        },
        "required": ["badge", "release_date", "title", "bullets", "interview_framing", "sources"],
    }


BRIEFING_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "hosts": {"type": "string"},
        "script_segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                    "chapter_title": {"type": "string"},
                },
                "required": ["speaker", "text", "chapter_title"],
            },
        },
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source_name": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["title", "source_name", "source_url"],
            },
        },
        "takeaways": {
            "type": "object",
            "properties": {domain: _domain_takeaway_schema() for domain in DOMAIN_ORDER},
            "required": list(DOMAIN_ORDER),
        },
        "flashcards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "cite": {"type": "string"},
                    "color_class": {"type": "string"},
                },
                "required": ["domain", "question", "answer", "cite", "color_class"],
            },
        },
    },
    "required": ["title", "summary", "hosts", "script_segments", "chapters", "takeaways", "flashcards"],
}

# --- Schema-rejection classification -----------------------------------------
# Indicators that an exception represents the API server rejecting the
# response_schema argument itself, as opposed to a transient transport, auth,
# or quota failure that happened to occur on the same call. Kept as module-
# level constants (rather than inlined) so they stay readable and unit-testable.
_SCHEMA_REJECTION_INDICATORS = (
    "response_schema",
    "schema",
    "invalid_argument",
    "unsupported",
    "not supported",
    "validation error",
    "validationerror",
    "pydantic",
)

_TRANSPORT_REJECTION_INDICATORS = (
    "timeout",
    "timed out",
    "connection",
    "401",
    "403",
    "429",
    "resource_exhausted",
    "deadline_exceeded",
    "unauthorized",
    "forbidden",
    "quota",
)


def _is_schema_rejection(exc: Exception) -> bool:
    """Classify whether an exception looks like a server-side rejection of the
    response_schema argument, versus a transient transport/auth/quota failure
    that happened to occur on the same call.

    Transport/auth/quota shapes are checked first and win on overlap: a
    schema-shaped word appearing inside what is otherwise a 429/401/timeout
    message should not be treated as a schema rejection.
    """
    message = str(exc).lower()

    if any(indicator in message for indicator in _TRANSPORT_REJECTION_INDICATORS):
        return False

    return any(indicator in message for indicator in _SCHEMA_REJECTION_INDICATORS)


# Memoises a confirmed schema rejection so the cascade doesn't re-probe the
# Pydantic response_schema on every model once we know this google-genai
# version/environment rejects it -- caps a run at 4 cascade calls + at most 1
# extra probe, instead of potentially doubling every call in the cascade.
_USE_DICT_SCHEMA: bool = False

# --- Retired specimen titles -------------------------------------------------
# The 8 chapter titles that used to appear, fully populated, inside the prompt
# specimen. Gemini was copying these verbatim instead of generating new ones
# from the day's corpus. Captured here (before the prompt is rewritten) so
# validate_synthesis() can detect a recurrence of the same failure mode.
RETIRED_SPECIMEN_TITLES: frozenset[str] = frozenset({
    "1. \U0001F916 AI & Multi-Agent Deterministic Routing",
    "2. ☁️ Multi-Region Resiliency & Azure Landing Zones",
    "3. \U0001F4CA Microsoft Fabric Direct Lake vs Snowflake Iceberg",
    "4. \U0001F6E1️ Zero-Trust SPIFFE Workload Tokens & Attestation",
    "5. ⚙️ SRE Kernel eBPF Observability & Distributed Tracing",
    "6. ⚡ Distributed Systems Architecture & Outbox CDC",
    "7. \U0001F4B0 Spot GPU Orchestration & LLM Token FinOps",
    "8. ⚖️ NIST AI Risk Management & ISO 42001 Governance",
})

# --- Domain metadata for the deterministic fallback -------------------------
# Chapter numbering/emoji/label convention preserved from the original
# hardcoded chapters. The literal title strings below are used ONLY as a
# last-resort default when a domain returns zero ingested articles — every
# other case derives the title from that domain's actual top article.
DOMAIN_META: Dict[str, Dict[str, Any]] = {
    "ai": {
        "num": 1, "emoji": "\U0001F916", "label": "AI & Multi-Agent Systems",
        "time": "00:00", "seconds": 0,
        "default_full_title": "1. \U0001F916 AI & Multi-Agent Deterministic Routing",
        "default_source_name": "Anthropic Research",
        "default_source_url": "https://www.anthropic.com/research/building-effective-agents",
    },
    "cloud": {
        "num": 2, "emoji": "☁️", "label": "Cloud & Platform Resiliency",
        "time": "01:15", "seconds": 75,
        "default_full_title": "2. ☁️ Multi-Region Resiliency & Azure Landing Zones",
        "default_source_name": "Azure Architecture",
        "default_source_url": "https://learn.microsoft.com/azure/architecture/",
    },
    "data": {
        "num": 3, "emoji": "\U0001F4CA", "label": "Data & Modern Lakehouse",
        "time": "02:25", "seconds": 145,
        "default_full_title": "3. \U0001F4CA Microsoft Fabric Direct Lake vs Snowflake Iceberg",
        "default_source_name": "MS Fabric Team Blog",
        "default_source_url": "https://blog.fabric.microsoft.com/",
    },
    "sec": {
        "num": 4, "emoji": "\U0001F6E1️", "label": "Zero-Trust & Workload Security",
        "time": "03:40", "seconds": 220,
        "default_full_title": "4. \U0001F6E1️ Zero-Trust SPIFFE Workload Tokens & Attestation",
        "default_source_name": "SPIFFE Foundation",
        "default_source_url": "https://spiffe.io/docs/latest/spiffe-about/overview/",
    },
    "devops": {
        "num": 5, "emoji": "⚙️", "label": "SRE & Observability",
        "time": "04:55", "seconds": 295,
        "default_full_title": "5. ⚙️ SRE Kernel eBPF Observability & Distributed Tracing",
        "default_source_name": "eBPF.io",
        "default_source_url": "https://ebpf.io/what-is-ebpf/",
    },
    "arch": {
        "num": 6, "emoji": "⚡", "label": "Distributed Systems Architecture",
        "time": "06:10", "seconds": 370,
        "default_full_title": "6. ⚡ Distributed Systems Architecture & Outbox CDC",
        "default_source_name": "Debezium Community",
        "default_source_url": "https://debezium.io/",
    },
    "finops": {
        "num": 7, "emoji": "\U0001F4B0", "label": "Cloud Economics & FinOps",
        "time": "07:25", "seconds": 445,
        "default_full_title": "7. \U0001F4B0 Spot GPU Orchestration & LLM Token FinOps",
        "default_source_name": "FinOps Foundation",
        "default_source_url": "https://www.finops.org/",
    },
    "gov": {
        "num": 8, "emoji": "⚖️", "label": "AI Governance & Compliance",
        "time": "08:35", "seconds": 515,
        "default_full_title": "8. ⚖️ NIST AI Risk Management & ISO 42001 Governance",
        "default_source_name": "NIST AI & Cybersecurity",
        "default_source_url": "https://www.nist.gov/itl/ai-risk-management-framework",
    },
}

SYSTEM_SYNTHESIS_PROMPT = """You are the Principal AI Synthesis Engine for TechPulse OS.
Your task is to analyze today's ingested engineering articles across 8 technology domains and generate an executive multi-host technical podcast briefing, structured interview takeaways, 8 timecoded chapters (one per technology domain), and spaced-repetition flashcards.

The briefing hosts are:
- Host A: Enterprise Cloud Architect & AI Systems Specialist
- Host B: Principal Systems Architect & Engineering Governance Lead

Return a single JSON object with exactly these top-level fields: "title", "summary", "hosts", "script_segments", "chapters", "takeaways", "flashcards".

STRUCTURAL REQUIREMENTS (do not deviate from these):
- "chapters" MUST contain exactly 8 entries, one per domain, in this fixed order: ai, cloud, data, sec, devops, arch, finops, gov. Each entry has "title", "source_name", "source_url".
- Each chapter "title" MUST name the SPECIFIC technology, product release, vulnerability, or finding reported in TODAY'S supplied articles for that domain. Generic or evergreen topic names that could describe any day's briefing are forbidden — the title must be traceable to a specific article below. Number and lightly emoji-prefix each title, e.g. "1. <emoji> <Domain Label>: <specific finding from today's article>".
- Each chapter's "source_name" and "source_url" MUST be copied from one of the articles supplied for that domain in the corpus below — never invented, and never carried over from a prior day's briefing.
- "takeaways" MUST be a JSON object with exactly these 8 keys, all REQUIRED: "ai", "cloud", "data", "sec", "devops", "arch", "finops", "gov". Do not omit any domain, even if that domain's corpus is thin — derive something concrete from whatever was supplied. Each value has "badge", "release_date", "title", "bullets" (array of strings), "interview_framing", and "sources" (array of {"title", "url"}). Every field must be grounded in that domain's own supplied articles.
- "flashcards" MUST contain between 4 and 8 entries. Each flashcard's "cite" field MUST reference a specific article title or publication from today's corpus — never a generic or previous-day citation. Each entry has "domain", "question", "answer", "cite", "color_class".
- "script_segments" is an array of {"speaker", "text", "chapter_title"} alternating between Host A and Host B, covering all 8 chapters in order. Each "chapter_title" MUST exactly match one of the 8 "title" strings used in "chapters".

Do not reuse chapter titles, takeaway content, or flashcards from any previous briefing. Every field must be grounded in today's supplied article corpus below — never fabricate sources, dates, or findings.
"""


def extract_full_articles_corpus(domain_corpus: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    corpus_map = {}
    for domain, articles in domain_corpus.items():
        if articles:
            blob = f"DOMAIN: {domain.upper()}\n"
            for a in articles[:4]:
                title = a.get('title', 'Untitled')
                src = a.get('source_name', 'Source')
                summary = a.get('summary', '')
                url = a.get('url', '')
                blob += f"• [{src}] {title}\n  Summary: {summary}\n  Link: {url}\n\n"
            corpus_map[domain] = blob.strip()
    return corpus_map


def validate_synthesis(parsed: dict, previous_titles: Optional[List[str]] = None) -> tuple[bool, Optional[str]]:
    """Structural guard against a recurrence of the specimen-copying bug.

    Rejects a synthesized payload (causing the model cascade to advance to the
    next candidate, or fall through to the deterministic fallback if all
    candidates are rejected) when:
      - chapter count != 8
      - any chapter title matches a retired prompt-specimen title (leak)
      - the takeaways keys don't exactly match the 8 required domains
      - previous_titles is supplied and the new chapter title list is identical
      - script_segments[].chapter_title does not bidirectionally join with
        chapters[].title (every segment references a real chapter, and every
        chapter is referenced by at least one segment)
    """
    chapters = parsed.get("chapters", [])
    if not isinstance(chapters, list) or len(chapters) != 8:
        return False, f"expected exactly 8 chapters, got {len(chapters) if isinstance(chapters, list) else type(chapters).__name__}"

    titles = [c.get("title") for c in chapters]

    leaked = [t for t in titles if t in RETIRED_SPECIMEN_TITLES]
    if leaked:
        return False, f"retired prompt-specimen title(s) leaked into output: {leaked}"

    takeaways = parsed.get("takeaways", {})
    if not isinstance(takeaways, dict) or set(takeaways.keys()) != set(DOMAIN_ORDER):
        got = sorted(takeaways.keys()) if isinstance(takeaways, dict) else type(takeaways).__name__
        return False, f"takeaways keys {got} do not match the required 8 domains {DOMAIN_ORDER}"

    # Phase 2: this assumes every one of the 8 domains always has fresh
    # coverage, so a full repeat of the title list is always stale. Phase 2
    # introduces a "continuing coverage" flag for domains with no fresh
    # articles, which can legitimately repeat a title -- this blanket
    # all-8-equal check will need to become domain-aware at that point.
    if previous_titles is not None and titles == previous_titles:
        return False, "chapter titles are identical to the previous episode (stale/repeated synthesis)"

    # Bidirectional chapter_title <-> chapters join. tts_engine.py's
    # generate_episode_podcast_audio matches script_segments[].chapter_title
    # against chapters[].title via exact string equality (chapter_map); a miss
    # silently defaults source_name/source_url instead of raising, and main.py
    # then overwrites the correct 8-chapter list with whatever dynamic_chapters
    # tts_engine produced -- so a broken join here is a silent data-loss bug
    # downstream, not just a cosmetic mismatch.
    script_segments = parsed.get("script_segments", [])
    if not isinstance(script_segments, list):
        return False, f"expected script_segments to be a list, got {type(script_segments).__name__}"

    chapter_title_set = set(titles)
    referenced_titles: set = set()
    unknown_titles: List[str] = []
    for i, seg in enumerate(script_segments):
        if not isinstance(seg, dict):
            return False, f"script_segments[{i}] is not an object (got {type(seg).__name__})"
        seg_chapter_title = seg.get("chapter_title")
        if seg_chapter_title in chapter_title_set:
            referenced_titles.add(seg_chapter_title)
        else:
            label = seg_chapter_title if isinstance(seg_chapter_title, str) else "<missing chapter_title>"
            if label not in unknown_titles:
                unknown_titles.append(label)

    if unknown_titles:
        return False, f"script_segments reference chapter_title(s) not present in chapters: {unknown_titles}"

    unreferenced_titles = [t for t in titles if t not in referenced_titles]
    if unreferenced_titles:
        return False, f"chapter title(s) not referenced by any script_segments entry: {unreferenced_titles}"

    return True, None


# Systematic fabrication of source URLs should fail the synthesis (advancing the
# model cascade) rather than be silently patched; a couple of stray corrections
# are tolerated because they are repaired from the corpus.
MAX_URL_SUBSTITUTIONS = 2


def enforce_corpus_urls(parsed: dict, domain_corpus: Dict[str, List[Dict[str, Any]]]) -> tuple[dict, int]:
    """Rebuild source URLs in model output from the ingested corpus (F-03).

    The prompt asks the model to copy URLs from the supplied articles, but that
    is a request, not a control. This enforces it:
      - chapters[i].source_url (domain = DOMAIN_ORDER[i]) not found anywhere in
        the FULL corpus is replaced, with source_name, by that domain's top
        article; a domain with no articles gets a blank URL and name.
      - takeaways[domain].sources[] entries whose url is not in the corpus are
        dropped (logged, not counted).
    Returns (payload, substitution_count). Malformed chapters count as
    substitutions. Never raises: this runs on untrusted model output.
    """
    allowed: Dict[str, str] = {}
    top_by_domain: Dict[str, Dict[str, str]] = {}
    if isinstance(domain_corpus, dict):
        for domain, articles in domain_corpus.items():
            if not isinstance(articles, list):
                continue
            for a in articles:
                if not isinstance(a, dict):
                    continue
                url = a.get("url")
                if not isinstance(url, str) or not url:
                    continue
                name = a.get("source_name")
                name = name if isinstance(name, str) else ""
                allowed.setdefault(url, name)
                top_by_domain.setdefault(domain, {"url": url, "source_name": name})

    if not isinstance(parsed, dict):
        return parsed, 0

    substitutions = 0
    dropped = 0

    chapters = parsed.get("chapters")
    if isinstance(chapters, list):
        for i, chapter in enumerate(chapters):
            if not isinstance(chapter, dict):
                substitutions += 1  # uncorrectable; counted so it pushes toward rejection
                continue
            url = chapter.get("source_url")
            if isinstance(url, str) and url in allowed:
                continue
            domain = DOMAIN_ORDER[i] if i < len(DOMAIN_ORDER) else None
            target = top_by_domain.get(domain) if domain else None
            chapter["source_url"] = target["url"] if target else ""
            chapter["source_name"] = target["source_name"] if target else ""
            substitutions += 1

    takeaways = parsed.get("takeaways")
    if isinstance(takeaways, dict):
        for takeaway in takeaways.values():
            if not isinstance(takeaway, dict) or "sources" not in takeaway:
                continue
            sources = takeaway["sources"]
            if not isinstance(sources, list):
                dropped += 1
                takeaway["sources"] = []
                continue
            kept = [
                s for s in sources
                if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"] in allowed
            ]
            dropped += len(sources) - len(kept)
            takeaway["sources"] = kept

    if substitutions or dropped:
        logger.warning(
            f"enforce_corpus_urls: {substitutions} chapter URL substitution(s), "
            f"{dropped} takeaway source(s) dropped (not in ingested corpus)."
        )
    return parsed, substitutions


def _build_takeaway_for_domain(domain: str, articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    meta = DOMAIN_META[domain]

    if not articles:
        return {
            "badge": meta["label"].upper(),
            "release_date": local_now().strftime("%b %Y"),
            "title": meta["default_full_title"].split(". ", 1)[-1],
            "bullets": [f"No new {meta['label']} articles were ingested today; showing baseline domain guidance."],
            "interview_framing": f"Discuss current best practices and open questions in {meta['label']}.",
            "sources": [{"title": meta["default_source_name"], "url": meta["default_source_url"]}],
        }

    bullets: List[str] = []
    for a in articles[:3]:
        title = (a.get("title") or "").strip()
        summary = (a.get("summary") or "").strip()
        if title and summary:
            bullets.append(f"{title}: {summary}")
        elif title:
            bullets.append(title)
    if not bullets:
        bullets = [f"Reviewed {len(articles)} article(s) in {meta['label']} today."]

    top = articles[0]
    sources = [
        {"title": a.get("title"), "url": a.get("url")}
        for a in articles[:2]
        if a.get("title") and a.get("url")
    ]
    if not sources:
        sources = [{"title": meta["default_source_name"], "url": meta["default_source_url"]}]

    return {
        "badge": meta["label"].upper(),
        "release_date": local_now().strftime("%b %Y"),
        "title": (top.get("title") or meta["default_full_title"]),
        "bullets": bullets,
        "interview_framing": f"Be ready to explain how '{top.get('title', meta['label'])}' impacts enterprise {meta['label'].lower()} decisions.",
        "sources": sources,
    }


def generate_deterministic_fallback(domain_corpus: Dict[str, List[Dict[str, Any]]], episode_num: int) -> Dict[str, Any]:
    logger.info("Generating structured deterministic intelligence payload from ingested corpus across all 8 domains...")

    full_articles = extract_full_articles_corpus(domain_corpus)

    # Derive chapter titles from each domain's top article headline, decorrelating
    # this path from the LLM synthesis path so the two cannot silently collapse
    # to the same content. The old hardcoded strings survive only as the
    # last-resort default for a domain that returned zero articles.
    chapters: List[Dict[str, Any]] = []
    for domain in DOMAIN_ORDER:
        meta = DOMAIN_META[domain]
        articles = domain_corpus.get(domain) or []
        top_item = articles[0] if articles else {}
        headline = (top_item.get("title") or "").strip()

        if headline:
            title = f"{meta['num']}. {meta['emoji']} {meta['label']}: {headline}"
        else:
            # Same derived format as above, but with honest placeholder text --
            # deliberately NOT meta["default_full_title"], which is byte-identical
            # to a RETIRED_SPECIMEN_TITLES entry. Using that here would make
            # validate_synthesis reject the fallback's own output on any
            # all-empty-domain corpus (e.g. a network partition inside
            # ingest_all_domains()'s 30s timeout) -- indistinguishable, in the
            # data and the UI, from the originally reported specimen-copying bug.
            title = f"{meta['num']}. {meta['emoji']} {meta['label']}: No new developments reported"

        chapters.append({
            "time": meta["time"],
            "seconds": meta["seconds"],
            "title": title,
            "source_name": top_item.get("source_name", meta["default_source_name"]),
            "source_url": top_item.get("url", meta["default_source_url"]),
        })

    chapter_title_by_domain = {domain: chapters[i]["title"] for i, domain in enumerate(DOMAIN_ORDER)}

    takeaways = {
        domain: _build_takeaway_for_domain(domain, domain_corpus.get(domain) or [])
        for domain in DOMAIN_ORDER
    }

    script_segments = [
        {
            "speaker": "Host A",
            "text": "Good morning and welcome to TechPulse OS. Today, we lead with enterprise multi-agent system design. Anthropic's latest engineering report highlights that autonomous agent swarms without a deterministic router suffer from cascading hallucination loops in high-context tasks.",
            "chapter_title": chapter_title_by_domain["ai"]
        },
        {
            "speaker": "Host B",
            "text": "That's a crucial architectural shift. In enterprise distributed systems, we cannot rely on unbounded prompt loops. The Main-as-Router pattern enforces strict state serialization and pre-execution dry-run approval gates, directly meeting production reliability and safety standards.",
            "chapter_title": chapter_title_by_domain["ai"]
        },
        {
            "speaker": "Host A",
            "text": "Moving to Cloud & Platforms: achieving cross-region high availability with RTO under 60 seconds requires decoupling Anycast ingress from asynchronous storage replication across Azure Landing Zones and AWS.",
            "chapter_title": chapter_title_by_domain["cloud"]
        },
        {
            "speaker": "Host B",
            "text": "Correct. Using Azure Front Door paired with GitOps controllers like FluxCD ensures identical stateless pod topologies while avoiding multi-region synchronous database locking penalties.",
            "chapter_title": chapter_title_by_domain["cloud"]
        },
        {
            "speaker": "Host A",
            "text": "Turning to enterprise data architecture, Microsoft Fabric's Direct Lake mode is transforming analytical reporting. Instead of duplicating data into VertiPaq files via scheduled batch jobs, it queries Delta Parquet files directly from OneLake into VertiPaq memory on demand.",
            "chapter_title": chapter_title_by_domain["data"]
        },
        {
            "speaker": "Host B",
            "text": "And Snowflake is competing directly with managed Apache Iceberg tables. The advantage for architects is vendor neutrality: an external Iceberg catalog allows Spark, Databricks, and Snowflake engines to operate on the same S3 storage tier without vendor lock-in.",
            "chapter_title": chapter_title_by_domain["data"]
        },
        {
            "speaker": "Host A",
            "text": "In infrastructure security, static credentials in CI/CD pipelines are officially obsolete. SPIFFE and SPIRE automated workload identity federation issues ephemeral X.509 SVID certificates rotating every 60 minutes.",
            "chapter_title": chapter_title_by_domain["sec"]
        },
        {
            "speaker": "Host B",
            "text": "SPIRE inspects Linux kernel cgroups and container namespaces directly, satisfying NIST SP 800-207 Zero Trust credential lifecycle mandates.",
            "chapter_title": chapter_title_by_domain["sec"]
        },
        {
            "speaker": "Host A",
            "text": "In SRE and observability: eBPF socket tracing captures TCP latency and packet drops inside kernel space with under 1% CPU overhead, propagating W3C distributed trace context into OpenTelemetry.",
            "chapter_title": chapter_title_by_domain["devops"]
        },
        {
            "speaker": "Host B",
            "text": "And in distributed systems architecture, Debezium Change Data Capture reads database Write-Ahead Logs to guarantee Transactional Outbox atomicity without fragile Two-Phase Commit locks.",
            "chapter_title": chapter_title_by_domain["arch"]
        },
        {
            "speaker": "Host A",
            "text": "On Cloud Economics and FinOps: auto-pausing idle Fabric capacities and leveraging Graviton4 spot instance pools reduces batch inference spend by over 35 percent.",
            "chapter_title": chapter_title_by_domain["finops"]
        },
        {
            "speaker": "Host B",
            "text": "Finally, on AI governance, the NIST AI Risk Management Framework and ISO 42001 guidelines mandate immutable audit logging capturing prompt snapshots, model temperature, and output for every production GenAI decision.",
            "chapter_title": chapter_title_by_domain["gov"]
        }
    ]

    flashcards = [
        {
            "domain": "\U0001F916 AI & Agent Systems",
            "question": "In an enterprise interview, explain why the Deterministic Main-as-Router pattern is preferred over recursive monolithic swarms.",
            "answer": "Decouples stateful planning from tool execution. It enforces strict dry-run approval gates, caps step retry loops to 3, and produces immutable audit trails required by enterprise production standards (NIST AI RMF & ISO 42001).",
            "cite": "Source: Anthropic Research 2026",
            "color_class": "bg-indigo-500/20 text-indigo-300"
        },
        {
            "domain": "\U0001F4CA Data & Modern Lakehouse",
            "question": "How does Microsoft Fabric Direct Lake mode differ from Import and DirectQuery in terms of memory paging?",
            "answer": "Direct Lake loads Delta Parquet straight from OneLake into VertiPaq memory on demand without .PBIX duplication or scheduled refresh pipelines, falling back to DirectQuery only if capacity memory is exceeded.",
            "cite": "Source: Microsoft Fabric Team Blog",
            "color_class": "bg-emerald-500/20 text-emerald-300"
        },
        {
            "domain": "\U0001F6E1️ Zero Trust & Security",
            "question": "How does SPIFFE/SPIRE workload identity satisfy NIST SP 800-207 Zero Trust static secret removal rules?",
            "answer": "It replaces static API tokens and database passwords with automated, cryptographic X.509 SVID tokens that rotate automatically every 60 minutes with mTLS verification.",
            "cite": "Source: SPIFFE Spec & NIST SP 800-207",
            "color_class": "bg-rose-500/20 text-rose-300"
        },
        {
            "domain": "⚙️ SRE & Kernel Observability",
            "question": "Why does eBPF kernel tracing outperform legacy user-space APM daemon agents during high network throughput?",
            "answer": "eBPF attaches verified bytecode sandboxes directly to kernel kprobes and socket buffers, eliminating expensive user-to-kernel context switching and running with <1% CPU overhead.",
            "cite": "Source: eBPF.io Foundation 2026",
            "color_class": "bg-amber-500/20 text-amber-300"
        }
    ]

    return {
        "id": f"ep-{episode_num}",
        "episode_number": episode_num,
        "date": local_now().strftime("%b %d, %Y"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": "Executive Briefing: Full-Stack Enterprise Architecture, Agentic Governance & Zero Trust",
        "summary": "Today's briefing covers all 8 engineering pillars: Anthropic deterministic agent routing, cross-region Kubernetes failover, Fabric Direct Lake, SPIFFE workload attestation, kernel eBPF tracing, Debezium transactional outbox CDC, spot GPU FinOps, and NIST AI Risk Management.",
        "hosts": "Host A (Enterprise Cloud Architect) & Host B (Principal Systems Architect & Governance Lead)",
        "duration": "09:45",
        "total_seconds": 585,
        "full_articles": full_articles,
        "script_segments": script_segments,
        "chapters": chapters,
        "takeaways": takeaways,
        "flashcards": flashcards
    }


async def synthesize_briefing(
    domain_corpus: Dict[str, List[Dict[str, Any]]],
    episode_num: int = 142,
    previous_titles: Optional[List[str]] = None,
    *, diagnostics=None,
) -> Dict[str, Any]:
    stats = diagnostics if diagnostics is not None else {}
    stats.update(path=None, model=None, schema_variant=None, fallback_reason=None,
                 attempts=[], url_substitutions=0, prompt_version="corpus-instructions-v1",
                 schema_version=1, validator_version=1)
    api_key = os.getenv("GEMINI_API_KEY")
    full_articles = extract_full_articles_corpus(domain_corpus)

    if not api_key:
        stats.update(path="deterministic_fallback", fallback_reason="no_api_key")
        logger.info("GEMINI_API_KEY not configured. Using deterministic synthesis pipeline.")
        return generate_deterministic_fallback(domain_corpus, episode_num)

    env_model = os.getenv("GEMINI_MODEL", "").strip()
    candidate_models = [m for m in [env_model, "gemini-3.6-flash", "gemini-2.5-flash", "gemini-flash"] if m]
    seen = set()
    models_to_try = []
    for m in candidate_models:
        if m not in seen:
            seen.add(m)
            models_to_try.append(m)

    # Prepare article corpus summary for LLM
    corpus_text = ""
    for domain, articles in domain_corpus.items():
        corpus_text += f"\n\n### DOMAIN: {domain.upper()}\n"
        for a in articles[:3]:
            corpus_text += f"- [{a.get('source_name')}]: {a.get('title')} ({a.get('url')})\n  Summary: {a.get('summary')}\n"

    prompt = f"{SYSTEM_SYNTHESIS_PROMPT}\n\n## INGESTED ARTICLE CORPUS:\n{corpus_text}"

    global _USE_DICT_SCHEMA

    for m in models_to_try:
        attempt = None
        try:
            from google import genai
            client = genai.Client(api_key=api_key)

            def generate(variant):
                nonlocal attempt
                attempt = {"model": model_identifier(m), "schema_variant": variant,
                           "outcome": "running", "error_category": None, "url_substitutions": 0}
                stats["attempts"].append(attempt)
                started = time.monotonic()
                try:
                    response = client.models.generate_content(
                        model=m, contents=prompt,
                        config={"response_mime_type": "application/json", "response_schema":
                                BRIEFING_JSON_SCHEMA if variant == "json_schema_dict" else Briefing})
                    attempt["outcome"] = "response_returned"
                    return response
                except Exception as exc:
                    category = error_category(exc)
                    attempt["error_category"] = category
                    attempt["outcome"] = "schema_rejected" if _is_schema_rejection(exc) else {
                        "timeout": "transport_error", "transport": "transport_error",
                        "auth": "auth_error", "quota": "quota_error"}.get(category, "provider_error")
                    raise
                finally:
                    attempt["duration_ms"] = round((time.monotonic() - started) * 1000, 3)

            if _USE_DICT_SCHEMA:
                # A prior model in this run (or a prior run) already confirmed a
                # genuine schema rejection -- go straight to the dict schema
                # instead of re-probing the Pydantic variant on every model.
                response = generate("json_schema_dict")
            else:
                try:
                    response = generate("pydantic")
                except Exception as schema_err:
                    if not _is_schema_rejection(schema_err):
                        # Transient network/auth/quota-shaped failure, not a schema
                        # rejection -- re-raise so the outer per-model handler below
                        # logs "Synthesis with model {m} failed" and advances the
                        # cascade normally, instead of masking it as a schema issue
                        # and burning a second API call for an unrelated reason.
                        raise

                    # Nested Pydantic models (Takeaways -> Takeaway -> Source) have a
                    # known acceptance issue with some google-genai versions
                    # (googleapis/python-genai issue #60). Memoise the finding so
                    # every later model in this cascade (and future runs) goes
                    # straight to the dict schema instead of re-probing.
                    logger.warning(
                        f"Nested Pydantic response_schema rejected for model {model_identifier(m)}; "
                        f"falling back to explicit JSON-schema dict for the remainder of this run."
                    )
                    _USE_DICT_SCHEMA = True
                    response = generate("json_schema_dict")

            try:
                parsed = json.loads(response.text)
            except (ValueError, TypeError):
                attempt["outcome"] = "invalid_json"
                raise

            ok, reject_reason = validate_synthesis(parsed, previous_titles)
            if not ok:
                attempt["outcome"] = "validation_rejected"
                logger.warning("Synthesis rejected by structural validator; trying fallback models")
                continue

            parsed, url_substitutions = enforce_corpus_urls(parsed, domain_corpus)
            attempt["url_substitutions"] = url_substitutions
            if url_substitutions > MAX_URL_SUBSTITUTIONS:
                attempt["outcome"] = "url_provenance_rejected"
                logger.warning(
                    f"Synthesis with model {model_identifier(m)} rejected: {url_substitutions} chapter URL(s) not in the ingested "
                    f"corpus (limit {MAX_URL_SUBSTITUTIONS}). Trying fallback models..."
                )
                continue

            parsed["id"] = f"ep-{episode_num}"
            parsed["episode_number"] = episode_num
            parsed["date"] = local_now().strftime("%b %d, %Y")
            parsed["created_at"] = datetime.now(timezone.utc).isoformat()
            parsed["duration"] = parsed.get("duration", "05:20")
            parsed["total_seconds"] = parsed.get("total_seconds", 320)
            parsed["full_articles"] = full_articles
            attempt["outcome"] = "accepted"
            stats.update(path="llm", model=model_identifier(m), schema_variant=attempt["schema_variant"],
                         url_substitutions=url_substitutions)
            logger.info("Synthesis succeeded with model: %s", model_identifier(m))
            return parsed
        except Exception as e:
            # A client construction/import failure precedes a provider call and
            # must not be mislabeled as an attempted generation request.
            if attempt is None:
                stats.setdefault("client_errors", []).append({"model": model_identifier(m), "error_category": error_category(e)})
            elif attempt["outcome"] == "response_returned":
                attempt.update(outcome="postprocessing_failure", error_category=error_category(e))
            logger.warning("Synthesis model failed (category=%s); trying fallback models", error_category(e))

    logger.error("All candidate models failed for synthesis. Falling back to deterministic pipeline.")
    stats.update(path="deterministic_fallback", fallback_reason="attempts_exhausted")
    return generate_deterministic_fallback(domain_corpus, episode_num)

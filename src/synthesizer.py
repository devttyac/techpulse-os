import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Optional

from pydantic import BaseModel, create_model
from src.provenance import error_category, model_identifier
from src.content_availability import build_content_availability, active_domains

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.synthesizer")

# Stable domain IDs for presentation and legacy complete-eight compatibility.
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
# dynamic takeaways model is rejected by the provider schema parser.

class Source(BaseModel):
    title: str
    url: str


class Chapter(BaseModel):
    domain: str
    title: str
    source_name: str
    source_url: str


class ScriptSegment(BaseModel):
    domain: str
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
    takeaways: Dict[str, Takeaway]
    flashcards: List[Flashcard]


# Explicit JSON schema template; synthesis narrows takeaway keys to active domains.
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
                    "domain": {"type": "string"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                    "chapter_title": {"type": "string"},
                },
                "required": ["domain", "speaker", "text", "chapter_title"],
            },
        },
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "title": {"type": "string"},
                    "source_name": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["domain", "title", "source_name", "source_url"],
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

# --- Display labels for the eight stable domain IDs -------------------------
DOMAIN_META: Dict[str, Dict[str, Any]] = {
    "ai": {
        "num": 1, "emoji": "\U0001F916", "label": "AI & Multi-Agent Systems",
    },
    "cloud": {
        "num": 2, "emoji": "☁️", "label": "Cloud & Platform Resiliency",
    },
    "data": {
        "num": 3, "emoji": "\U0001F4CA", "label": "Data & Modern Lakehouse",
    },
    "sec": {
        "num": 4, "emoji": "\U0001F6E1️", "label": "Zero-Trust & Workload Security",
    },
    "devops": {
        "num": 5, "emoji": "⚙️", "label": "SRE & Observability",
    },
    "arch": {
        "num": 6, "emoji": "⚡", "label": "Distributed Systems Architecture",
    },
    "finops": {
        "num": 7, "emoji": "\U0001F4B0", "label": "Cloud Economics & FinOps",
    },
    "gov": {
        "num": 8, "emoji": "⚖️", "label": "AI Governance & Compliance",
    },
}

SYSTEM_SYNTHESIS_PROMPT = """You synthesize TechPulse OS briefings from received RSS headlines and summaries.
Return title, summary, hosts, script_segments, chapters, takeaways and flashcards.
Generate content ONLY for AVAILABLE DOMAIN IDS supplied below. Gaps are valid.
Chapters appear in the supplied domain order with domain, title, source_name and source_url.
Each title names a specific received finding; each source belongs to that same domain.
Script segments contain domain, speaker, text and chapter_title; the domain and title
must join the same chapter. Alternate Host A and Host B where useful.
Takeaways contain only available domain keys with badge, release_date, title, bullets,
interview_framing and sources. Never invent dates, findings, guidance or citations.
Flashcards may contain zero entries. Only produce cards supported by received summaries,
with domain (the stable ID), question, answer, cite and color_class. No minimum card count.
Never fill unavailable domains with evergreen material or previous episodes.
If a summary cannot support a claim, omit the claim. Explain coverage gaps in the synopsis.
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


def validate_synthesis(parsed: dict, previous_titles: Optional[List[str]] = None,
                       availability=None) -> tuple[bool, Optional[str]]:
    """Validate active content joins; old complete-eight payloads remain readable."""
    if not isinstance(parsed, dict):
        return False, "synthesis is not an object"
    coverage = availability or parsed.get("content_availability")
    active = active_domains(coverage) if coverage is not None else list(DOMAIN_ORDER)
    chapters = parsed.get("chapters", [])
    if not isinstance(chapters, list) or len(chapters) != len(active) or not active:
        return False, "chapter count does not match available domains"
    if any(not isinstance(c, dict) for c in chapters):
        return False, "chapter is not an object"
    legacy = all("domain" not in c for c in chapters) and active == DOMAIN_ORDER
    domains = list(DOMAIN_ORDER) if legacy else [c.get("domain") for c in chapters]
    if domains != active:
        return False, "chapter domains do not match available domain order"
    titles = [c.get("title") for c in chapters]
    if any(not isinstance(t, str) or not t.strip() for t in titles) or len(set(titles)) != len(titles):
        return False, "chapter titles must be distinct nonempty strings"
    if any(t in RETIRED_SPECIMEN_TITLES for t in titles):
        return False, "retired prompt-specimen title leaked into output"
    if previous_titles is not None and titles == previous_titles:
        return False, "chapter titles are identical to the previous episode"
    takeaways = parsed.get("takeaways", {})
    if not isinstance(takeaways, dict) or not set(active).issubset(takeaways) or not set(takeaways).issubset(DOMAIN_ORDER):
        return False, "takeaway domains do not match available domains"
    if coverage is None and set(takeaways) != set(DOMAIN_ORDER):
        return False, "legacy takeaways must have all eight domains"
    for domain, data in takeaways.items():
        if not isinstance(data, dict):
            return False, "takeaway is not an object"
        if domain not in active and (data.get("bullets") or data.get("sources") or data.get("interview_framing") or data.get("title")):
            return False, "inactive takeaway contains unsupported content"
        if domain in active and (not isinstance(data.get("bullets"), list) or not isinstance(data.get("sources"), list)):
            return False, "active takeaway fields must be lists"
        if domain in active:
            if any(not isinstance(data.get(k), str) for k in ("badge", "release_date", "title", "interview_framing")):
                return False, "active takeaway fields must be text"
            if any(not isinstance(b, str) for b in data["bullets"]):
                return False, "takeaway bullets must be text"
            if any(not isinstance(source, dict) or any(not isinstance(source.get(k), str) for k in ("title", "url")) for source in data["sources"]):
                return False, "takeaway sources must have text titles and URLs"
    segments = parsed.get("script_segments", [])
    if not isinstance(segments, list):
        return False, "script_segments must be a list"
    title_domains = dict(zip(titles, domains))
    referenced = set()
    for seg in segments:
        if not isinstance(seg, dict) or not isinstance(seg.get("chapter_title"), str) or seg["chapter_title"] not in title_domains:
            return False, "script segment references an unknown chapter"
        if not legacy and seg.get("domain") != title_domains[seg["chapter_title"]]:
            return False, "script segment domain does not match its chapter"
        if not isinstance(seg.get("text"), str) or not seg["text"].strip():
            return False, "script segment has no supported narration"
        referenced.add(seg["chapter_title"])
    if referenced != set(titles):
        return False, "chapter has no script segment"
    cards = parsed.get("flashcards", [])
    if not isinstance(cards, list) or any(not isinstance(c, dict) for c in cards):
        return False, "flashcards must be objects"
    if not legacy and any(c.get("domain") not in active for c in cards):
        return False, "flashcard belongs to an unavailable domain"
    return True, None


# Systematic fabrication of source URLs should fail the synthesis (advancing the
# model cascade) rather than be silently patched; a couple of stray corrections
# are tolerated because they are repaired from the corpus.
MAX_URL_SUBSTITUTIONS = 2


def enforce_corpus_urls(parsed: dict, domain_corpus: Dict[str, List[Dict[str, Any]]]) -> tuple[dict, int]:
    """Rebuild source URLs in model output from the ingested corpus (F-03).

    The prompt asks the model to copy URLs from the supplied articles, but that
    is a request, not a control. This enforces it:
      - explicit chapter domain IDs constrain citations to that domain's corpus;
        old complete-eight chapters retain the positional mapping. Unsupported
        citations are replaced with the same domain's top received article.
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
            domain_id = chapter.get("domain")
            domain_id = domain_id if isinstance(domain_id, str) else None
            domain_urls = {a.get("url") for a in (domain_corpus.get(domain_id) or []) if isinstance(a, dict)} if isinstance(domain_corpus, dict) and domain_id else None
            if isinstance(url, str) and url in allowed and (domain_urls is None or url in domain_urls):
                continue
            domain = domain_id
            if domain is None and len(chapters) == 8 and not parsed.get("content_availability"):
                domain = DOMAIN_ORDER[i] if i < len(DOMAIN_ORDER) else None
            target = top_by_domain.get(domain) if domain else None
            chapter["source_url"] = target["url"] if target else ""
            chapter["source_name"] = target["source_name"] if target else ""
            substitutions += 1

    takeaways = parsed.get("takeaways")
    if isinstance(takeaways, dict):
        for domain, takeaway in takeaways.items():
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
                and (not parsed.get("content_availability") or s["url"] in {a.get("url") for a in (domain_corpus.get(domain) or []) if isinstance(a, dict)})
            ]
            dropped += len(sources) - len(kept)
            takeaway["sources"] = kept

    if substitutions or dropped:
        logger.warning(
            f"enforce_corpus_urls: {substitutions} chapter URL substitution(s), "
            f"{dropped} takeaway source(s) dropped (not in ingested corpus)."
        )
    return parsed, substitutions


def _inactive_takeaway(domain, state):
    return {"domain": domain, "status": state["status"], "reason": state["reason"],
            "badge": DOMAIN_META[domain]["label"].upper(), "release_date": "", "title": "",
            "bullets": [], "interview_framing": "", "sources": []}


def _build_takeaway_for_domain(domain: str, articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    meta = DOMAIN_META[domain]
    if not articles:
        return _inactive_takeaway(domain, build_content_availability({})["domains"][domain])
    top = articles[0]
    bullets = [f"{a.get('title', '')}: {a.get('summary', '')}" if a.get("summary")
               else a.get("title", "") for a in articles[:3] if a.get("title")]
    return {"domain": domain, "status": "available", "reason": "Articles available from received RSS candidates.",
            "badge": meta["label"].upper(), "release_date": "", "title": top.get("title", ""),
            "bullets": bullets, "interview_framing": "",
            "sources": [{"title": a["title"], "url": a["url"]} for a in articles[:2]
                        if a.get("title") and a.get("url")]}


def generate_deterministic_fallback(domain_corpus, episode_num, *, availability=None):
    """Summarize received RSS evidence; never substitute fixed expert guidance."""
    coverage = availability or build_content_availability(domain_corpus)
    active = active_domains(coverage)
    if not active:
        return None
    chapters, segments, cards = [], [], []
    for index, domain in enumerate(active):
        articles = domain_corpus[domain]
        top, meta = articles[0], DOMAIN_META[domain]
        title = f"{index + 1}. {meta['emoji']} {meta['label']}: {top.get('title', '')}"
        chapters.append({"domain": domain, "time": "00:00", "seconds": 0, "title": title,
                         "source_name": top.get("source_name", ""), "source_url": top.get("url", "")})
        narration = " ".join(f"{a.get('title', '')}. {a.get('summary', '')}".strip() for a in articles[:3])
        segments.append({"domain": domain, "speaker": "Host A" if index % 2 == 0 else "Host B",
                         "text": narration, "chapter_title": title})
        for article in articles[:1]:
            if article.get("summary") and article.get("title"):
                cards.append({"domain": domain, "question": f"What does the received summary report about {article['title']}?",
                              "answer": article["summary"], "cite": article.get("title", ""),
                              "color_class": "bg-indigo-500/20 text-indigo-300"})
    takeaways = {d: (_build_takeaway_for_domain(d, domain_corpus[d]) if d in active
                     else _inactive_takeaway(d, coverage["domains"][d])) for d in DOMAIN_ORDER}
    return {"id": f"ep-{episode_num}", "episode_number": episode_num,
            "date": local_now().strftime("%b %d, %Y"), "created_at": datetime.now(timezone.utc).isoformat(),
            "title": f"RSS Briefing: {len(active)} available domains", "summary":
            f"Received RSS summaries cover {', '.join(active)}. {8 - len(active)} domains have no available articles.",
            "hosts": "Host A & Host B", "duration": "00:00", "total_seconds": 0,
            "content_basis": "rss_summaries", "fallback_content": "source_derived",
            "content_availability": coverage, "full_articles": extract_full_articles_corpus(domain_corpus),
            "script_segments": segments, "chapters": chapters, "takeaways": takeaways, "flashcards": cards}


async def synthesize_briefing(
    domain_corpus: Dict[str, List[Dict[str, Any]]],
    episode_num: int = 142,
    previous_titles: Optional[List[str]] = None,
    *, diagnostics=None,
) -> Optional[Dict[str, Any]]:
    stats = diagnostics if diagnostics is not None else {}
    stats.update(path=None, model=None, schema_variant=None, fallback_reason=None,
                 attempts=[], url_substitutions=0, prompt_version="corpus-instructions-v2",
                 schema_version=1, validator_version=1, content_basis="rss_summaries")
    api_key = os.getenv("GEMINI_API_KEY")
    full_articles = extract_full_articles_corpus(domain_corpus)
    availability = build_content_availability(domain_corpus)
    active = active_domains(availability)
    if not active:
        stats.update(path="no_content", fallback_reason="no_received_candidates")
        return None

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
    for domain in active:
        articles = domain_corpus[domain]
        corpus_text += f"\n\n### DOMAIN: {domain.upper()}\n"
        for a in articles[:3]:
            corpus_text += f"- [{a.get('source_name')}]: {a.get('title')} ({a.get('url')})\n  Summary: {a.get('summary')}\n"

    prompt = f"{SYSTEM_SYNTHESIS_PROMPT}\n\nAVAILABLE DOMAIN IDS: {', '.join(active)}\n\n## INGESTED ARTICLE CORPUS:\n{corpus_text}"

    global _USE_DICT_SCHEMA
    active_takeaways = create_model("ActiveTakeaways", **{d: (Takeaway, ...) for d in active})
    active_briefing = create_model("ActiveBriefing", __base__=Briefing, takeaways=(active_takeaways, ...))
    active_json_schema = json.loads(json.dumps(BRIEFING_JSON_SCHEMA))
    active_json_schema["properties"]["takeaways"] = {
        "type": "object", "properties": {d: _domain_takeaway_schema() for d in active}, "required": active}

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
                                active_json_schema if variant == "json_schema_dict" else active_briefing})
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

            ok, reject_reason = validate_synthesis(parsed, previous_titles, availability)
            if not ok:
                attempt["outcome"] = "validation_rejected"
                logger.warning("Synthesis rejected by structural validator; trying fallback models")
                continue

            for domain, chapter in zip(active, parsed["chapters"]):
                chapter["domain"] = domain
            parsed["content_availability"] = availability
            parsed, url_substitutions = enforce_corpus_urls(parsed, domain_corpus)
            attempt["url_substitutions"] = url_substitutions
            if url_substitutions > MAX_URL_SUBSTITUTIONS:
                attempt["outcome"] = "url_provenance_rejected"
                logger.warning(
                    f"Synthesis with model {model_identifier(m)} rejected: {url_substitutions} chapter URL(s) not in the ingested "
                    f"corpus (limit {MAX_URL_SUBSTITUTIONS}). Trying fallback models..."
                )
                continue

            # Normalize accepted complete-eight legacy provider fixtures to explicit IDs.
            for domain, chapter in zip(active, parsed["chapters"]):
                chapter["domain"] = domain
            domains_by_title = {c["title"]: c["domain"] for c in parsed["chapters"]}
            for segment in parsed["script_segments"]:
                segment["domain"] = domains_by_title[segment["chapter_title"]]
            for domain in DOMAIN_ORDER:
                if domain not in active:
                    parsed["takeaways"][domain] = _inactive_takeaway(domain, availability["domains"][domain])
                else:
                    parsed["takeaways"][domain].update(domain=domain, status="available", reason=availability["domains"][domain]["reason"])
            parsed["content_availability"] = availability
            parsed["content_basis"] = "rss_summaries"
            parsed.pop("fallback_content", None)
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

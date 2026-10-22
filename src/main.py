import asyncio
import html
import json
import logging
import os
import re
import shutil
import hashlib
import hmac
import math
import copy
import xml.etree.ElementTree as ET
import email.utils
from urllib.parse import urlparse, quote
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.ingestion import ingest_all_domains, DOMAIN_FEEDS
from src.content_availability import (DOMAIN_ORDER, SOURCE_FAILURES, build_content_availability,
    empty_source_health, build_source_health, load_source_health, save_source_health, sanitize_source_health)
from src.synthesizer import synthesize_briefing, local_now
from src.tts_engine import (generate_episode_podcast_audio, generate_all_domain_audios,
                            generate_story_audio_bundle, VOICE_MAP, format_seconds_to_time)
from src.story_manifest import (freeze_story_manifest, manifest_from_dict, manifest_to_dict,
    episode_fingerprint, audio_recipe_fingerprint, validate_story_episode)
from src.grounded_chat import process_grounded_chat
from src.provenance import (RunRecord, append_record, attach_episode_record,
                            selection_record, error_category, action_record)
import time
from functools import wraps
from mutagen.mp3 import MP3
from mutagen import MutagenError
from src.legacy_reader import project_retained_episode
from src.settings import (SettingsConflict, SettingsIntegrityError, SettingsService,
                          SettingsValidationError)
from src.run_coordinator import RunCoordinator
from src.state_store import StateStore, StoreIntegrityError

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.main")

APP_VERSION = "3.5.0"

STORAGE_DIR = os.getenv("STORAGE_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
EPISODES_DIR = os.path.join(STORAGE_DIR, "episodes")
AUDIO_DIR = os.path.join(STORAGE_DIR, "audio")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
CONFIG_FILE = os.path.join(STORAGE_DIR, "config.json")

os.makedirs(EPISODES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "max_episodes_retained": 14,
    "chapters_per_episode": 8,
    "gemini_model": "gemini-3.6-flash",
    "cron_schedule": "0 7 * * *"
}

def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in cfg:
                        cfg[k] = v
                return cfg
        except Exception as e:
            logger.error(f"Error loading config: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"Saved configuration to {CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Error saving config: {e}")

scheduler = AsyncIOScheduler()
run_coordinator = RunCoordinator(STORAGE_DIR)
scheduler_status = {"status": "not_started", "bound_revision": None}


def _scheduled_tick():
    """APScheduler wakes the owned ledger; it never invokes generation."""
    result = run_coordinator.tick(live_tick=True)
    scheduler_status["status"] = result["status"]
    if result["status"] in ("ready", "busy", "started"):
        try:
            scheduler_status["bound_revision"] = SettingsService(STORAGE_DIR).read()["revision"]
        except SettingsIntegrityError:
            scheduler_status["bound_revision"] = None
    else:
        scheduler_status["bound_revision"] = None
    if result["status"] == "blocked":
        logger.warning("Scheduler blocked (%s)", result.get("reason", "unknown"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("API_SECRET_KEY", "").strip():
        logger.info("API authentication is enabled: /api/* requires a valid X-API-Key header")
    else:
        logger.warning("API is in READ-ONLY mode: API_SECRET_KEY is unset, so all non-GET /api/* requests are rejected")
    global source_health, run_coordinator
    source_health = load_source_health(STORAGE_DIR)
    run_coordinator = RunCoordinator(STORAGE_DIR)
    # The first scan classifies provable downtime misses. Subsequent wakeups
    # admit an on-time slot only after the bound durable ledger records it.
    initial = run_coordinator.tick(live_tick=False)
    scheduler_status["status"] = initial["status"]
    scheduler_status["bound_revision"] = None
    scheduler.add_job(_scheduled_tick, "interval", seconds=1, max_instances=1,
                      coalesce=False, misfire_grace_time=1)
    scheduler.start()

    # Historical records are never migrated, deduplicated or retired at startup.
    # TP26 owns reference-aware retirement after its separate acceptance gate.
    try:
        init_seed_data()
    except Exception as e:
        logger.error(f"Error in fresh-install seed initialization: {e}")

    yield

    if scheduler.running:
        scheduler.shutdown()

app = FastAPI(
    title="TechPulse OS",
    description="Multi-Domain Technical Intelligence & Socratic Sparring Platform",
    version=APP_VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

async def require_auth(request: Request, x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    """Gate for every /api/* route.

    API_SECRET_KEY is read per request. When it is set, the X-API-Key header must
    match (constant-time compare). When it is unset or blank the server runs in
    read-only mode: safe methods pass, everything else is rejected. This fails
    closed without refusing to start, so a rebuild that forgets the variable
    keeps the site up but cannot be used to change or delete data.
    """
    secret = os.getenv("API_SECRET_KEY", "").strip()
    if secret:
        supplied = (x_api_key or "").encode("utf-8")
        if not supplied or not hmac.compare_digest(supplied, secret.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing X-API-Key header")
        return True
    if request.method.upper() in SAFE_METHODS:
        return True
    raise HTTPException(
        status_code=401,
        detail="Unauthorized: server is in read-only mode because API_SECRET_KEY is unset; mutating requests are disabled",
    )

# Seed episodes for instant out-of-the-box readiness
SEED_EPISODES = {
    "ep-142": {
        "id": "ep-142",
        "episode_number": 142,
        "date": "Aug 26, 2026",
        "title": "Executive Synthesis: Multi-Agent Swarm Governance, Microsoft Fabric OneLake & Zero-Trust Workload Identity",
        "summary": "Today we analyze Anthropic's deterministic agent routing patterns, compare Microsoft Fabric Direct Lake against Snowflake Iceberg catalogs, review SPIFFE workload identity in distributed microservices, and examine NIST AI Risk Management Framework standards for production LLMs.",
        "hosts": "Host A (Enterprise Cloud Architect) & Host B (Principal Systems Architect & Governance Lead)",
        "duration": "05:20",
        "total_seconds": 320,
        "audio_url": "/audio/ep-142.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Intro & Multi-Agent Deterministic Routing", "source_name": "Anthropic", "source_url": "https://www.anthropic.com/research/building-effective-agents"},
            {"time": "01:15", "seconds": 75, "title": "2. Microsoft Fabric Direct Lake vs Snowflake Iceberg", "source_name": "MS Fabric", "source_url": "https://blog.fabric.microsoft.com/"},
            {"time": "02:35", "seconds": 155, "title": "3. Zero-Trust SPIFFE Workload Tokens & Cryptographic Attestation", "source_name": "SPIFFE.io", "source_url": "https://spiffe.io/docs/latest/spiffe-about/overview/"},
            {"time": "04:00", "seconds": 240, "title": "4. NIST AI Risk Management & Enterprise Model Safety", "source_name": "NIST AI & Cybersecurity", "source_url": "https://www.nist.gov/itl/ai-risk-management-framework"}
        ],
        "takeaways": {
            "ai": {
                "badge": "AGENTIC DESIGN PATTERN",
                "release_date": "Aug 2026",
                "title": "Deterministic Main-as-Router Pattern vs Monolithic Multi-Agent Swarms",
                "bullets": [
                    "Decouples stateful planning from tool execution to prevent cascading prompt hallucinations.",
                    "Implements structural dry-run approval gates before executing file system or API mutations.",
                    "Evaluates agent trajectories using automated LLM-as-a-Judge benchmark suites."
                ],
                "interview_framing": "Explain how the 'Main-as-Router' pattern guarantees deterministic audit logs and cost bounds in enterprise production.",
                "sources": [
                    {"title": "Anthropic: Building Effective Agents", "url": "https://www.anthropic.com/research/building-effective-agents"},
                    {"title": "OpenAI: Governing Agentic AI", "url": "https://openai.com/research/practices-for-governing-agentic-ai"}
                ]
            }
        }
    },
    "ep-141": {
        "id": "ep-141",
        "episode_number": 141,
        "date": "Aug 25, 2026",
        "title": "Deep Dive: Agentic RAG Architecture, Hybrid Search & Local Vector Stores",
        "summary": "Explores agentic chunking, dynamic re-ranking with Cohere, hybrid dense-sparse vector search, and sub-10ms query execution.",
        "hosts": "Host A (AI Engineer) & Host B (Systems Architect)",
        "duration": "06:15",
        "total_seconds": 375,
        "audio_url": "/audio/ep-141.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Multi-Vector Representation & Document Chunking Strategies", "source_name": "LangChain", "source_url": "https://blog.langchain.dev/"},
            {"time": "02:10", "seconds": 130, "title": "2. BM25 + Dense Embeddings Hybrid Search Routing", "source_name": "Qdrant", "source_url": "https://qdrant.tech/articles/"},
            {"time": "04:30", "seconds": 270, "title": "3. Sub-10ms Vector Quantization & Hardware Acceleration", "source_name": "Pinecone", "source_url": "https://www.pinecone.io/learn/"}
        ]
    },
    "ep-140": {
        "id": "ep-140",
        "episode_number": 140,
        "date": "Aug 24, 2026",
        "title": "Modern Data Stack: Microsoft Fabric Real-Time Analytics vs Snowflake Iceberg Catalogs",
        "summary": "Deep dive into Delta Parquet on OneLake, open Iceberg metadata management, and enterprise capacity cost modeling.",
        "hosts": "Host A (Data Architect) & Host B (Analytics Lead)",
        "duration": "05:10",
        "total_seconds": 310,
        "audio_url": "/audio/ep-140.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Microsoft Fabric OneLake Delta Parquet Architecture", "source_name": "MS Learn", "source_url": "https://learn.microsoft.com/en-us/fabric/"},
            {"time": "01:45", "seconds": 105, "title": "2. Apache Iceberg Metadata Scaling on S3/Blob Storage", "source_name": "Snowflake", "source_url": "https://www.snowflake.com/en/blog/"},
            {"time": "03:30", "seconds": 210, "title": "3. Cost Comparison: Fabric F-SKU vs Snowflake Warehouses", "source_name": "FinOps", "source_url": "https://www.finops.org/framework/"}
        ]
    },
    "ep-139": {
        "id": "ep-139",
        "episode_number": 139,
        "date": "Aug 23, 2026",
        "title": "Platform Engineering: Kernel eBPF Telemetry & Workload Identity Federation",
        "summary": "Covers kernel-level network tracing with <1% overhead, OpenTelemetry W3C trace context, and zero-trust SPIFFE credential removal.",
        "hosts": "Host A (Principal SRE) & Host B (CISO)",
        "duration": "04:30",
        "total_seconds": 270,
        "audio_url": "/audio/ep-139.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Kernel-Level TCP Telemetry with Zero Overhead", "source_name": "eBPF.io", "source_url": "https://ebpf.io/what-is-ebpf/"},
            {"time": "01:20", "seconds": 80, "title": "2. OpenTelemetry W3C Distributed Context Propagation", "source_name": "OTel Docs", "source_url": "https://opentelemetry.io/docs/"},
            {"time": "02:50", "seconds": 170, "title": "3. Zero-Trust Identity & Static Secret Removal", "source_name": "NIST SP 800-207", "source_url": "https://csrc.nist.gov/publications/detail/sp/800-207/final"}
        ]
    }
}

def init_seed_data():
    """Install bundled examples only in a storage root without retained episodes."""
    if any(re.fullmatch(r"ep-[0-9]+\.json", name) for name in os.listdir(EPISODES_DIR)):
        return
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed_data")
    if os.path.exists(seed_dir):
        for root, dirs, files in os.walk(seed_dir):
            rel = os.path.relpath(root, seed_dir)
            target_dir = os.path.join(STORAGE_DIR, rel)
            os.makedirs(target_dir, exist_ok=True)
            for file in files:
                src_f = os.path.join(root, file)
                dst_f = os.path.join(target_dir, file)
                if not os.path.exists(dst_f):
                    try:
                        shutil.copyfile(src_f, dst_f)
                        logger.info("Installed fresh seed asset: %s", dst_f)
                    except Exception as e:
                        logger.error(f"Failed to copy seed file {src_f}: {e}")

def sanitize_existing_episodes():
    """Scans EPISODES_DIR and in-place sanitizes any stale MAS/TRM references in saved JSON files (gated by one-time marker)."""
    if not os.path.exists(EPISODES_DIR):
        return
    marker_file = os.path.join(STORAGE_DIR, ".migration_sanitized")
    if os.path.exists(marker_file):
        return

    for fn in os.listdir(EPISODES_DIR):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(EPISODES_DIR, fn)
        try:
            with open(fp, "r") as f:
                content = f.read()
            
            if any(k in content for k in ["MAS FEAT", "MAS TRM", "Singapore MAS", "Singapore PDPC", "mas.gov.sg", "SR 11-7", "in banking perimeters"]):
                updated = (
                    content.replace("4. MAS FEAT Model Risk Compliance & Socratic QA", "4. NIST AI Risk Management & Enterprise Model Safety")
                    .replace("MAS FEAT Model Risk Compliance & Socratic QA", "NIST AI Risk Management & Enterprise Model Safety")
                    .replace("Zero-Trust SPIFFE Workload Tokens in Banking", "Zero-Trust SPIFFE Workload Tokens & Cryptographic Attestation")
                    .replace("Zero-Trust SPIFFE Workload Tokens & MAS TRM 9 (Full Paper)", "Zero-Trust SPIFFE Workload Tokens & Cryptographic Attestation (Full Paper)")
                    .replace("Zero-Trust SPIFFE Workload Identity in Banking & MAS TRM Sec 9", "Zero-Trust SPIFFE Workload Identity in Microservices & NIST SP 800-207")
                    .replace("Singapore MAS FEAT Principles & US Fed SR 11-7 Model Governance", "NIST AI Risk Management Framework (AI RMF 1.0) & ISO/IEC 42001 Governance")
                    .replace("Singapore PDPC Guidelines on Synthetic Data & Privacy-Preserving AI", "Enterprise Privacy-Preserving AI & Synthetic Data Governance")
                    .replace("https://www.mas.gov.sg/regulation/guidelines/technology-risk-management-guidelines", "https://csrc.nist.gov/publications/detail/sp/800-207/final")
                    .replace("https://www.mas.gov.sg/publications/monographs-or-information-paper/2018/FEAT", "https://www.nist.gov/itl/ai-risk-management-framework")
                    .replace("https://www.pdpc.gov.sg/help-and-resources/2020/01/model-ai-governance-framework", "https://www.nist.gov/privacy-framework")
                    .replace("MAS Technology Risk Management Guidelines (TRM Sec 9)", "NIST SP 800-207: Zero Trust Architecture")
                    .replace("Monetary Authority of Singapore FEAT Principles", "NIST AI Risk Management Framework")
                    .replace("Singapore PDPC AI Governance Framework", "NIST Privacy Framework & Synthetic Data")
                    .replace("Singapore MAS Technology Risk Management", "NIST SP 800-207")
                    .replace("MAS TRM Section 9.2 Compliance: Meets strict regulatory mandates for end-to-end mTLS encryption and automated 60-minute secret rotation.", "Zero-Trust Compliance: Meets strict mandates for end-to-end mTLS encryption and automated 60-minute secret rotation.")
                    .replace("MAS TRM Section 9", "NIST SP 800-207")
                    .replace("MAS TRM 9", "Zero Trust")
                    .replace("MAS FEAT", "NIST AI RMF")
                    .replace("Singapore MAS", "Enterprise Governance")
                    .replace("Singapore PDPC", "Data Privacy Standards")
                    .replace("in Banking", "in Microservices")
                    .replace("in banking perimeters", "in zero-trust perimeters")
                )
                with open(fp, "w") as f:
                    f.write(updated)
                logger.info(f"Sanitized historical episode {fn} of stale regional references.")
        except Exception as e:
            logger.error(f"Error sanitizing episode {fn}: {e}")

    try:
        with open(marker_file, "w") as mf:
            mf.write(datetime.now(timezone.utc).isoformat())
    except Exception as e:
        logger.warning(f"Could not write migration marker: {e}")

def enforce_retention_policy(custom_limit: Optional[int] = None) -> Dict[str, Any]:
    """Purges older episodes and audio files beyond the configured retention limit."""
    cfg = load_config()
    limit = int(custom_limit if custom_limit is not None else cfg.get("max_episodes_retained", 14))
    if limit <= 0 or not os.path.exists(EPISODES_DIR):
        return {"purged_episodes": 0, "freed_bytes": 0}

    files = [f for f in os.listdir(EPISODES_DIR) if f.startswith("ep-") and f.endswith(".json")]
    def sort_key(fn: str) -> int:
        try:
            return int(fn.replace("ep-", "").replace(".json", ""))
        except:
            return 0
    sorted_files = sorted(files, key=sort_key, reverse=True)

    freed_bytes = 0
    purged_count = 0

    if len(sorted_files) > limit:
        to_purge = sorted_files[limit:]
        for fn in to_purge:
            fp = os.path.join(EPISODES_DIR, fn)
            try:
                freed_bytes += os.path.getsize(fp)
                os.remove(fp)
                purged_count += 1
                logger.info(f"Retention policy: purged old episode JSON {fn}")
            except Exception as e:
                logger.error(f"Failed to purge episode {fn}: {e}")

            audio_fn = fn.replace(".json", ".mp3")
            audio_fp = os.path.join(AUDIO_DIR, audio_fn)
            if os.path.exists(audio_fp):
                try:
                    freed_bytes += os.path.getsize(audio_fp)
                    os.remove(audio_fp)
                    logger.info(f"Retention policy: purged old episode audio {audio_fn}")
                except Exception as e:
                    logger.error(f"Failed to purge audio {audio_fn}: {e}")

    return {"purged_episodes": purged_count, "freed_bytes": freed_bytes}

def get_storage_stats(retention_limit: Optional[int] = None) -> Dict[str, Any]:
    episodes_count = 0
    audio_count = 0
    total_bytes = 0

    if os.path.exists(EPISODES_DIR):
        for f in os.listdir(EPISODES_DIR):
            if f.endswith(".json"):
                episodes_count += 1
                total_bytes += os.path.getsize(os.path.join(EPISODES_DIR, f))

    if os.path.exists(AUDIO_DIR):
        for f in os.listdir(AUDIO_DIR):
            if f.endswith(".mp3"):
                audio_count += 1
                total_bytes += os.path.getsize(os.path.join(AUDIO_DIR, f))

    cfg = load_config() if retention_limit is None else {"max_episodes_retained": retention_limit}
    return {
        "total_episodes": episodes_count,
        "total_audio": audio_count,
        "disk_usage_bytes": total_bytes,
        "disk_usage_mb": round(total_bytes / (1024 * 1024), 2),
        "max_episodes_retained": cfg.get("max_episodes_retained", 14)
    }

def _safe_url(value: Any) -> str:
    """Return value only if it is an absolute http(s) URL; otherwise an empty string.

    Blocks javascript:, data:, file: and other schemes from reaching href attributes.
    Callers must still html.escape() the result when interpolating into HTML.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    try:
        if (parsed.scheme.lower() in ('http', 'https') and parsed.hostname
                and parsed.username is None and parsed.password is None
                and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate)
                and '\\' not in candidate and (parsed.port is None or 0 < parsed.port <= 65535)):
            return candidate
    except ValueError:
        return ''
    return ""

_HTML_TAG_RE = re.compile(r"<[^>]*>")

def _plain_text(value: Any) -> str:
    """Reduce possibly-HTML text to inert plain text for RSS <description>.

    Podcast clients commonly render <description> as HTML, so escaping is not
    enough (they decode entities back into live markup). Instead: unescape
    entities, then strip tags, repeating until stable so entity-encoded or
    nested markup (e.g. &lt;script&gt;, <scr<b>ipt>) cannot survive; finally drop any
    stray angle brackets and collapse whitespace. Stdlib only.
    """
    if not isinstance(value, str):
        return ""
    text = value
    for _ in range(10):
        previous = text
        text = _HTML_TAG_RE.sub("", html.unescape(text))
        if text == previous:
            break
    text = text.replace("<", "").replace(">", "")
    return " ".join(text.split())

def get_sorted_episode_files() -> List[str]:
    def sort_key(filename: str) -> int:
        try:
            base = filename.replace("ep-", "").replace(".json", "")
            return int(base)
        except ValueError:
            return 0
    files = [f for f in os.listdir(EPISODES_DIR) if re.fullmatch(r"ep-[0-9]+\.json", f)]
    return sorted(files, key=sort_key, reverse=True)

pipeline_state = {
    "running": False,
    "stage": "idle",
    "progress": 0,
    "message": "Ready",
    "last_run": None,
    "last_episode_id": None,
    "error": None
}

# Recovery reads one bounded file. Polling never scans episodes or source feeds.
source_health = load_source_health(STORAGE_DIR)
current_pipeline_task: Optional[asyncio.Task] = None


def _cache_source_health(snapshot):
    global source_health
    source_health = sanitize_source_health(snapshot)
    try:
        save_source_health(STORAGE_DIR, source_health)
    except Exception:
        # The valid in-memory check survives persistence failure.
        logger.error("Source-health snapshot write failed")


def _publish_source_check(run):
    snapshot = build_source_health(run.data["ingestion"], run.data["run_id"], datetime.now(timezone.utc).isoformat())
    snapshot["latest_run"] = source_health.get("latest_run")
    _cache_source_health(snapshot)


def _finish_source_run(run):
    snapshot = dict(source_health)
    if run.data["stages"]["ingestion"]["status"] != "completed":
        attempt = build_source_health(run.data["ingestion"], run.data["run_id"], run.data["finished_at"])
        snapshot["status"] = "cancelled" if run.data["status"] == "cancelled" else "unknown"
        snapshot["last_attempt_at"] = run.data["finished_at"]
        snapshot["last_attempt"] = {key: attempt[key] for key in ("run_id", "checked_at", "status", "available_sources", "sources")}
        snapshot["last_attempt"]["status"] = snapshot["status"]
    snapshot["latest_run"] = {"status": run.data["status"], "finished_at": run.data["finished_at"],
                              "episode_id": run.data.get("episode_id")}
    _cache_source_health(snapshot)


def _usable_audio(path):
    return isinstance(path, (str, os.PathLike)) and os.path.isfile(path) and os.path.getsize(path) > 1000


def _retained_audio_path(episode: dict, filename: str) -> str | None:
    """Resolve only an explicitly bound, decodable saved audio file.

    TP22 adds the committed artifact lease and complete version binding.
    No seed path, cache promotion, or synthesis is permitted on this read.
    """
    match = re.fullmatch(r"(ep-[0-9]+)(?:-([a-z]+))?\.mp3", filename)
    if not match or not isinstance(episode, dict):
        return None
    episode_id, domain = match.groups()
    if episode.get("id") != episode_id:
        return None
    try:
        if "story_manifest" in episode:
            valid, _ = validate_story_episode(episode)
            if not valid:
                return None
            manifest = manifest_from_dict(episode["story_manifest"])
            if episode.get("episode_fingerprint") != episode_fingerprint(manifest):
                return None
            if not domain and episode.get("audio_recipe_fingerprint") != audio_recipe_fingerprint(manifest, VOICE_MAP):
                return None
        if domain:
            declared = episode.get("domain_audio", {}).get(domain)
            state = episode.get("audio_availability", {}).get("domains", {}).get(domain, {})
            coverage = episode.get("content_availability", {}).get("domains", {}).get(domain, {})
            if coverage.get("status") != "available":
                return None
        else:
            declared = episode.get("audio_url")
            state = episode.get("audio_availability", {}).get("podcast", {})
        if declared != f"/audio/{filename}" or state.get("status") != "available":
            return None
        path = os.path.join(AUDIO_DIR, filename)
        if os.path.islink(path) or not _usable_audio(path):
            return None
        measured = MP3(path)
        if not measured.info or not math.isfinite(measured.info.length) or measured.info.length <= 0:
            return None
        if not domain:
            expected = episode.get("total_seconds")
            if expected is not None:
                if type(expected) not in (int, float) or not math.isfinite(expected) or expected <= 0:
                    return None
                if abs(measured.info.length - expected) > 2:
                    return None
            clock = episode.get("duration")
            if clock is not None:
                if not isinstance(clock, str) or not re.fullmatch(r"[0-9]+:[0-5][0-9]", clock):
                    return None
                minutes, seconds = map(int, clock.split(":"))
                if abs(measured.info.length - (minutes * 60 + seconds)) > 2:
                    return None
        return path
    except (ValueError, TypeError, KeyError, AttributeError, OSError, MutagenError):
        return None


def _retained_episode_view(saved: dict) -> dict:
    """Use the same audio oracle for list/detail metadata and media reads."""
    episode_id = saved["id"]
    links = saved.get("domain_audio", {})
    if not isinstance(links, dict):
        links = {}
    available_domains = frozenset(
        domain for domain in links if domain in DOMAIN_ORDER
        and _retained_audio_path(saved, f"{episode_id}-{domain}.mp3") is not None)
    return project_retained_episode(
        saved, audio_available=_retained_audio_path(saved, f"{episode_id}.mp3") is not None,
        available_domains=available_domains)


def _audit_action(action, outcome, *, run_id=None, changed_fields=()):
    try:
        append_record(STORAGE_DIR, "audit.jsonl", action_record(action, outcome, run_id=run_id, changed_fields=changed_fields))
        pipeline_state["action_audit_status"] = "recorded"
        pipeline_state["action_audit_error"] = None
    except Exception:
        pipeline_state["action_audit_status"] = "write_failed"
        pipeline_state["action_audit_error"] = "audit_write_failed"
        logger.error("Privileged-action audit write failed")


def _audit_handler_failures(action):
    def decorate(handler):
        @wraps(handler)
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except Exception:
                _audit_action(action, "handler_failed")
                raise
        return wrapped
    return decorate


def _finalize_pipeline_run(run, status, category=None, episode_path=None, episode=None):
    # The coroutine and its pre-entry cancellation callback share this guard.
    if run.append_attempted:
        return
    run.finish(status, category)
    metadata_failed = False
    if episode_path is not None and episode is not None:
        try:
            attach_episode_record(episode_path, episode, run.data)
        except Exception:
            metadata_failed = True
            run.data["status"] = "failed"
            run.data["error_category"] = "episode_metadata_write_failed"
            logger.error("Episode provenance write failed")
    _finish_source_run(run)
    run.append_attempted = True
    try:
        append_record(STORAGE_DIR, "runs.jsonl", run.data)
        pipeline_state["provenance_status"] = "write_failed" if metadata_failed else "recorded"
        pipeline_state["provenance_error"] = "episode_metadata_write_failed" if metadata_failed else None
    except Exception:
        pipeline_state["provenance_status"] = "write_failed"
        pipeline_state["provenance_error"] = "audit_write_failed"
        logger.error("Pipeline-run audit write failed")


async def _observed_stage(run, name, operation, returned_status="completed"):
    with run.stage(name, returned_status):
        return await operation


async def run_daily_pipeline(*, run_record=None):
    global current_pipeline_task
    run = run_record if run_record is not None else RunRecord()
    terminal_status, terminal_error = "failed", None
    episode_path, saved_episode = None, None
    pipeline_state["run_id"] = run.data["run_id"]
    pipeline_state["provenance_status"] = "pending"
    pipeline_state["provenance_error"] = None
    logger.info("Executing scheduled TechPulse daily ingestion and synthesis pipeline...")
    pipeline_state["running"] = True
    pipeline_state["stage"] = "checking"
    pipeline_state["progress"] = 15
    pipeline_state["message"] = "Checking configured RSS sources for available articles..."
    pipeline_state["error"] = None

    try:
        # Step 1: Ingestion with 30s hard timeout
        corpus = await _observed_stage(run, "ingestion", asyncio.wait_for(
            ingest_all_domains(diagnostics=run.data["ingestion"]), timeout=30.0))
        # Keep counts truthful even when an existing caller supplies a custom
        # ingestion function which does not populate optional feed diagnostics.
        run.data["ingestion"]["per_domain"] = {d: len(items) for d, items in corpus.items()}
        _publish_source_check(run)
        availability = build_content_availability(corpus, run.data["ingestion"])
        run.data["content_availability"] = availability
        if not any(state["candidate_count"] for state in availability["domains"].values()):
            feeds = run.data["ingestion"].get("feeds", [])
            expected_ids = {f"{domain}:{i}" for domain, values in DOMAIN_FEEDS.items() for i in range(len(values))}
            checked_ids = {feed.get("feed_id") for feed in feeds if isinstance(feed, dict)}
            all_failed = checked_ids == expected_ids and all(feed.get("outcome") in SOURCE_FAILURES for feed in feeds)
            terminal_status = "failed" if all_failed else "no_content"
            terminal_error = "sources_unavailable" if all_failed else None
            pipeline_state.update(stage="error" if all_failed else "no_content", progress=100,
                message="Checked sources were unavailable. Previous episodes remain available in history." if all_failed else
                        "No articles available from checked sources. Previous episodes remain available in history.",
                error=terminal_error, last_run=datetime.now(timezone.utc).isoformat())
            return
        manifest = freeze_story_manifest(corpus, availability)
        current_fingerprint = episode_fingerprint(manifest)
        dedup_started = time.monotonic()
        run.data['stages']['dedup']['status'] = 'running'
        all_corpus_urls = sorted(item['url'] for items in corpus.values() for item in items if item.get('url'))
        current_corpus_hash = hashlib.sha256(''.join(all_corpus_urls).encode('utf-8')).hexdigest()
        previous_titles: Optional[List[str]] = None
        sorted_files = get_sorted_episode_files()
        if sorted_files:
            with open(os.path.join(EPISODES_DIR, sorted_files[0])) as f:
                latest_ep = json.load(f)
            prior_chapter_titles = [c.get('title') for c in latest_ep.get('chapters', []) if isinstance(c, dict)]
            if prior_chapter_titles and all(isinstance(t, str) and t.strip() for t in prior_chapter_titles):
                previous_titles = prior_chapter_titles
            is_same_corpus = False
            if 'story_manifest' in latest_ep:
                try:
                    previous_manifest = manifest_from_dict(latest_ep['story_manifest'])
                    previous_fingerprint = episode_fingerprint(previous_manifest)
                    is_same_corpus = (latest_ep.get('episode_fingerprint') == previous_fingerprint
                                      and previous_fingerprint == current_fingerprint)
                except (ValueError, TypeError, KeyError):
                    pass
            if is_same_corpus:
                latest_num = latest_ep.get("episode_number", latest_ep.get("id", "142").replace("ep-", ""))
                logger.info(f"Selected evidence fingerprint matches latest Episode #{latest_num}. Skipping duplicate synthesis.")
                pipeline_state["stage"] = "up_to_date"
                pipeline_state["progress"] = 100
                pipeline_state["message"] = "Checked RSS candidates match the previous briefing. History remains available."
                pipeline_state["last_episode_id"] = latest_ep["id"]
                pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
                pipeline_state["running"] = False
                run.data["episode_id"] = latest_ep["id"]
                run.data["stages"]["dedup"].update(status="completed", duration_ms=round((time.monotonic()-dedup_started)*1000, 3))
                terminal_status = "skipped"
                return

        run.data["stages"]["dedup"].update(status="completed", duration_ms=round((time.monotonic()-dedup_started)*1000, 3))

        # Calculate next episode number
        existing = [f for f in os.listdir(EPISODES_DIR) if f.startswith("ep-") and f.endswith(".json")]
        next_num = 143 if not existing else max([int(f.split("-")[1].split(".")[0]) for f in existing if f.split("-")[1].split(".")[0].isdigit()] + [142]) + 1
        
        pipeline_state["stage"] = "synthesizing"
        pipeline_state["progress"] = 50
        pipeline_state["message"] = f"Available articles found. Synthesizing briefing for Episode #{next_num}..."

        # One outer synthesis budget includes story selection and provider cleanup.
        briefing_data = await _observed_stage(run, "synthesis", asyncio.wait_for(
            synthesize_briefing(corpus, next_num, previous_titles=previous_titles,
                               diagnostics=run.data["synthesis"], manifest=manifest), timeout=120.0))
        if briefing_data is None:
            terminal_status = "no_content"
            pipeline_state.update(stage="no_content", progress=100,
                message="No articles available from checked sources. Previous episodes remain available in history.",
                last_run=datetime.now(timezone.utc).isoformat())
            return
        valid, reason = validate_story_episode(briefing_data)
        if not valid:
            raise ValueError(reason)
        accepted = copy.deepcopy(briefing_data['story_manifest'])
        for story in accepted['stories']:
            story['selected_unit_ids'] = []
        if (accepted != manifest_to_dict(manifest)
                or briefing_data.get('episode_fingerprint') != current_fingerprint):
            raise ValueError('Synthesis changed frozen story identity')
        # Synthesis timing is not measured media evidence. Only identities enter
        # the bundle; its validated numeric cues may supply timing afterward.
        identity_keys = ('story_id', 'domain', 'title', 'source_name', 'source_url')
        briefing_data['chapters'] = [{key: chapter[key] for key in identity_keys}
                                    for chapter in briefing_data['chapters']]
        # Received counts and source-health decisions belong to ingestion.
        briefing_data['content_availability'] = availability
        run.data["episode_id"] = briefing_data["id"]
        run.data["selection"] = selection_record(corpus, briefing_data, run.data["synthesis"]["path"],
                                                provider_attempted=bool(run.data["synthesis"]["attempts"]))
        briefing_data["corpus_hash"] = current_corpus_hash
        briefing_data["ingested_urls"] = all_corpus_urls
        
        pipeline_state["stage"] = "audio_tts"
        pipeline_state["progress"] = 75
        pipeline_state["message"] = "Synthesizing Neural Edge-TTS podcast dialogue (GuyNeural & AriaNeural)..."

        # Both provenance stages describe one bounded bundle; its internal
        # 60-second deadline includes cleanup and every domain output.
        bundle = None
        try:
            with run.stage('podcast_audio', 'call_returned'), run.stage('domain_audio', 'call_returned'):
                bundle = await generate_story_audio_bundle(briefing_data, AUDIO_DIR)
        except Exception as exc:
            logger.warning('Story audio unavailable (category=%s)', error_category(exc))
        for name in ('podcast_audio', 'domain_audio'):
            run.data['stages'][name]['operation'] = 'story_audio_bundle'
        recipe = audio_recipe_fingerprint(manifest_from_dict(briefing_data['story_manifest']), VOICE_MAP)
        briefing_data['audio_recipe_fingerprint'] = recipe
        expected_podcast = os.path.abspath(os.path.join(AUDIO_DIR, briefing_data['id'] + '.mp3'))
        podcast_present = False
        measured_chapters = []
        domain_outputs = {}
        audio_availability = {'podcast': {'status': 'unavailable', 'reason': 'Audio generation failed.'},
            'domains': {d: {'status': 'unavailable', 'reason': availability['domains'][d]['reason']
                if availability['domains'][d]['status'] != 'available' else 'Audio generation failed.'}
                for d in DOMAIN_ORDER}}
        if bundle is not None and bundle.recipe_fingerprint == recipe:
            audio_availability = copy.deepcopy(bundle.availability)
            domain_outputs = bundle.domain_paths if isinstance(bundle.domain_paths, dict) else {}
            total = bundle.total_seconds
            cues = bundle.chapters
            ids = [c['story_id'] for c in briefing_data['chapters']]
            usable_cues = (isinstance(cues, list) and len(cues) == len(ids)
                and all(isinstance(c, dict) for c in cues)
                and [c.get('story_id') for c in cues] == ids
                and all(type(c.get('seconds')) in (int, float) and math.isfinite(c['seconds'])
                        and 0 <= c['seconds'] < total and isinstance(c.get('time'), str)
                        # Canonical minutes/seconds must agree with the measured
                        # numeric cue, including fractional-second truncation.
                        and c['time'] == format_seconds_to_time(int(c['seconds'])) for c in cues)
                and cues[0]['seconds'] == 0
                and all(a['seconds'] < b['seconds'] for a, b in zip(cues, cues[1:]))) if type(total) in (int, float) and math.isfinite(total) and total > 0 else False
            podcast_present = (audio_availability.get('podcast', {}).get('status') == 'available'
                and _usable_audio(bundle.podcast_path)
                and os.path.abspath(bundle.podcast_path) == expected_podcast and usable_cues)
            if podcast_present:
                # Keep source identity from the validated text, joining only measured timing.
                measured_chapters = [{**chapter, 'seconds': cue['seconds'], 'time': cue['time']}
                                     for chapter, cue in zip(briefing_data['chapters'], cues)]
        briefing_data['audio_url'] = '/audio/' + briefing_data['id'] + '.mp3' if podcast_present else ''
        briefing_data['duration'] = bundle.duration if podcast_present else '00:00'
        briefing_data['total_seconds'] = bundle.total_seconds if podcast_present else 0
        if measured_chapters:
            briefing_data['chapters'] = measured_chapters
        # Text-only chapters retain their identity without fabricated timing.
        briefing_data['domain_audio'] = {domain: '/audio/' + briefing_data['id'] + '-' + domain + '.mp3'
            for domain, path in domain_outputs.items() if domain in DOMAIN_ORDER
            and availability['domains'][domain]['status'] == 'available'
            and audio_availability.get('domains', {}).get(domain, {}).get('status') == 'available'
            and _usable_audio(path) and os.path.abspath(path) == os.path.abspath(
                os.path.join(AUDIO_DIR, briefing_data['id'] + '-' + domain + '.mp3'))}
        audio_availability.setdefault('podcast', {})['status'] = 'available' if podcast_present else 'unavailable'
        if not podcast_present:
            audio_availability['podcast']['reason'] = 'Audio generation failed or returned no playable track.'
        audio_availability.setdefault('domains', {})
        for domain in DOMAIN_ORDER:
            state = audio_availability['domains'].setdefault(domain, {'reason': 'Audio generation failed.'})
            present = domain in briefing_data['domain_audio']
            state['status'] = 'available' if present else 'unavailable'
            if not present:
                state['reason'] = availability['domains'][domain]['reason'] if availability['domains'][domain]['status'] != 'available' else 'Audio generation failed or returned no playable track.'
        briefing_data['audio_availability'] = audio_availability
        duration_str = briefing_data['duration']
        run.data['stages']['podcast_audio'].update(output_file_present=bool(bundle and _usable_audio(bundle.podcast_path)),
            returned_chapters=len(bundle.chapters) if bundle and isinstance(bundle.chapters, list) else 0,
            published_chapters=len(measured_chapters), audio_validated=podcast_present)
        run.data['stages']['domain_audio'].update(
            returned_outputs=len(bundle.domain_paths) if bundle and isinstance(bundle.domain_paths, dict) else 0,
            published_outputs=len(briefing_data['domain_audio']),
            expected_domains=len({s.domain for s in manifest.stories}), audio_validated=bool(briefing_data['domain_audio']))

        # Save episode JSON
        ep_path = os.path.join(EPISODES_DIR, f"{briefing_data['id']}.json")
        with run.stage("episode_write"):
            with open(ep_path, "w") as f:
                json.dump(briefing_data, f, indent=2)
        episode_path, saved_episode = ep_path, briefing_data
            
        pipeline_state["stage"] = "complete"
        pipeline_state["progress"] = 100
        pipeline_state["message"] = f"New Episode #{next_num} generated successfully!"
        pipeline_state["last_episode_id"] = briefing_data["id"]
        pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
        pipeline_state["running"] = False

        # Enforce retention policy automatically
        with run.stage("retention", "call_returned"):
            cfg = load_config()
            cleanup = enforce_retention_policy(cfg.get("max_episodes_retained", 14))
            run.data["stages"]["retention"]["reported_deleted"] = cleanup.get("purged_episodes", 0)
        terminal_status = "completed"

        logger.info(f"Successfully generated Episode #{next_num}: {briefing_data['title']} ({duration_str})")
    except asyncio.CancelledError:
        terminal_status, terminal_error = "cancelled", "cancelled"
        logger.info("Pipeline task cancelled by user.")
        pipeline_state["running"] = False
        pipeline_state["stage"] = "idle"
        pipeline_state["progress"] = 0
        pipeline_state["message"] = "Pipeline stopped by user."
        pipeline_state["error"] = None
        raise
    except asyncio.TimeoutError:
        terminal_error = "timeout"
        pipeline_state["running"] = False
        pipeline_state["stage"] = "error"
        pipeline_state["error"] = "Operation timed out."
        pipeline_state["message"] = "Pipeline execution timed out. Aborted."
        logger.error("Pipeline timed out.")
    except Exception as e:
        terminal_error = error_category(e)
        pipeline_state["running"] = False
        pipeline_state["stage"] = "error"
        pipeline_state["error"] = terminal_error
        pipeline_state["message"] = "Pipeline failed. Check the run status for details."
        logger.error("Daily pipeline failed (category=%s)", terminal_error)
    finally:
        pipeline_state["running"] = False
        for stage in run.data["stages"].values():
            if stage["status"] == "running":
                stage.update(status=terminal_status, error_category=terminal_error)
                if "dedup_started" in locals():
                    stage["duration_ms"] = round((time.monotonic()-dedup_started)*1000, 3)
        _finalize_pipeline_run(run, terminal_status, terminal_error, episode_path, saved_episode)

class ChatRequest(BaseModel):
    query: str
    episode_id: Optional[str] = "ep-142"

@app.get("/healthz")
async def health_check():
    raw_key = os.getenv("GEMINI_API_KEY", "")
    clean_key = raw_key.strip().strip('"').strip("'")
    key_configured = bool(clean_key and len(clean_key) > 10 and not clean_key.startswith("${"))
    ep_count = len([f for f in os.listdir(EPISODES_DIR) if f.endswith(".json")])
    return {
        "status": "healthy",
        "service": "techpulse-os",
        "version": APP_VERSION,
        "gemini_api_key_configured": key_configured,
        "episodes_count": ep_count,
        "scheduler_running": scheduler.running,
        "scheduler_status": dict(scheduler_status),
        "pipeline_status": pipeline_state,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/episodes")
async def get_episodes(auth: bool = Depends(require_auth)):
    episodes = []
    for f in get_sorted_episode_files():
        episode_id = f[:-5]
        try:
            with open(os.path.join(EPISODES_DIR, f), "r") as fp:
                saved = json.load(fp)
            if not isinstance(saved, dict) or saved.get("id") != episode_id:
                raise ValueError("retained episode identity mismatch")
            episodes.append(_retained_episode_view(saved))
        except (OSError, ValueError, TypeError) as exc:
            # Keep the retained identifier visible without inventing metadata
            # or downgrading a malformed captured record to never-captured.
            logger.warning("Retained episode %s unavailable (%s)", episode_id, type(exc).__name__)
            episodes.append({"id": episode_id, "audio_url": "",
                             "legacy_compatibility": {"schema_version": 1,
                                 "provenance": "unknown", "evidence": "unavailable",
                                 "full_article_features": "unavailable",
                                 "reason": "Retained episode record unreadable or invalid"}})
    return {"episodes": episodes}

async def get_episode_detail(episode_id: str, auth: bool = Depends(require_auth)):
    """Raw internal legacy reader retained for existing non-API callers."""
    if not re.fullmatch(r"ep-[0-9]+", episode_id):
        raise HTTPException(status_code=404, detail="Episode not found")
    ep_file = os.path.join(EPISODES_DIR, f"{episode_id}.json")
    try:
        with open(ep_file, "r") as fp:
            saved = json.load(fp)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Episode not found")
    except (OSError, ValueError):
        raise HTTPException(status_code=409, detail="Retained episode record unavailable")
    if not isinstance(saved, dict) or saved.get("id") != episode_id:
        raise HTTPException(status_code=409, detail="Retained episode identity invalid")
    return saved


@app.get("/api/episodes/{episode_id}")
async def read_episode_detail(episode_id: str, auth: bool = Depends(require_auth)):
    saved = await get_episode_detail(episode_id, auth)
    try:
        return _retained_episode_view(saved)
    except (ValueError, TypeError):
        raise HTTPException(status_code=409, detail="Retained episode manifest or provenance invalid")

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, auth: bool = Depends(require_auth)):
    # TP19/TP44 must replace the legacy provider cascade with an approved,
    # revision-bound adapter before this route can make a provider call.
    raise HTTPException(status_code=503, detail="Chat model and adapter are not approved")

@app.post("/api/refresh")
@_audit_handler_failures("refresh")
async def manual_refresh(auth: bool = Depends(require_auth)):
    result = run_coordinator.start_manual()
    if result["status"] == "busy":
        _audit_action("refresh", "busy")
        return {"status": "busy", "message": "Ingestion pipeline is already actively running.", "state": pipeline_state}
    if result["status"] != "started":
        raise HTTPException(status_code=503, detail={"status": "blocked", "reason": result.get("reason")})
    pipeline_state.update(running=True, stage="processing", progress=0,
                          message="Coordinated worker accepted; read durable run status for completion.",
                          error=None, run_id=result["run_id"], provenance_status="pending",
                          provenance_error=None)
    _audit_action("refresh", "request_accepted", run_id=result["run_id"])
    return {"status": "ok", "message": "Ingestion and synthesis pipeline triggered in background.", "state": pipeline_state}

@app.post("/api/refresh/cancel")
@_audit_handler_failures("refresh_cancel")
async def cancel_refresh(auth: bool = Depends(require_auth)):
    result = run_coordinator.cancel_owned()
    if result["status"] == "no_active_task":
        _audit_action("refresh_cancel", "no_active_task")
        return {"status": "no_active_task", "message": "No coordinated worker is active.",
                "state": pipeline_state}
    _audit_action("refresh_cancel", "request_accepted", run_id=result.get("run_id"))
    if result["status"] != "stopped_pending_reconciliation":
        raise HTTPException(status_code=409, detail=result)
    pipeline_state.update(running=False, stage="blocked", progress=0,
                          message="Worker stopped; retained publication requires reconciliation.",
                          error="pending_reconciliation")
    return {"status": "stopped_pending_reconciliation", "state": pipeline_state}

@app.post("/api/refresh/reset")
@_audit_handler_failures("refresh_reset")
async def reset_refresh(auth: bool = Depends(require_auth)):
    result = run_coordinator.cancel_owned()
    if result["status"] != "no_active_task":
        _audit_action("refresh_reset", "handler_failed", run_id=result.get("run_id"))
        raise HTTPException(status_code=409, detail={"status": "blocked",
            "reason": "run_or_publication_reconciliation_required"})
    pipeline_state.update(running=False, stage="idle", progress=0,
                          message="Ready", error=None)
    _audit_action("refresh_reset", "no_active_task")
    return {"status": "reset", "message": "No active run was reset.", "state": pipeline_state}

@app.get("/api/refresh/status")
async def get_refresh_status(auth: bool = Depends(require_auth)):
    coordinated = run_coordinator.status()
    current_source_health = (load_source_health(STORAGE_DIR)
                             if coordinated.get("run_id") else source_health)
    state = {**pipeline_state, "source_health": current_source_health,
             "coordinated_run": coordinated}
    if coordinated.get("run_id") is not None:
        state["run_id"] = coordinated["run_id"]
        state["running"] = coordinated["status"] == "running"
        state["stage"] = coordinated["status"]
        if coordinated["status"] == "completed":
            state["progress"] = 100
        elif coordinated["status"] == "blocked":
            state["error"] = coordinated.get("reason")
    elif coordinated["status"] == "blocked":
        state.update(running=False, stage="blocked", error=coordinated.get("reason"))
    return state

@app.get("/api/settings")
async def get_settings(auth: bool = Depends(require_auth)):
    service = SettingsService(STORAGE_DIR)
    try:
        current = service.read()
    except SettingsIntegrityError as exc:
        kind = service.initialization_kind()
        detail = {"message": str(exc), "initialization": kind}
        if kind == "fresh":
            detail["fresh_defaults"] = SettingsService.fresh_defaults()
        raise HTTPException(status_code=409, detail=detail) from exc
    try:
        current["storage"] = get_storage_stats(current["config"]["max_episodes_retained"])
        current["storage_status"] = "available"
    except OSError as exc:
        logger.warning("Settings readable; storage statistics unavailable: %s", type(exc).__name__)
        current["storage"] = None
        current["storage_status"] = "unavailable"
    if scheduler.running:
        if (scheduler_status["bound_revision"] == current["revision"]
                and scheduler_status["status"] in ("ready", "busy", "started")):
            current["application_status"]["schedule"] = "applied"
        elif scheduler_status["status"] == "blocked":
            current["application_status"]["schedule"] = "unavailable"
    return current

class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Dict[str, Any]
    expected_revision: int = Field(strict=True, ge=0)
    initialization: Optional[str] = None

@app.post("/api/settings")
async def update_settings(payload: SettingsUpdate, auth: bool = Depends(require_auth)):
    try:
        result = SettingsService(STORAGE_DIR).save(
            payload.config, expected_revision=payload.expected_revision,
            initialization=payload.initialization)
    except SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail={"fields": exc.fields}) from exc
    except SettingsConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SettingsIntegrityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Required Settings commit or audit failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Settings could not be committed and audited") from exc
    try:
        result["storage"] = get_storage_stats(result["config"]["max_episodes_retained"])
        result["storage_status"] = "available"
    except OSError as exc:
        logger.warning("Settings committed; storage statistics unavailable: %s", type(exc).__name__)
        result["storage"] = None
        result["storage_status"] = "unavailable"
    return result

@app.post("/api/settings/cleanup")
@_audit_handler_failures("settings_cleanup")
async def trigger_storage_cleanup(auth: bool = Depends(require_auth)):
    cleanup_res = enforce_retention_policy()
    stats = get_storage_stats()
    _audit_action("settings_cleanup", "handler_returned")
    return {
        "status": "success",
        "cleanup": cleanup_res,
        "storage": stats
    }

def _export_url(value):
    return quote(_safe_url(value), safe=':/?&=;%+@!$,*~-.#')


def _markdown_text(value):
    literal = html.escape(str(value if value is not None else ''), quote=True)
    literal = ' '.join(literal.splitlines())
    return re.sub(r'([\\`*_{}\[\]()#+|>~$])', r'\\\1', literal)


def _podcast_is_available(ep):
    coverage = ep.get('content_availability')
    has_coverage = isinstance(coverage, dict) and type(coverage.get('schema_version')) is int and coverage['schema_version'] == 1
    if 'story_manifest' not in ep and not has_coverage:
        return True  # Preserve the historical reader.
    episode_id = ep.get('id')
    if not isinstance(episode_id, str) or not re.fullmatch(r'ep-[0-9]+', episode_id):
        return False
    return (ep.get('audio_availability', {}).get('podcast', {}).get('status') == 'available'
            and ep.get('audio_url') == f'/audio/{episode_id}.mp3'
            and _usable_audio(os.path.join(AUDIO_DIR, f'{episode_id}.mp3')))


def generate_markdown_content(ep: Dict[str, Any], ep_id: str) -> str:
    text = _markdown_text
    scalar = lambda value: json.dumps(str(value if value is not None else ''), ensure_ascii=False)
    md_content = '\n'.join(['---',
        'title: ' + scalar(ep.get('title')), 'date: ' + scalar(ep.get('date')),
        'duration: ' + scalar(ep.get('duration')), 'hosts: ' + scalar(ep.get('hosts')),
        'tags:', '  - techpulse/daily-briefing', '  - ' + scalar('episode/' + str(ep_id)),
        '  - architecture/enterprise', '  - cloud/resiliency', '  - ai/agent-governance',
        'status: permanent', 'type: literature-note', '---', '', '# ' + text(ep.get('title')), '',
        f"**Date**: {text(ep.get('date'))} | **Duration**: {text(ep.get('duration'))} | **Hosts**: {text(ep.get('hosts'))} | **Series**: [[TechPulse Daily Briefings MOC]]",
        '', '---', '', '## Executive Summary', text(ep.get('summary')), '', '---', '',
        '## Timecoded Chapters & Primary Whitepapers', ''])
    if ep.get('content_basis') == 'rss_summaries':
        md_content += '\nContent basis: received RSS summaries; full article bodies were not checked.\n\n'
    timed = _podcast_is_available(ep)
    for chapter in ep.get('chapters', []):
        cue = f"**[{text(chapter['time'])}]** " if timed and chapter.get('time') is not None else ''
        md_content += f"- {cue}{text(chapter.get('title'))} — [{text(chapter.get('source_name'))}]({_export_url(chapter.get('source_url'))})\n"
    md_content += '\n---\n\n## Domain Takeaways\n'
    for domain, data in ep.get('takeaways', {}).items():
        md_content += f"\n### {text(data.get('badge', domain.upper()))}: {text(data.get('title'))}\n"
        availability = ep.get('content_availability', {}).get('domains', {}).get(domain, {})
        if availability.get('status') in ('no_received_candidates', 'source_unavailable'):
            md_content += '\n' + text(availability.get('reason', 'No articles available from checked sources.')) + '\n'
            continue
        for bullet in data.get('bullets', []):
            md_content += '- ' + text(bullet) + '\n'
        if data.get('interview_framing'):
            md_content += '\n> [!TIP]\n> **Staff Architect Interview & Regulatory Framing:**\n> ' + text(data['interview_framing']) + '\n'
    md_content += """
---

## Related Notes & Vault Navigation
- **Series Index**: [[TechPulse Daily Briefings MOC]]
- **Study Notes Index**: [[Research Notes MOC]]
- **Tags**: #techpulse/daily-briefing #architecture/enterprise #ai/agent-governance
"""
    return md_content

@app.get("/api/export-markdown/{episode_id}")
async def export_markdown_file(episode_id: str, auth: bool = Depends(require_auth)):
    ep_file = os.path.join(EPISODES_DIR, f"{episode_id}.json")
    if not os.path.exists(ep_file):
        seed_f = os.path.join(os.path.dirname(__file__), "..", "seed_data", "episodes", f"{episode_id}.json")
        if os.path.exists(seed_f):
            ep_file = seed_f
        else:
            ep_file = os.path.join(EPISODES_DIR, "ep-142.json")
    
    if not os.path.exists(ep_file):
        raise HTTPException(status_code=404, detail="Episode not found")

    with open(ep_file, "r") as fp:
        ep = json.load(fp)

    md_content = generate_markdown_content(ep, episode_id)
    return Response(
        content=md_content,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="techpulse-{episode_id}.md"',
            "Access-Control-Allow-Origin": "*"
        }
    )

@app.post("/api/export-vault")
async def export_vault(req: Dict[str, Any], auth: bool = Depends(require_auth)):
    ep_id = req.get("episode_id", "ep-142")
    ep_file = os.path.join(EPISODES_DIR, f"{ep_id}.json")
    if not os.path.exists(ep_file):
        seed_f = os.path.join(os.path.dirname(__file__), "..", "seed_data", "episodes", f"{ep_id}.json")
        if os.path.exists(seed_f):
            ep_file = seed_f
        else:
            ep_file = os.path.join(EPISODES_DIR, "ep-142.json")
    
    if not os.path.exists(ep_file):
        raise HTTPException(status_code=404, detail="Episode not found")

    with open(ep_file, "r") as fp:
        ep = json.load(fp)

    md_content = generate_markdown_content(ep, ep_id)
    filename = f"techpulse-{ep_id}.md"

    return {
        "status": "success",
        "episode_id": ep_id,
        "filename": filename,
        "markdown": md_content,
        "message": f"Successfully exported Episode #{ep.get('episode_number', ep_id)} as Markdown note ({filename})."
    }

@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    audio_match = re.fullmatch(r"(ep-[0-9]+)(?:-([a-z]+))?\.mp3", filename)
    if not audio_match:
        raise HTTPException(status_code=404, detail="Audio unavailable")
    episode_id = audio_match.group(1)
    episode_file = os.path.join(EPISODES_DIR, f"{episode_id}.json")
    try:
        with open(episode_file, "r") as fp:
            episode = json.load(fp)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Audio unavailable")
    file_path = _retained_audio_path(episode, filename)
    if file_path is None:
        raise HTTPException(status_code=404, detail="Audio unavailable")
    return FileResponse(file_path, media_type="audio/mpeg",
                        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache",
                                 "Access-Control-Allow-Origin": "*"})

@app.get("/feed.xml")
async def podcast_rss(request: Request):
    host_url = os.getenv("HOST_URL", str(request.base_url).rstrip("/"))
    
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:content": "http://purl.org/rss/1.0/modules/content/",
        "xmlns:psc": "http://podlove.org/simple-chapters"
    })
    
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "TechPulse OS — Daily Engineering & Architecture Briefings"
    ET.SubElement(channel, "link").text = host_url
    ET.SubElement(channel, "description").text = "Synthesized multi-domain technical briefings across AI agents, cloud platforms, modern lakehouses, zero trust, and enterprise AI risk governance."
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "itunes:author").text = "TechPulse Intelligence Engine"
    ET.SubElement(channel, "itunes:category", {"text": "Technology"})
    ET.SubElement(channel, "itunes:explicit").text = "false"

    for f in get_sorted_episode_files():
        with open(os.path.join(EPISODES_DIR, f), "r") as fp:
            ep = json.load(fp)

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"Episode #{ep.get('episode_number', 142)}: {ep.get('title')}"
        ET.SubElement(item, "link").text = f"{host_url}/#/{ep.get('id')}"
        ET.SubElement(item, "guid").text = ep.get("id")
        
        # Format RFC 822 pubDate
        date_str = ep.get("date", "")
        try:
            dt = datetime.strptime(date_str, "%b %d, %Y").replace(tzinfo=timezone.utc)
            pub_date_rfc = email.utils.format_datetime(dt)
        except Exception:
            pub_date_rfc = email.utils.formatdate(usegmt=True)

        ET.SubElement(item, "pubDate").text = pub_date_rfc
        ET.SubElement(item, "description").text = _plain_text(ep.get("summary"))
        
        publish_podcast = _podcast_is_available(ep)
        # HTML Show notes with direct article links
        show_notes_html = f"<p>{html.escape(str(ep.get('summary', '')), quote=True)}</p><h3>Podcast Chapters & Source Links:</h3><ul>"
        for c in ep.get("chapters", []):
            c_time = html.escape(str(c.get("time", "")), quote=True) if publish_podcast else ""
            c_title = html.escape(str(c.get("title", "")), quote=True)
            c_source = html.escape(str(c.get("source_name", "")), quote=True)
            c_href = html.escape(_export_url(c.get("source_url")), quote=True)
            cue = f"<strong>{c_time}</strong>: " if c_time else ""
            show_notes_html += f"<li>{cue}<a href=\"{c_href}\">{c_title} ({c_source})</a></li>"
        show_notes_html += "</ul>"
        if ep.get("content_basis") == "rss_summaries":
            show_notes_html += "<p>Content basis: received RSS summaries; full article bodies were not checked.</p>"
        for domain, state in ep.get("content_availability", {}).get("domains", {}).items():
            if state.get("status") in ("no_received_candidates", "source_unavailable"):
                show_notes_html += f"<p>{html.escape(domain.upper())}: {html.escape(str(state.get('reason', 'No articles available from checked sources.')))}</p>"
        
        content_encoded = ET.SubElement(item, "content:encoded")
        content_encoded.text = show_notes_html
        
        ET.SubElement(item, "itunes:duration").text = ep.get("duration", "05:20")
        
        audio_url = f"{host_url}/audio/{ep.get('id')}.mp3"
        audio_length = "5242880"
        audio_file_path = os.path.join(AUDIO_DIR, f"{ep.get('id')}.mp3")
        if os.path.exists(audio_file_path):
            audio_length = str(os.path.getsize(audio_file_path))
        else:
            seed_audio_path = os.path.join(os.path.dirname(__file__), "..", "seed_data", "audio", f"{ep.get('id')}.mp3")
            if os.path.exists(seed_audio_path):
                audio_length = str(os.path.getsize(seed_audio_path))

        if publish_podcast:
            ET.SubElement(item, 'enclosure', {'url': _export_url(audio_url),
                'length': audio_length, 'type': 'audio/mpeg'})
            psc = ET.SubElement(item, 'psc:chapters', {'version': '1.2'})
            for c in ep.get('chapters', []):
                if c.get('time') is not None:
                    ET.SubElement(psc, 'psc:chapter', {'start': str(c['time']),
                        'title': str(c.get('title', '')), 'href': _export_url(c.get('source_url'))})

    xml_str = ET.tostring(rss, encoding="utf-8", method="xml")
    return Response(content=xml_str, media_type="application/rss+xml")

@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def serve_favicon():
    fav_file = os.path.join(STATIC_DIR, "favicon.svg")
    if os.path.exists(fav_file):
        return FileResponse(fav_file, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found")

# Root Route with No-Cache headers to prevent stale UI caching
@app.get("/", response_class=FileResponse)
async def serve_root():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(
            index_file,
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    raise HTTPException(status_code=404, detail="Frontend index.html not found")

# Mount SPA Static Frontend
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

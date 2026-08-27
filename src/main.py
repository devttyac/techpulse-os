import asyncio
import json
import logging
import os
import shutil
import hashlib
import xml.etree.ElementTree as ET
import email.utils
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.ingestion import ingest_all_domains
from src.synthesizer import synthesize_briefing
from src.tts_engine import generate_episode_podcast_audio, generate_all_domain_audios
from src.grounded_chat import process_grounded_chat

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.main")

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    cron_expr = cfg.get("cron_schedule", os.getenv("CRON_SCHEDULE", "0 7 * * *"))
    try:
        parts = cron_expr.split()
        if len(parts) == 5:
            trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4], timezone="Asia/Singapore")
            scheduler.add_job(run_daily_pipeline, trigger)
            scheduler.start()
            logger.info(f"Scheduled daily pipeline with cron [{cron_expr}] SGT")
    except Exception as e:
        logger.warning(f"Could not parse cron expression: {e}")

    # Run startup seed data initialization, duplicate cleanup, migration, and retention
    try:
        init_seed_data()
        cleanup_duplicate_episodes()
        sanitize_existing_episodes()
        enforce_retention_policy()
    except Exception as e:
        logger.error(f"Error in startup initialization/sanitization/retention: {e}")

    # Pre-generate seed episode audio in background if missing
    try:
        ep142_mp3 = os.path.join(AUDIO_DIR, "ep-142.mp3")
        if not os.path.exists(ep142_mp3):
            with open(os.path.join(EPISODES_DIR, "ep-142.json"), "r") as fp:
                ep142_data = json.load(fp)
            asyncio.create_task(generate_episode_podcast_audio(ep142_data, AUDIO_DIR))
    except Exception as e:
        logger.error(f"Error checking seed audio on startup: {e}")

    yield

    if scheduler.running:
        scheduler.shutdown()

app = FastAPI(
    title="TechPulse OS",
    description="Multi-Domain Technical Intelligence & Socratic Sparring Platform",
    version="3.4.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def verify_mutating_auth(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    """Optional shared secret verification for mutating endpoints if API_SECRET_KEY is configured."""
    secret = os.getenv("API_SECRET_KEY", "").strip()
    if secret:
        if not x_api_key or x_api_key != secret:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing X-API-Key header")
    return True

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
    # 1. Populate from seed_data, always replacing if seed file is larger/newer
    seed_dir = os.path.join(os.path.dirname(__file__), "..", "seed_data")
    if os.path.exists(seed_dir):
        for root, dirs, files in os.walk(seed_dir):
            rel = os.path.relpath(root, seed_dir)
            target_dir = os.path.join(STORAGE_DIR, rel)
            os.makedirs(target_dir, exist_ok=True)
            for file in files:
                src_f = os.path.join(root, file)
                dst_f = os.path.join(target_dir, file)
                # Overwrite if destination is missing or smaller than seed
                if not os.path.exists(dst_f) or os.path.getsize(dst_f) < os.path.getsize(src_f):
                    try:
                        shutil.copyfile(src_f, dst_f)
                        logger.info(f"Force-updated seed asset to volume: {dst_f} ({os.path.getsize(src_f)} bytes)")
                    except Exception as e:
                        logger.error(f"Failed to copy seed file {src_f}: {e}")

    # 2. Populate and upgrade JSON episode definitions with full_articles corpus
    seed_json_dir = os.path.join(os.path.dirname(__file__), "..", "seed_data", "episodes")
    if os.path.exists(seed_json_dir):
        for f in os.listdir(seed_json_dir):
            if f.endswith(".json"):
                src_f = os.path.join(seed_json_dir, f)
                dst_f = os.path.join(EPISODES_DIR, f)
                # Overwrite if destination lacks full_articles or is smaller than seed
                need_update = True
                if os.path.exists(dst_f):
                    try:
                        with open(dst_f, "r") as fp:
                            curr_data = json.load(fp)
                        if "full_articles" in curr_data:
                            need_update = False
                    except:
                        need_update = True
                if need_update:
                    shutil.copyfile(src_f, dst_f)
                    logger.info(f"Upgraded episode JSON with full paper corpus: {dst_f}")

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

def get_storage_stats() -> Dict[str, Any]:
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

    cfg = load_config()
    return {
        "total_episodes": episodes_count,
        "total_audio": audio_count,
        "disk_usage_bytes": total_bytes,
        "disk_usage_mb": round(total_bytes / (1024 * 1024), 2),
        "max_episodes_retained": cfg.get("max_episodes_retained", 14)
    }

def cleanup_duplicate_episodes():
    """Scans EPISODES_DIR and purges any duplicate episodes with identical titles or summaries."""
    if not os.path.exists(EPISODES_DIR):
        return
    files = [f for f in os.listdir(EPISODES_DIR) if f.startswith("ep-") and f.endswith(".json")]
    def sort_key(fn: str) -> int:
        try:
            return int(fn.replace("ep-", "").replace(".json", ""))
        except:
            return 0
    sorted_files = sorted(files, key=sort_key)
    seen_signatures = {}
    
    for fn in sorted_files:
        fp = os.path.join(EPISODES_DIR, fn)
        try:
            with open(fp, "r") as f:
                data = json.load(f)
            title = data.get("title", "").strip().lower()
            summary = data.get("summary", "").strip().lower()
            sig_title = f"title::{title}"
            sig_sum = f"sum::{summary[:100]}"
            
            is_dup = bool(title and sig_title in seen_signatures) or bool(summary and sig_sum in seen_signatures)
            if is_dup:
                canonical_fn = seen_signatures.get(sig_title) or seen_signatures.get(sig_sum)
                logger.warning(f"Detected duplicate episode {fn} (identical to {canonical_fn}). Purging duplicate...")
                try:
                    os.remove(fp)
                    audio_fp = os.path.join(AUDIO_DIR, fn.replace(".json", ".mp3"))
                    if os.path.exists(audio_fp):
                        os.remove(audio_fp)
                    logger.info(f"Successfully purged duplicate episode {fn}")
                except Exception as e:
                    logger.error(f"Failed to remove duplicate {fn}: {e}")
            else:
                if title:
                    seen_signatures[sig_title] = fn
                if summary:
                    seen_signatures[sig_sum] = fn
        except Exception as e:
            logger.error(f"Error checking episode {fn} for duplicates: {e}")

def get_sorted_episode_files() -> List[str]:
    cleanup_duplicate_episodes()
    def sort_key(filename: str) -> int:
        try:
            base = filename.replace("ep-", "").replace(".json", "")
            return int(base)
        except ValueError:
            return 0
    files = [f for f in os.listdir(EPISODES_DIR) if f.endswith(".json")]
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

current_pipeline_task: Optional[asyncio.Task] = None

async def run_daily_pipeline():
    global current_pipeline_task
    logger.info("Executing scheduled TechPulse daily ingestion and synthesis pipeline...")
    pipeline_state["running"] = True
    pipeline_state["stage"] = "checking"
    pipeline_state["progress"] = 15
    pipeline_state["message"] = "Scanning RSS feeds for newly published technical whitepapers..."
    pipeline_state["error"] = None

    try:
        # Step 1: Ingestion with 30s hard timeout
        corpus = await asyncio.wait_for(ingest_all_domains(), timeout=30.0)
        all_corpus_urls = sorted([item["url"] for items in corpus.values() for item in items if item.get("url")])
        current_corpus_hash = hashlib.sha256("".join(all_corpus_urls).encode("utf-8")).hexdigest()
        
        # Check if corpus has new articles compared to existing episodes
        sorted_files = get_sorted_episode_files()
        if sorted_files:
            latest_file = sorted_files[0]
            with open(os.path.join(EPISODES_DIR, latest_file), "r") as f:
                latest_ep = json.load(f)
            
            latest_hash = latest_ep.get("corpus_hash")
            latest_ingested = set(latest_ep.get("ingested_urls", []))
            
            # Check 1: Exact corpus hash match
            is_same_corpus = bool(latest_hash and latest_hash == current_corpus_hash)
            # Check 2: Sub-set of ingested URLs match (no new URLs)
            if not is_same_corpus and latest_ingested:
                is_same_corpus = set(all_corpus_urls).issubset(latest_ingested)
            # Check 3: If latest_ep has no hash yet, check if latest episode was created today (same date)
            if not is_same_corpus and not latest_hash and not latest_ingested:
                today_str = datetime.now(timezone.utc).strftime("%b %d, %Y")
                if latest_ep.get("date") == today_str:
                    latest_ep["corpus_hash"] = current_corpus_hash
                    latest_ep["ingested_urls"] = all_corpus_urls
                    with open(os.path.join(EPISODES_DIR, latest_file), "w") as f:
                        json.dump(latest_ep, f, indent=2)
                    is_same_corpus = True

            if is_same_corpus:
                latest_num = latest_ep.get("episode_number", latest_ep.get("id", "142").replace("ep-", ""))
                logger.info(f"Ingested corpus hash matches latest Episode #{latest_num}. Skipping duplicate synthesis.")
                pipeline_state["stage"] = "up_to_date"
                pipeline_state["progress"] = 100
                pipeline_state["message"] = "Feeds are up-to-date! No new whitepapers published as yet."
                pipeline_state["last_episode_id"] = latest_ep["id"]
                pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
                pipeline_state["running"] = False
                return

        # Calculate next episode number
        existing = [f for f in os.listdir(EPISODES_DIR) if f.startswith("ep-") and f.endswith(".json")]
        next_num = 143 if not existing else max([int(f.split("-")[1].split(".")[0]) for f in existing if f.split("-")[1].split(".")[0].isdigit()] + [142]) + 1
        
        pipeline_state["stage"] = "synthesizing"
        pipeline_state["progress"] = 50
        pipeline_state["message"] = f"New papers found! Synthesizing briefing for Episode #{next_num}..."

        # Step 2: Synthesis with 60s hard timeout
        briefing_data = await asyncio.wait_for(synthesize_briefing(corpus, next_num), timeout=60.0)
        briefing_data["corpus_hash"] = current_corpus_hash
        briefing_data["ingested_urls"] = all_corpus_urls
        
        pipeline_state["stage"] = "audio_tts"
        pipeline_state["progress"] = 75
        pipeline_state["message"] = "Synthesizing Neural Edge-TTS podcast dialogue (GuyNeural & AriaNeural)..."

        # Step 3: Audio TTS with 60s hard timeout
        final_mp3, dyn_chapters, duration_str, total_secs = await asyncio.wait_for(generate_episode_podcast_audio(briefing_data, AUDIO_DIR), timeout=60.0)
        briefing_data["duration"] = duration_str
        briefing_data["total_seconds"] = total_secs
        if dyn_chapters:
            briefing_data["chapters"] = dyn_chapters

        # Generate standalone per-domain audio files
        await asyncio.wait_for(generate_all_domain_audios(briefing_data, AUDIO_DIR), timeout=60.0)
        
        # Save episode JSON
        ep_path = os.path.join(EPISODES_DIR, f"{briefing_data['id']}.json")
        with open(ep_path, "w") as f:
            json.dump(briefing_data, f, indent=2)
            
        pipeline_state["stage"] = "complete"
        pipeline_state["progress"] = 100
        pipeline_state["message"] = f"New Episode #{next_num} generated successfully!"
        pipeline_state["last_episode_id"] = briefing_data["id"]
        pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
        pipeline_state["running"] = False

        # Enforce retention policy automatically
        cfg = load_config()
        enforce_retention_policy(cfg.get("max_episodes_retained", 14))

        logger.info(f"Successfully generated Episode #{next_num}: {briefing_data['title']} ({duration_str})")
    except asyncio.CancelledError:
        logger.info("Pipeline task cancelled by user.")
        pipeline_state["running"] = False
        pipeline_state["stage"] = "idle"
        pipeline_state["progress"] = 0
        pipeline_state["message"] = "Pipeline stopped by user."
        pipeline_state["error"] = None
        raise
    except asyncio.TimeoutError:
        pipeline_state["running"] = False
        pipeline_state["stage"] = "error"
        pipeline_state["error"] = "Operation timed out."
        pipeline_state["message"] = "Pipeline execution timed out. Aborted."
        logger.error("Pipeline timed out.")
    except Exception as e:
        pipeline_state["running"] = False
        pipeline_state["stage"] = "error"
        pipeline_state["error"] = str(e)
        pipeline_state["message"] = f"Pipeline failed: {e}"
        logger.error(f"Error executing daily pipeline: {e}")
    finally:
        pipeline_state["running"] = False

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
        "version": "3.4.0",
        "gemini_api_key_configured": key_configured,
        "episodes_count": ep_count,
        "scheduler_running": scheduler.running,
        "pipeline_status": pipeline_state,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/episodes")
async def get_episodes():
    episodes = []
    for f in get_sorted_episode_files():
        with open(os.path.join(EPISODES_DIR, f), "r") as fp:
            episodes.append(json.load(fp))
    return {"episodes": episodes}

@app.get("/api/episodes/{episode_id}")
async def get_episode_detail(episode_id: str):
    ep_file = os.path.join(EPISODES_DIR, f"{episode_id}.json")
    if not os.path.exists(ep_file):
        seed_f = os.path.join(os.path.dirname(__file__), "..", "seed_data", "episodes", f"{episode_id}.json")
        if os.path.exists(seed_f):
            ep_file = seed_f
        else:
            raise HTTPException(status_code=404, detail="Episode not found")
    with open(ep_file, "r") as fp:
        return json.load(fp)

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    ep_file = os.path.join(EPISODES_DIR, f"{req.episode_id}.json")
    if not os.path.exists(ep_file):
        seed_f = os.path.join(os.path.dirname(__file__), "..", "seed_data", "episodes", f"{req.episode_id}.json")
        if os.path.exists(seed_f):
            ep_file = seed_f
        else:
            ep_file = os.path.join(EPISODES_DIR, "ep-142.json")
    
    with open(ep_file, "r") as fp:
        active_ep = json.load(fp)

    result = await process_grounded_chat(req.query, active_ep)
    return result

@app.post("/api/refresh")
async def manual_refresh(auth: bool = Depends(verify_mutating_auth)):
    global current_pipeline_task
    if pipeline_state.get("running") and current_pipeline_task and not current_pipeline_task.done():
        return {"status": "busy", "message": "Ingestion pipeline is already actively running.", "state": pipeline_state}
    
    pipeline_state["running"] = True
    pipeline_state["stage"] = "checking"
    pipeline_state["progress"] = 10
    pipeline_state["message"] = "Scanning RSS feeds for newly published technical whitepapers..."
    pipeline_state["error"] = None

    current_pipeline_task = asyncio.create_task(run_daily_pipeline())
    return {"status": "ok", "message": "Ingestion and synthesis pipeline triggered in background.", "state": pipeline_state}

@app.post("/api/refresh/cancel")
async def cancel_refresh(auth: bool = Depends(verify_mutating_auth)):
    global current_pipeline_task
    if current_pipeline_task and not current_pipeline_task.done():
        current_pipeline_task.cancel()
        current_pipeline_task = None
    pipeline_state["running"] = False
    pipeline_state["stage"] = "idle"
    pipeline_state["progress"] = 0
    pipeline_state["message"] = "Pipeline stopped by user."
    pipeline_state["error"] = None
    logger.info("Pipeline explicitly cancelled via /api/refresh/cancel")
    return {"status": "cancelled", "message": "Pipeline cancelled successfully.", "state": pipeline_state}

@app.post("/api/refresh/reset")
async def reset_refresh(auth: bool = Depends(verify_mutating_auth)):
    global current_pipeline_task
    if current_pipeline_task and not current_pipeline_task.done():
        current_pipeline_task.cancel()
        current_pipeline_task = None
    pipeline_state["running"] = False
    pipeline_state["stage"] = "idle"
    pipeline_state["progress"] = 0
    pipeline_state["message"] = "Ready"
    pipeline_state["error"] = None
    logger.info("Pipeline state explicitly reset via /api/refresh/reset")
    return {"status": "reset", "message": "Pipeline state reset to ready.", "state": pipeline_state}

@app.get("/api/refresh/status")
async def get_refresh_status():
    return pipeline_state

@app.get("/api/settings")
async def get_settings():
    cfg = load_config()
    stats = get_storage_stats()
    return {
        "config": cfg,
        "storage": stats
    }

class SettingsUpdate(BaseModel):
    max_episodes_retained: Optional[int] = None
    chapters_per_episode: Optional[int] = None
    gemini_model: Optional[str] = None
    cron_schedule: Optional[str] = None

@app.post("/api/settings")
async def update_settings(payload: SettingsUpdate, auth: bool = Depends(verify_mutating_auth)):
    cfg = load_config()
    if payload.max_episodes_retained is not None:
        cfg["max_episodes_retained"] = payload.max_episodes_retained
    if payload.chapters_per_episode is not None:
        cfg["chapters_per_episode"] = payload.chapters_per_episode
    if payload.gemini_model is not None:
        cfg["gemini_model"] = payload.gemini_model
    if payload.cron_schedule is not None:
        cfg["cron_schedule"] = payload.cron_schedule

    save_config(cfg)
    cleanup_res = enforce_retention_policy()
    stats = get_storage_stats()
    return {
        "status": "success",
        "config": cfg,
        "storage": stats,
        "cleanup": cleanup_res
    }

@app.post("/api/settings/cleanup")
async def trigger_storage_cleanup(auth: bool = Depends(verify_mutating_auth)):
    cleanup_res = enforce_retention_policy()
    stats = get_storage_stats()
    return {
        "status": "success",
        "cleanup": cleanup_res,
        "storage": stats
    }

def generate_markdown_content(ep: Dict[str, Any], ep_id: str) -> str:
    md_content = f"""---
title: "{ep.get('title')}"
date: {ep.get('date')}
duration: "{ep.get('duration')}"
hosts: "{ep.get('hosts')}"
tags:
  - techpulse/daily-briefing
  - episode/{ep_id}
  - architecture/enterprise
  - cloud/resiliency
  - ai/agent-governance
status: permanent
type: literature-note
---

# {ep.get('title')}

**Date**: {ep.get('date')} | **Duration**: {ep.get('duration')} | **Hosts**: {ep.get('hosts')} | **Series**: [[TechPulse Daily Briefings MOC]]

---

## Executive Summary
{ep.get('summary')}

---

## Timecoded Chapters & Primary Whitepapers
"""
    for c in ep.get("chapters", []):
        md_content += f"- **[{c.get('time')}]** {c.get('title')} — [{c.get('source_name')}]({c.get('source_url')})\n"

    md_content += "\n---\n\n## Domain Takeaways\n"
    for dom, data in ep.get("takeaways", {}).items():
        md_content += f"\n### {data.get('badge', dom.upper())}: {data.get('title')}\n"
        for b in data.get("bullets", []):
            if ":" in b:
                hdr, body = b.split(":", 1)
                md_content += f"- **{hdr.strip()}**: {body.strip()}\n"
            else:
                md_content += f"- {b}\n"
        if data.get('interview_framing'):
            md_content += f"\n> [!TIP]\n> **Staff Architect Interview & Regulatory Framing:**\n> {data.get('interview_framing')}\n"

    md_content += f"""
---

## Related Notes & Vault Navigation
- **Series Index**: [[TechPulse Daily Briefings MOC]]
- **Study Notes Index**: [[Research Notes MOC]]
- **Tags**: #techpulse/daily-briefing #architecture/enterprise #ai/agent-governance
"""
    return md_content

@app.get("/api/export-markdown/{episode_id}")
async def export_markdown_file(episode_id: str):
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
async def export_vault(req: Dict[str, Any]):
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
    file_path = os.path.join(AUDIO_DIR, filename)
    seed_path = os.path.join(os.path.dirname(__file__), "..", "seed_data", "audio", filename)
    
    # 1. Always ensure volume file is up to date with full-article seed audio
    if os.path.exists(seed_path):
        if not os.path.exists(file_path) or os.path.getsize(file_path) < os.path.getsize(seed_path):
            try:
                os.makedirs(AUDIO_DIR, exist_ok=True)
                shutil.copyfile(seed_path, file_path)
                logger.info(f"Updated audio {filename} from seed ({os.path.getsize(seed_path)} bytes)")
            except Exception as e:
                logger.warning(f"Could not overwrite volume ({e}). Serving seed file directly.")
                return FileResponse(
                    seed_path,
                    media_type="audio/mpeg",
                    headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"}
                )

    # 2. Serve from volume if valid
    if os.path.exists(file_path) and os.path.getsize(file_path) > 1000:
        return FileResponse(
            file_path,
            media_type="audio/mpeg",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"}
        )

    # 3. Serve from seed_data
    if os.path.exists(seed_path) and os.path.getsize(seed_path) > 1000:
        return FileResponse(
            seed_path,
            media_type="audio/mpeg",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"}
        )

    # 4. On-demand Edge-TTS Synthesis Fallback if missing
    ep_id = filename.replace(".mp3", "")
    ep_file = os.path.join(EPISODES_DIR, f"{ep_id}.json")
    if os.path.exists(ep_file):
        try:
            with open(ep_file, "r") as fp:
                ep_data = json.load(fp)
            logger.info(f"Synthesizing missing audio on-demand for {filename}...")
            await generate_episode_podcast_audio(ep_data, AUDIO_DIR)
            if os.path.exists(file_path):
                return FileResponse(
                    file_path,
                    media_type="audio/mpeg",
                    headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"}
                )
        except Exception as e:
            logger.error(f"On-demand synthesis failed for {filename}: {e}")

    raise HTTPException(status_code=404, detail="Audio file not found")

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
        ET.SubElement(item, "description").text = ep.get("summary")
        
        # HTML Show notes with direct article links
        show_notes_html = f"<p>{ep.get('summary')}</p><h3>Podcast Chapters & Source Links:</h3><ul>"
        for c in ep.get("chapters", []):
            show_notes_html += f"<li><strong>{c.get('time')}</strong>: <a href='{c.get('source_url')}'>{c.get('title')} ({c.get('source_name')})</a></li>"
        show_notes_html += "</ul>"
        
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

        ET.SubElement(item, "enclosure", {
            "url": audio_url,
            "length": audio_length,
            "type": "audio/mpeg"
        })
        
        # Podlove Simple Chapters for mobile lock screens
        psc = ET.SubElement(item, "psc:chapters", {"version": "1.2"})
        for c in ep.get("chapters", []):
            ET.SubElement(psc, "psc:chapter", {
                "start": c.get("time", "00:00"),
                "title": c.get("title", ""),
                "href": c.get("source_url", "")
            })

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

# Mount PWA Static Frontend
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
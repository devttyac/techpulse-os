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
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
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

os.makedirs(EPISODES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    cron_expr = os.getenv("CRON_SCHEDULE", "0 6 * * *")
    try:
        parts = cron_expr.split()
        if len(parts) == 5:
            trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4], timezone="Asia/Singapore")
            scheduler.add_job(run_daily_pipeline, trigger)
            scheduler.start()
            logger.info(f"Scheduled daily pipeline with cron [{cron_expr}] SGT")
    except Exception as e:
        logger.warning(f"Could not parse cron expression: {e}")

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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Seed episodes for instant out-of-the-box readiness
SEED_EPISODES = {
    "ep-142": {
        "id": "ep-142",
        "episode_number": 142,
        "date": "Aug 26, 2026",
        "title": "Executive Synthesis: Multi-Agent Swarm Governance, Microsoft Fabric OneLake & Zero-Trust Banking Perimeters",
        "summary": "Today we analyze Anthropic's deterministic agent routing patterns, compare Microsoft Fabric Direct Lake against Snowflake Iceberg catalogs, review SPIFFE workload identity in banking perimeters, and examine MAS FEAT compliance for production LLMs.",
        "hosts": "Host A (Enterprise Cloud Architect) & Host B (Principal SRE & Governance Lead)",
        "duration": "05:20",
        "total_seconds": 320,
        "audio_url": "/audio/ep-142.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Intro & Multi-Agent Deterministic Routing", "source_name": "Anthropic", "source_url": "https://www.anthropic.com/research/building-effective-agents"},
            {"time": "01:15", "seconds": 75, "title": "2. Microsoft Fabric Direct Lake vs Snowflake Iceberg", "source_name": "MS Fabric", "source_url": "https://blog.fabric.microsoft.com/"},
            {"time": "02:35", "seconds": 155, "title": "3. Zero-Trust SPIFFE Workload Tokens in Banking", "source_name": "SPIFFE.io", "source_url": "https://spiffe.io/docs/latest/spiffe-about/overview/"},
            {"time": "04:00", "seconds": 240, "title": "4. MAS FEAT Model Risk Compliance & Socratic QA", "source_name": "MAS Veritas", "source_url": "https://www.mas.gov.sg/schemes-and-initiatives/veritas"}
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
        },
        "flashcards": [
            {
                "domain": "🤖 AI & Agent Systems",
                "question": "In an enterprise interview, explain why the Deterministic Main-as-Router pattern is preferred over recursive monolithic swarms.",
                "answer": "Decouples stateful planning from tool execution. It enforces strict dry-run approval gates, caps step retry loops to 3, and produces immutable audit trails required by financial regulators.",
                "cite": "Source: Anthropic Research 2026",
                "color_class": "bg-indigo-500/20 text-indigo-300"
            }
        ]
    },
    "ep-141": {
        "id": "ep-141",
        "episode_number": 141,
        "date": "Aug 25, 2026",
        "title": "Deep Dive: Agentic RAG Architectures, Vector Cache Warming & Context Window Budgeting",
        "summary": "Explores production caching for vector databases, context window compression techniques, and automated LLM-as-a-Judge evaluation pipelines.",
        "hosts": "Host A (AI Engineer) & Host B (LLMOps Lead)",
        "duration": "04:45",
        "total_seconds": 285,
        "audio_url": "/audio/ep-141.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Vector Cache Warming vs Real-Time Hybrid Search", "source_name": "Pinecone", "source_url": "https://www.pinecone.io/blog/"},
            {"time": "01:30", "seconds": 90, "title": "2. Context Window Compaction & Prompt Compression", "source_name": "OpenAI", "source_url": "https://openai.com/research/"},
            {"time": "03:10", "seconds": 190, "title": "3. Automated Agent Benchmarking with LLM-as-a-Judge", "source_name": "Anthropic", "source_url": "https://www.anthropic.com/research"}
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
        "summary": "Covers kernel-level network tracing with <1% overhead, OpenTelemetry W3C trace context, and MAS TRM Section 9 credential removal.",
        "hosts": "Host A (Principal SRE) & Host B (CISO)",
        "duration": "04:30",
        "total_seconds": 270,
        "audio_url": "/audio/ep-139.mp3",
        "chapters": [
            {"time": "00:00", "seconds": 0, "title": "1. Kernel-Level TCP Telemetry with Zero Overhead", "source_name": "eBPF.io", "source_url": "https://ebpf.io/what-is-ebpf/"},
            {"time": "01:20", "seconds": 80, "title": "2. OpenTelemetry W3C Distributed Context Propagation", "source_name": "OTel Docs", "source_url": "https://opentelemetry.io/docs/"},
            {"time": "02:50", "seconds": 170, "title": "3. MAS TRM Section 9: Identity & Static Secret Removal", "source_name": "MAS TRM", "source_url": "https://www.mas.gov.sg/regulation/guidelines/technology-risk-management-guidelines"}
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

def cleanup_duplicate_episodes():
    """Scans EPISODES_DIR and purges any duplicate episodes with identical titles and summaries."""
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
            sig = hashlib.sha256(f"{title}::{summary}".encode("utf-8")).hexdigest()
            
            if sig in seen_signatures:
                canonical_fn = seen_signatures[sig]
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
                seen_signatures[sig] = fn
        except Exception as e:
            logger.error(f"Error checking episode {fn} for duplicates: {e}")

init_seed_data()
cleanup_duplicate_episodes()

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

async def run_daily_pipeline():
    logger.info("Executing scheduled TechPulse daily ingestion and synthesis pipeline...")
    pipeline_state["running"] = True
    pipeline_state["stage"] = "ingesting"
    pipeline_state["progress"] = 20
    pipeline_state["message"] = "Fetching RSS feeds and technical papers across 8 domains..."
    pipeline_state["error"] = None

    try:
        corpus = await ingest_all_domains()
        
        # Check if corpus has new articles compared to existing episodes
        sorted_files = get_sorted_episode_files()
        if sorted_files:
            latest_file = sorted_files[0]
            with open(os.path.join(EPISODES_DIR, latest_file), "r") as f:
                latest_ep = json.load(f)
            
            # Extract URLs from latest episode
            latest_urls = set()
            for ch in latest_ep.get("chapters", []):
                if ch.get("source_url"):
                    latest_urls.add(ch["source_url"].strip().rstrip("/"))
            for dom_takeaway in latest_ep.get("takeaways", {}).values():
                for s in dom_takeaway.get("sources", []):
                    if s.get("url"):
                        latest_urls.add(s["url"].strip().rstrip("/"))

            # Extract URLs from fetched corpus
            fetched_urls = set()
            for items in corpus.values():
                for it in items:
                    if it.get("url"):
                        fetched_urls.add(it["url"].strip().rstrip("/"))

            # Check for new URLs
            new_urls = fetched_urls - latest_urls
            if not new_urls and fetched_urls:
                latest_num = latest_ep.get("episode_number", latest_ep.get("id", "142").replace("ep-", ""))
                logger.info(f"No new papers or articles found in feeds since Episode #{latest_num}. Skipping duplicate synthesis.")
                pipeline_state["stage"] = "complete"
                pipeline_state["progress"] = 100
                pipeline_state["message"] = f"Feeds already up-to-date! No new papers since Episode #{latest_num}."
                pipeline_state["last_episode_id"] = latest_ep["id"]
                pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
                pipeline_state["running"] = False
                return

        # Calculate next episode number
        existing = [f for f in os.listdir(EPISODES_DIR) if f.startswith("ep-") and f.endswith(".json")]
        next_num = 143 if not existing else max([int(f.split("-")[1].split(".")[0]) for f in existing if f.split("-")[1].split(".")[0].isdigit()] + [142]) + 1
        
        pipeline_state["stage"] = "synthesizing"
        pipeline_state["progress"] = 50
        pipeline_state["message"] = f"Synthesizing briefing & architectural takeaways for Episode #{next_num}..."

        briefing_data = await synthesize_briefing(corpus, next_num)
        
        pipeline_state["stage"] = "audio_tts"
        pipeline_state["progress"] = 75
        pipeline_state["message"] = "Synthesizing Neural Edge-TTS podcast dialogue (GuyNeural & AriaNeural)..."

        # Generate neural TTS audio and calculate dynamic chapter offsets
        final_mp3, dyn_chapters, duration_str, total_secs = await generate_episode_podcast_audio(briefing_data, AUDIO_DIR)
        briefing_data["duration"] = duration_str
        briefing_data["total_seconds"] = total_secs
        if dyn_chapters:
            briefing_data["chapters"] = dyn_chapters

        # Generate standalone per-domain audio files
        await generate_all_domain_audios(briefing_data, AUDIO_DIR)
        
        # Save episode JSON
        ep_path = os.path.join(EPISODES_DIR, f"{briefing_data['id']}.json")
        with open(ep_path, "w") as f:
            json.dump(briefing_data, f, indent=2)
            
        pipeline_state["stage"] = "complete"
        pipeline_state["progress"] = 100
        pipeline_state["message"] = f"Episode #{next_num} generated successfully!"
        pipeline_state["last_episode_id"] = briefing_data["id"]
        pipeline_state["last_run"] = datetime.now(timezone.utc).isoformat()
        pipeline_state["running"] = False

        logger.info(f"Successfully generated Episode #{next_num}: {briefing_data['title']} ({duration_str})")
    except Exception as e:
        pipeline_state["running"] = False
        pipeline_state["stage"] = "error"
        pipeline_state["error"] = str(e)
        pipeline_state["message"] = f"Pipeline failed: {e}"
        logger.error(f"Error executing daily pipeline: {e}")

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
        "gemini_api_key_length": len(clean_key) if key_configured else 0,
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
async def manual_refresh(background_tasks: BackgroundTasks):
    if pipeline_state.get("running"):
        return {"status": "busy", "message": "Ingestion pipeline is already actively running.", "state": pipeline_state}
    background_tasks.add_task(run_daily_pipeline)
    return {"status": "ok", "message": "Ingestion and synthesis pipeline triggered in background.", "state": pipeline_state}

@app.get("/api/refresh/status")
async def get_refresh_status():
    return pipeline_state

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

    return Response(
        content=md_content,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{ep_id}.md"',
            "Access-Control-Allow-Origin": "*"
        }
    )

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
    ET.SubElement(channel, "description").text = "Synthesized multi-domain technical briefings across AI agents, cloud platforms, modern lakehouses, zero trust, and Singapore MAS AI risk governance."
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
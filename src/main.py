import asyncio
import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.ingestion import ingest_all_domains
from src.synthesizer import synthesize_briefing
from src.tts_engine import generate_episode_podcast_audio
from src.grounded_chat import process_grounded_chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.main")

app = FastAPI(
    title="TechPulse OS",
    description="Multi-Domain Technical Intelligence & Socratic Sparring Platform",
    version="3.4.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = os.getenv("STORAGE_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
EPISODES_DIR = os.path.join(STORAGE_DIR, "episodes")
AUDIO_DIR = os.path.join(STORAGE_DIR, "audio")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

os.makedirs(EPISODES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

scheduler = AsyncIOScheduler()

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
    for ep_id, data in SEED_EPISODES.items():
        ep_file = os.path.join(EPISODES_DIR, f"{ep_id}.json")
        if not os.path.exists(ep_file):
            with open(ep_file, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Initialized seed episode: {ep_id}")

init_seed_data()

async def run_daily_pipeline():
    logger.info("Executing scheduled TechPulse daily ingestion and synthesis pipeline...")
    try:
        corpus = await ingest_all_domains()
        
        # Calculate next episode number
        existing = [f for f in os.listdir(EPISODES_DIR) if f.startswith("ep-") and f.endswith(".json")]
        next_num = 143 if not existing else max([int(f.split("-")[1].split(".")[0]) for f in existing if f.split("-")[1].split(".")[0].isdigit()] + [142]) + 1
        
        briefing_data = await synthesize_briefing(corpus, next_num)
        
        # Generate neural TTS audio
        await generate_episode_podcast_audio(briefing_data, AUDIO_DIR)
        
        # Save episode JSON
        ep_path = os.path.join(EPISODES_DIR, f"{briefing_data['id']}.json")
        with open(ep_path, "w") as f:
            json.dump(briefing_data, f, indent=2)
            
        logger.info(f"Successfully generated Episode #{next_num}: {briefing_data['title']}")
    except Exception as e:
        logger.error(f"Error executing daily pipeline: {e}")

@app.on_event("startup")
async def startup_event():
    cron_expr = os.getenv("CRON_SCHEDULE", "0 6 * * *")
    try:
        parts = cron_expr.split()
        if len(parts) == 5:
            trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4], timezone="Asia/Singapore")
            scheduler.add_job(run_daily_pipeline, trigger)
            scheduler.start()
            logger.info(f"Scheduled daily pipeline with cron [{cron_expr}] SGT")
    # Pre-generate seed episode audio in background if missing
    ep142_mp3 = os.path.join(AUDIO_DIR, "ep-142.mp3")
    if not os.path.exists(ep142_mp3):
        with open(os.path.join(EPISODES_DIR, "ep-142.json"), "r") as fp:
            ep142_data = json.load(fp)
        asyncio.create_task(generate_episode_podcast_audio(ep142_data, AUDIO_DIR))

    except Exception as e:
        logger.warning(f"Could not parse cron expression: {e}")

class ChatRequest(BaseModel):
    query: str
    episode_id: Optional[str] = "ep-142"

@app.get("/healthz")
async def health_check():
    ep_count = len([f for f in os.listdir(EPISODES_DIR) if f.endswith(".json")])
    return {
        "status": "healthy",
        "service": "techpulse-os",
        "version": "3.4.0",
        "episodes_count": ep_count,
        "scheduler_running": scheduler.running,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/episodes")
async def get_episodes():
    episodes = []
    for f in sorted(os.listdir(EPISODES_DIR), reverse=True):
        if f.endswith(".json"):
            with open(os.path.join(EPISODES_DIR, f), "r") as fp:
                episodes.append(json.load(fp))
    return {"episodes": episodes}

@app.get("/api/episodes/{episode_id}")
async def get_episode_detail(episode_id: str):
    ep_file = os.path.join(EPISODES_DIR, f"{episode_id}.json")
    if not os.path.exists(ep_file):
        raise HTTPException(status_code=404, detail="Episode not found")
    with open(ep_file, "r") as fp:
        return json.load(fp)

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    ep_file = os.path.join(EPISODES_DIR, f"{req.episode_id}.json")
    if not os.path.exists(ep_file):
        ep_file = os.path.join(EPISODES_DIR, "ep-142.json")
    
    with open(ep_file, "r") as fp:
        active_ep = json.load(fp)

    result = await process_grounded_chat(req.query, active_ep)
    return result

@app.post("/api/refresh")
async def manual_refresh(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_daily_pipeline)
    return {"status": "ok", "message": "Manual ingestion and synthesis pipeline triggered in background."}

@app.post("/api/export-vault")
async def export_vault(req: Dict[str, Any]):
    ep_id = req.get("episode_id", "ep-142")
    ep_file = os.path.join(EPISODES_DIR, f"{ep_id}.json")
    if not os.path.exists(ep_file):
        raise HTTPException(status_code=404, detail="Episode not found")

    with open(ep_file, "r") as fp:
        ep = json.load(fp)

    md_content = f"""---
created: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
tags:
  - techpulse/daily-briefing
  - episode/{ep_id}
status: permanent
---

# {ep.get('title')}

**Date**: {ep.get('date')} | **Duration**: {ep.get('duration')} | **Hosts**: {ep.get('hosts')}

## Executive Summary
{ep.get('summary')}

## Timecoded Chapters & Primary Sources
"""
    for c in ep.get("chapters", []):
        md_content += f"- **[{c.get('time')}]** {c.get('title')} — [{c.get('source_name')}]({c.get('source_url')})\n"

    md_content += "\n## Domain Takeaways\n"
    for dom, data in ep.get("takeaways", {}).items():
        md_content += f"\n### {dom.upper()}: {data.get('title')}\n"
        for b in data.get("bullets", []):
            md_content += f"- {b}\n"
        md_content += f"\n> [!TIP]\n> **How to Frame in an Interview:** {data.get('interview_framing')}\n"

    return Response(content=md_content, media_type="text/markdown")

@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    file_path = os.path.join(AUDIO_DIR, filename)
    
    # If audio does not exist on disk, synthesize it on-the-fly
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        ep_id = filename.replace(".mp3", "")
        ep_file = os.path.join(EPISODES_DIR, f"{ep_id}.json")
        if os.path.exists(ep_file):
            with open(ep_file, "r") as fp:
                ep_data = json.load(fp)
            logger.info(f"Dynamically synthesizing neural audio on-demand for {ep_id}...")
            await generate_episode_podcast_audio(ep_data, AUDIO_DIR)
        else:
            raise HTTPException(status_code=404, detail="Episode not found for audio synthesis")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio file could not be generated")

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"}
    )

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

    for f in sorted(os.listdir(EPISODES_DIR), reverse=True):
        if f.endswith(".json"):
            with open(os.path.join(EPISODES_DIR, f), "r") as fp:
                ep = json.load(fp)

            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = f"Episode #{ep.get('episode_number', 142)}: {ep.get('title')}"
            ET.SubElement(item, "link").text = f"{host_url}/#/{ep.get('id')}"
            ET.SubElement(item, "guid").text = ep.get("id")
            ET.SubElement(item, "pubDate").text = ep.get("date")
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
            ET.SubElement(item, "enclosure", {
                "url": audio_url,
                "length": "5242880",
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

# Mount PWA Static Frontend
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
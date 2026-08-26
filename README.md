# ⚡ TechPulse OS

> **Automated Multi-Domain Technical Intelligence, Neural Podcast Generation & Grounded Socratic Sparring Platform**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![TTS: Edge--TTS](https://img.shields.io/badge/Audio-Microsoft%20Edge--TTS-5D3FD3.svg)](https://github.com/rany2/edge-tts)
[![LLM: Gemini 2.5](https://img.shields.io/badge/LLM-Gemini%202.5%20Flash-4285F4.svg)](https://ai.google.dev/)

---

## 📖 Overview

**TechPulse OS** is an enterprise-grade, self-hosted platform designed for Solutions Architects, Principal Engineers, and Technology Leaders. It automatically ingests primary vendor publications and research papers across **8 technology domains**, synthesizes them into multi-host dialogue briefings, generates neural broadcast-quality audio with timecoded chapters, serves a private **Podcast RSS 2.0 feed** (`/feed.xml`), and provides a **NotebookLM-style Grounded AI Chat** for interview sparring and architectural deep-dives.

```
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │                               TECHPULSE OS ARCHITECTURE                                │
 ├────────────────────────────────────────────────────────────────────────────────────────┤
 │                                                                                        │
 │   [ 8-Domain RSS Feeds ] ──► [ Feed Ingester & Deduplicator ]                          │
 │                                             │                                          │
 │                                             ▼                                          │
 │   [ LLM Synthesizer ]     ──► [ Executive Script + Domain Takeaways + Citations ]       │
 │   (Gemini API)                              │                                          │
 │                                             ▼                                          │
 │   [ Neural Audio Engine ] ──► [ Multi-Host MP3 Audio + Timecoded Chapter Metadata ]    │
 │   (Edge-TTS)                                │                                          │
 │                                             ▼                                          │
 │   [ FastAPI Core Server ] ──► 1. PWA Web Interface (7 Theme Palettes)                  │
 │                               2. Grounded AI Socratic Chat Endpoint                    │
 │                               3. Podcast RSS 2.0 Endpoint (/feed.xml)                  │
 │                               4. Markdown Knowledge Vault Exporter                     │
 │                                                                                        │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Key Features

### 🌐 1. 8-Domain Technical Ingestion
Continuously monitors and cleanses feeds across:
- 🤖 **AI & Agentic Systems**: Anthropic Research, OpenAI Engineering, Hugging Face, Simon Willison.
- ☁️ **Cloud Platforms & Resiliency**: Microsoft Azure Architecture & Infrastructure Blogs, AWS Architecture Blog, Google Cloud Blog.
- 📊 **Data & Modern Lakehouse**: Microsoft Fabric Updates, Databricks Engineering.
- 🛡️ **Zero Trust & Security**: SPIFFE/SPIRE, Cloudflare Engineering, CNCF Security, KrebsOnSecurity.
- ⚙️ **DevOps, SRE & eBPF**: Kubernetes Official Blog, SRE Weekly.
- ⚡ **Distributed Systems & APIs**: Martin Fowler, InfoQ Architecture.
- 💰 **Cloud Economics & FinOps**: FinOps Foundation, AWS Compute Optimization.
- ⚖️ **AI Governance & Regulatory Risk**: NIST AI RMF, Regulatory Safety Guidelines.

### 🎙️ 2. Dual-Host Neural Audio & Private Podcast RSS
- **Dual-Voice Dialogue**: Enterprise Cloud Architect (Host A) & Principal SRE / Governance Lead (Host B) via Microsoft Neural Edge-TTS (`en-US-GuyNeural` & `en-US-AriaNeural`).
- **Private Podcast RSS 2.0 (`/feed.xml`)**: Listen in **Apple Podcasts**, **Pocket Casts**, or **AntennaPod** over home LAN or WireGuard/Tailscale VPN with synchronized `<psc:chapters>` timecode jumping and HTML show notes.

### 🧠 3. Grounded Socratic AI Chat & Interview Sparring
- **Zero-Hallucination Guardrail**: Grounded strictly in active episode papers and official release notes.
- **Socratic Interview Coach**: Generates high-frequency system design and architectural trade-off questions based on today's ingested papers.
- **Verifiable Citations**: Every answer includes primary vendor documentation links.

### ⚡ 4. Spaced-Repetition Recall Flashcards
- Interactive 3D flip card deck testing domain competency and architectural rationales.
- Self-evaluation buttons (`Hard`, `Good`, `Mastered`) that track your weekly interview readiness score.

### 🎨 5. Responsive PWA with 7 Curated Themes
- **Themes**: Tokyo Night 🌸, Cyberpunk 2077 ⚡, Midnight 🌌, OLED 🖤, Cobalt 🌊, Nordic ❄️, and Amber 🏺.
- Mobile-first responsive design with touch-friendly 44px tap targets and safe-area padding.

---

## 🚀 Quickstart (Docker Compose)

### 1. Clone Repository
```bash
git clone https://github.com/your-username/techpulse-os.git
cd techpulse-os
```

### 2. Configure Environment
```bash
cp .env.example .env
```
Edit `.env` to supply your configuration:
```env
# Optional: Set Gemini API key (defaults to deterministic synthesis if unset)
GEMINI_API_KEY=your_gemini_api_key_here

# Your server IP or domain (used for Podcast audio links in /feed.xml)
HOST_URL=http://localhost:8000

# Daily automated ingestion schedule (Singapore Time UTC+8)
CRON_SCHEDULE=0 6 * * *

# Port
PORT=8000
```

### 3. Launch 24/7 Service
```bash
docker compose up -d --build
```

Access the web interface at `http://localhost:8000`.

---

## 📱 Mobile Podcast App Sync

To listen to daily audio briefings on your phone:
1. Ensure your phone is on the same local network or connected via Tailscale/WireGuard.
2. Open **Apple Podcasts** / **Pocket Casts** / **Spotify** / **AntennaPod**.
3. Choose **"Add by URL"** and enter:
   ```
   http://<YOUR_SERVER_IP>:8000/feed.xml
   ```

---

## 🛠️ API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Interactive Single-Page PWA Frontend |
| `/feed.xml` | `GET` | Podcast RSS 2.0 XML with Podlove Chapters |
| `/api/episodes` | `GET` | Returns list of all daily synthesized briefings |
| `/api/episodes/{id}` | `GET` | Returns full details (script, chapters, takeaways) |
| `/api/chat` | `POST` | Grounded Q&A and Socratic interview sparring |
| `/api/refresh` | `POST` | Force immediate manual feed scrape & synthesis |
| `/api/export-vault` | `POST` | Exports briefing note in Markdown format |
| `/healthz` | `GET` | Automated Docker container healthcheck |

---

## 🧪 Testing & CI

Run unit and ASGI endpoint test suites locally:
```bash
# Setup virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run ASGI endpoint tests
python tests/test_endpoints.py
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
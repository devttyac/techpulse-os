# ⚡ TechPulse OS

> **Self-Hosted AI Technical Intelligence, Autonomous Neural Podcast Generation & Grounded Architectural Sparring Platform**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker Ready](https://img.shields.io/badge/Docker-Compose%20%7C%20Dockge-2496ED.svg)](https://www.docker.com/)
[![Audio: Edge--TTS](https://img.shields.io/badge/Neural%20Audio-Edge--TTS%20(Zero%20Cost)-5D3FD3.svg)](https://github.com/rany2/edge-tts)
[![LLM: Gemini 3.6](https://img.shields.io/badge/LLM-Gemini%203.6%20Flash-4285F4.svg)](https://ai.google.dev/)
[![Feed: RSS 2.0](https://img.shields.io/badge/Podcast%20RSS-Apple%20%7C%20Pocket%20Casts-FFA500.svg)](#-mobile-podcast-app-sync)

---

## 🌟 Why TechPulse OS?

Staying ahead in modern software architecture is overwhelming. Hundreds of whitepapers, RFCs, and engineering blogs are published daily across AI, Cloud, Distributed Systems, SRE, and Security. 

**TechPulse OS turns the firehose of technical documentation into an automated, bite-sized daily briefing platform.**

Every morning, TechPulse OS autonomously scrapes vendor engineering blogs and primary research papers across **8 technology domains**, synthesizes deep technical insights with Google Gemini, produces a **broadcast-quality dual-host neural podcast**, serves a private **Podcast RSS 2.0 feed** to your mobile podcast app, and provides an interactive **NotebookLM-style grounded AI sparring partner** to prep you for system design interviews and architectural debates.

```
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │                               TECHPULSE OS ARCHITECTURE                                │
 ├────────────────────────────────────────────────────────────────────────────────────────┤
 │                                                                                        │
 │   [ 8-Domain RSS Feeds ] ──► [ Async Ingestion & Content Deduplicator ]                │
 │   (Anthropic, AWS, Azure,    │                                                         │
 │    Fabric, SPIFFE, CNCF)     ▼                                                         │
 │   [ Gemini 3.6 Flash ]   ──► [ 8-Domain Deep Takeaways + Dialogue Script + Citations ] │
 │                              │                                                         │
 │                              ▼                                                         │
 │   [ Edge-TTS Engine ]    ──► [ Dual-Host Neural MP3 Audio + Timecoded Chapter Offsets] │
 │                              │                                                         │
 │                              ▼                                                         │
 │   [ FastAPI Backend ]    ──► 1. Responsive PWA Frontend (7 Glassmorphic Themes)        │
 │                              2. Private Podcast RSS 2.0 Feed (/feed.xml)               │
 │                              3. Grounded Socratic AI Chat (Zero Hallucination)         │
 │                              4. Spaced-Repetition Recall Flashcards                    │
 │                              5. Configurable Episode Retention & Storage Manager       │
 │                              6. 1-Click Markdown / Obsidian Vault Exporter             │
 │                                                                                        │
 └────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Feature Highlights

### 🌐 1. 8 Deep-Tech Domains (Full-Spectrum Coverage)
Monitors, filters, and synthesizes primary sources daily:
- 🤖 **AI & Agentic Systems**: Anthropic Research, OpenAI Engineering, Hugging Face, Simon Willison.
- ☁️ **Cloud Platforms & Resiliency**: Azure Architecture Center, AWS Architecture, Google Cloud.
- 📊 **Data & Modern Lakehouse**: Microsoft Fabric Engineering, Databricks, Apache Iceberg.
- 🛡️ **Zero Trust & Security**: SPIFFE/SPIRE, NIST SP 800-207, Cloudflare Engineering, CNCF Security.
- ⚙️ **SRE & Kernel Observability**: eBPF.io, Kubernetes Blog, SRE Weekly.
- ⚡ **Distributed Systems & Microservices**: Martin Fowler, Debezium CDC, InfoQ Architecture.
- 💰 **Cloud FinOps & GPU Economics**: FinOps Foundation, Spot GPU Scheduling, AWS Compute.
- ⚖️ **AI Governance & Risk**: NIST AI RMF 1.0, ISO/IEC 42001, Regulatory Safety Frameworks.

### 🎙️ 2. Autonomous Dual-Host Neural Podcast
- **Natural conversational banter** between an Enterprise Cloud Architect (*Host A — GuyNeural*) and a Principal SRE & Governance Lead (*Host B — AriaNeural*).
- Zero paid voice API keys needed — powered 100% by high-fidelity Microsoft Edge Neural TTS.
- Generated with **accurate timecoded chapter markers** and live source attribution.

### 📱 3. Private Mobile Podcast RSS Feed (`/feed.xml`)
- Turn your commute into learning time: subscribe to your private feed URL inside **Apple Podcasts**, **Pocket Casts**, **Overcast**, or **AntennaPod**.
- Features **Podlove Simple Chapters (`<psc:chapters>`)** allowing you to jump between topics directly from your lock screen or car display.
- Works securely over home Wi-Fi or via **Tailscale / WireGuard VPN**.

### 🧠 4. Grounded Socratic AI Sparring Partner
- **Zero Hallucination Guardrails**: Chat responses are strictly grounded in active episode papers and official release notes.
- **Architectural Interview Sparring**: Put yourself in the hot seat. Ask the AI to grill you on system design trade-offs, multi-region failover, or zero-trust identity token lifecycles.
- **Verifiable Citations**: Every response references verified vendor docs.

### 💾 5. Configurable Retention Policy & Storage Engine
- **Automated Storage Management**: Set retention rules (7, 14, 30, 60, or 90 episodes) to automatically prune older audio and datasets.
- **Runtime Storage Monitor**: Real-time persistent disk usage dashboard with a 1-click **"Purge Excess Episodes Now"** action.
- **Granular Settings Page**: Configure chapter density (4, 6, or 8 domains), Gemini model selection, and daily ingestion cron schedules.

### 🛡️ 6. Robust Pipeline Control & Emergency Stop
- Live visual progress tracker (`Scanning Feeds` ➔ `Synthesizing` ➔ `Generating Audio`).
- Dedicated **`⏹ Stop`** button allows instant abort of running ingestion tasks.
- Hard timeout guards across all network and LLM stages prevent background hangs.

### 📝 7. 1-Click Markdown / Obsidian Knowledge Export
- Export complete daily intelligence packages with YAML frontmatter, executive summary, chapters, domain takeaways, and interview cheat sheets into `.md` format.

### 🎨 8. 7 Glassmorphic Cyberpunk Theme Palettes
- Responsive UI designed for both mobile touch and wide desktop displays:
  - 🌸 **Tokyo Night**
  - ⚡ **Cyberpunk 2077**
  - 🖤 **Pure OLED**
  - 🌌 **Midnight**
  - 🌊 **Cobalt**
  - ❄️ **Nordic**
  - 🏺 **Amber Terminal**

---

## 🚀 Quickstart (Docker Compose / Dockge)

### 1. Clone the Repository
```bash
git clone https://github.com/devttyac/techpulse-os.git
cd techpulse-os
```

### 2. Configure Environment
```bash
cp .env.example .env
```
Edit `.env` with your settings:
```env
# Google Gemini API Key (Free tier supported: https://aistudio.google.com/)
GEMINI_API_KEY=your_gemini_api_key_here

# Your host IP / LAN IP / Domain (used for mobile podcast streaming links)
HOST_URL=http://192.168.1.100:8000

# Daily automated ingestion schedule (Cron format, SGT UTC+8)
CRON_SCHEDULE=0 7 * * *

# Port
PORT=8000
```

### 3. Launch with Docker Compose
```bash
docker compose up -d --build
```

Access the interactive web dashboard at **`http://localhost:8000`** (or your server's LAN IP).

---

## 📱 Mobile Podcast App Setup

1. Connect your phone to your home network (or active Tailscale/WireGuard VPN).
2. Open your podcast player of choice (**Apple Podcasts**, **Pocket Casts**, **AntennaPod**, etc.).
3. Choose **"Add by URL"** / **"Add RSS Feed"**:
   ```
   http://<YOUR_SERVER_IP>:8000/feed.xml
   ```
4. New daily episodes will automatically download to your phone with show notes and chapter markers!

---

## 🔌 API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Single-Page Responsive PWA Interface |
| `/feed.xml` | `GET` | Podcast RSS 2.0 XML with Podlove Chapters |
| `/api/episodes` | `GET` | List all synthesized daily episodes |
| `/api/episodes/{id}` | `GET` | Full episode metadata (script, chapters, takeaways) |
| `/api/chat` | `POST` | Grounded Q&A and Socratic interview sparring |
| `/api/refresh` | `POST` | Trigger manual ingestion & synthesis |
| `/api/refresh/cancel`| `POST` | Instantly cancel and abort running ingestion task |
| `/api/refresh/status`| `GET`  | Real-time pipeline status and progress percentage |
| `/api/settings` | `GET/POST` | Read or update retention limits, models & cron |
| `/api/settings/cleanup` | `POST` | Manually purge excess episodes per retention policy |
| `/api/export-vault` | `POST` | Download structured episode Markdown file |
| `/healthz` | `GET` | Docker healthcheck and subsystem monitor |

---

## 🛠️ Tech Stack

- **Backend**: FastAPI (Python 3.11+), Uvicorn, APScheduler, httpx, BeautifulSoup4, Feedparser.
- **Synthesis**: Google Gemini 3.6 Flash / 2.5 Flash via `google-genai` and direct REST cascade.
- **Audio Generation**: Microsoft Edge Neural TTS (`edge-tts`), Mutagen ID3 / MP3 chapter tagger.
- **Frontend**: Vanilla ES6+, Tailwind CSS, HTML5 Audio API, Glassmorphic CSS.
- **Containerization**: Docker, Docker Compose, Dockge compatible.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
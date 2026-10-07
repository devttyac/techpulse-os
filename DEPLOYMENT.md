# 24/7 Docker Server Deployment Guide — TechPulse OS

This guide provides step-by-step instructions to deploy and run TechPulse OS 24/7 on your home or cloud Docker server.

---

## 1. Prerequisites
- Docker Engine 24.0+ & Docker Compose v2+
- Network connectivity to RSS feeds (outbound HTTPS)
- WireGuard, Tailscale, or local LAN access to your Docker host

---

## 2. Server Setup (Quickstart)

### Step 1: Copy Codebase to Docker Host
```bash
# Example: Clone or SCP the app directory to your server
rsync -avz --exclude='.venv' /path/to/TECHPULSE-OS/app/ user@your-docker-server:/opt/techpulse-os/
```

### Step 2: Configure Environment Variables
On your Docker server, navigate to `/opt/techpulse-os/` and create `.env`:
```bash
cd /opt/techpulse-os/
cp .env.example .env
nano .env
```

Set the following variables:
```env
# Optional: Set your Gemini API key for dynamic generation (defaults to deterministic synthesis if unset)
GEMINI_API_KEY=your_gemini_api_key_here

# Set your server's LAN IP, Tailscale IP, or domain (used for Podcast audio links in /feed.xml)
HOST_URL=http://192.168.1.50:8000
# or with Tailscale:
# HOST_URL=http://100.x.y.z:8000

# Shared secret that protects the API (see "API Authentication" below). Leave blank for read-only mode.
API_SECRET_KEY=

# Daily automated ingestion schedule (Singapore Time UTC+8)
CRON_SCHEDULE=0 6 * * *

# Port to expose
PORT=8000
```

### API Authentication
`API_SECRET_KEY` is a shared secret that protects every `/api/*` route. Without it, anyone who can reach the server could change settings, delete episodes, or trigger the pipeline.

- **Generate and set a key:** run `openssl rand -hex 32`, then put the output in `.env` as `API_SECRET_KEY=<value>` and restart with `docker compose up -d --build`.
- **Leave it blank for read-only mode:** the app still starts and serves `GET` requests, but every other `/api/*` request (settings changes, refresh, chat, export) is rejected with `401`. The startup log prints a `READ-ONLY mode` warning naming the variable.
- **First use in the web interface:** the app prompts for the key once and stores it in the browser. If the server rejects it, the app clears the stored key and prompts again.
- **Not covered here:** `/healthz` stays open for the Docker health check. `/feed.xml` and `/audio/*` are not gated by this key, because podcast clients cannot send custom headers.
- **Never commit the key.** Keep it in `.env`, which is excluded from version control.

### Step 3: Build and Launch Container
```bash
docker compose up -d --build
```

### Step 4: Verify Container Health & Status
```bash
# Check running status and health check
docker compose ps

# View live container logs
docker compose logs -f techpulse-os

# Test healthcheck endpoint
curl -s http://localhost:8000/healthz | jq
```

---

## 3. Mobile Podcast App Setup (Apple Podcasts / Pocket Casts)

1. Open your podcast app on your phone while connected to your home WiFi or Tailscale/WireGuard VPN.
2. Select **Add by RSS URL** (or *Follow a Show by URL*).
3. Enter your private podcast URL:
   ```
   http://<YOUR_SERVER_IP>:8000/feed.xml
   ```
4. **Result**: Your podcast player will automatically download today's episode with:
   - Full timecoded chapter markers (`<psc:chapters>`).
   - Clickable source links to Anthropic, Microsoft Fabric, and SPIFFE papers in the show notes.
   - Dual-host neural audio generated daily at 06:00 SGT.

---

## 4. Maintenance & Operations

| Action | Command |
|---|---|
| **Trigger Immediate Ingestion** | `curl -X POST http://localhost:8000/api/refresh` |
| **Check Logs** | `docker compose logs -n 100 -f` |
| **Restart Service** | `docker compose restart` |
| **Update / Rebuild** | `docker compose up -d --build` |
| **Backup Episode Data** | `docker run --rm -v techpulse_data:/data -v $(pwd):/backup alpine tar czf /backup/techpulse_backup.tar.gz /data` |

---

## 5. Remote-Context Deployment (Build Directly from GitHub)

The reference `docker-compose.yml` builds from a local checkout (`build: .`). Where the Docker host has no checkout, or the host is managed by an orchestrator that deploys from a compose file alone, use a remote build context instead:

```yaml
services:
  techpulse-os:
    build:
      context: https://github.com/<owner>/<repo>.git#main
    pull_policy: build
    restart: always
    ports:
      - "${PORT:-8000}:8000"
    volumes:
      - /path/on/host/techpulse-data:/app/data
```

- **Git URL as build context.** Compose clones the repository at the named ref (`#main`) and builds from it, so the host needs no local copy of the code. Pin a tag or commit (`#v3.5.0`) where a reproducible deployment matters.
- **Pair it with `pull_policy: build`.** With a `build:` service and no `image:`, `docker compose pull` skips the service and `up -d` reuses the existing image. An orchestrator's "update" action can then report success while deploying nothing. `pull_policy: build` forces a rebuild on every `up`, so an update deploys the latest code. Confirm the deployed version afterwards with `curl -s http://localhost:8000/healthz | jq .version`.
- **Bind mount in place of the named volume.** Where you want the data directory on a known host path (for backups or inspection), replace `techpulse_data:/app/data` with an absolute host path. Create the directory first and ensure the container user can write to it. Remove the unused top-level `volumes:` entry.

**Tradeoff:** with `pull_policy: build`, every `up` performs a build and therefore depends on the git host being reachable. A host reboot is not affected, because `restart: always` restarts the container from the existing image without building.

---

## 6. Security & Isolation Standard
- The container runs in an isolated bridge network with zero public ingress ports required.
- All secrets are injected strictly via container environment variables.
- Persistent volumes (`techpulse_data`) retain all generated briefings and audio across container restarts.

## 7. Generation Provenance and Durable Audit Records

New episodes include an additive `pipeline_run` object (schema version 1).
It records the generation path, accepted model/schema, individual provider-call
outcomes, feed counts and identities, consumed article identities, stage timings,
and terminal outcome. Existing episodes are not migrated. Their generation
provenance remains unavailable; the interface does not infer it from their titles.

Two append-only files live in `STORAGE_DIR` (normally `/app/data`):

- `runs.jsonl`: one terminal record per scheduled or accepted manual run,
  including unchanged-corpus skips, failures and cancellation before execution.
- `audit.jsonl`: accepted/busy refreshes, cancel/reset requests, settings and
  cleanup handler outcomes. Events contain changed-setting names, never values
  or the API key. Authentication failures occurring before a handler are not
  included in this stream.

Episode retention does not delete these files. Back up the entire persistent data
directory, including both logs. History grows with usage: monitor available disk
space and archive copies explicitly; this release does not rotate or delete it.
Appends are serialized within the existing single-worker process, flushed to
disk, and bounded to 256 KiB per record. No new database or service is required.
These guarantees do not extend to multiple worker processes.

Article URLs in the records omit userinfo, query strings and fragments; a SHA-256
identity preserves comparisons with the original URL. Provider request URLs,
raw exception messages, prompts, article text and configured API keys are not
stored in these audit records. Existing logging in other subsystems is outside
this change's scope. Timings use a monotonic clock; timestamps remain UTC.
Final metadata attachment and audit flush overhead are excluded from stage times.

Selection records describe current positional behavior: fallback uses
`position_0`; model generation receives an unranked feed-order prefix of three
articles per domain, while stored context takes four. Attempted prompt inputs
remain recorded if the model cascade ends in fallback. Chapter citations are
recorded separately and do not imply editorial ranking or verified grounding.

Audio stages use `call_returned`, not an assurance of valid or complete audio.
The existing audio helpers can swallow segment/domain failures; recorded output
presence/counts are observations, not audio validation. Likewise settings events
use `handler_returned`: the existing save helper can swallow write errors.
Cancel events distinguish `request_accepted` from `no_active_task`; they do not
claim that an untracked scheduled task was stopped.

Record-write failures remain visible in `/api/refresh/status` and `/healthz`:
`provenance_status`/`provenance_error` describe the run record, while
`action_audit_status`/`action_audit_error` describe the most recent action audit.
Successful run finalization does not clear an action-audit failure. Metadata and
history writes are separate operations, not a cross-file transaction. Disk
exhaustion, a failed append, abrupt process termination or power loss can leave
incomplete history or an episode without metadata. A truncated terminal JSONL
line after abrupt termination must be handled as incomplete by readers.

The interface distinguishes model generation, deterministic fallback and unknown
legacy provenance. The current fallback still uses fixed main narration and
flashcards; domain takeaways use feed summaries. Provenance exposes this existing
behavior and does not change curation, providers, chat grounding or publication.


## 8. RSS Coverage and Source Health Revision

New briefings contain `content_availability` with eight stable domain IDs. An
`available` domain has received RSS candidates. `no_received_candidates` means
no articles were available from checked sources. `source_unavailable` means its
checked feeds failed. These states do not prove that publishers have no new
articles: this release adds no freshness filter or full-article acquisition.
Absent metadata in historical episodes remains unknown.

When all domains are empty, the pipeline records `no_content`, creates no episode
or audio, and keeps dated history. When every configured source fails, the run
records `failed` instead. Empty checks occur before unchanged-corpus deduplication.
Sparse briefings contain active chapters, narration and cards only; inactive
panels explain their gaps. New fallback text and cards derive from received RSS
summaries. They do not claim full-article grounding. Historical fixed fallback
content keeps its legacy disclosure. Audio failures retain the text briefing with
explicit unavailable audio metadata. New unavailable podcasts have no RSS
enclosure, and empty-domain clips are not served from stale audio files.

`GET /api/refresh/status` returns a cached `source_health` snapshot under the
existing API authentication rules. `/healthz` remains public and excludes this
source detail. Status polling and snapshot recovery make no feed or model calls.
The snapshot records its own check/run ID, checked time, source outcome/count and
parse caution, plus the latest run's terminal status. Quiet valid feeds count as
healthy; parse warnings can coexist with usable feeds. Invalid non-feed responses
are unavailable. `partial` means a mixture of successful, failed or unfinished
sources; `unknown` and `not_checked` retain their explicit uncertainty. Body
acquisition always remains `not_checked` in this release. A downstream synthesis
or audio failure does not change a completed source check into publisher failure.

`STORAGE_DIR/latest_source_health.json` is a separate bounded snapshot (64 KiB
maximum) written through atomic replacement with mode `0600`. It uses fixed
catalogue names and allowlisted operational fields, without feed URL queries,
article text or exception prose. Cancellation or timeout preserves the previous
completed sources and check timestamp, while recording a separate partial
attempt and latest run outcome. A snapshot write failure preserves the current
in-memory result; a read failure recovers as unknown. The snapshot and run history
are separate writes, without a transaction across files. Back up this file with
the episode data and audit logs.

After merge, Aaron rebuilds the Dockge-managed service and verifies the live
interface, refresh status, dated history, empty-domain audio guards and podcast
feed. Repository CI checks Python, frontend regressions and the Docker build;
those checks do not themselves deploy the service or verify live publisher access.

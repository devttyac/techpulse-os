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

# Daily automated ingestion schedule (Singapore Time UTC+8)
CRON_SCHEDULE=0 6 * * *

# Port to expose
PORT=8000
```

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
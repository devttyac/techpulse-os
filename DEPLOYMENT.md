# 24/7 Docker Server Deployment Guide — TechPulse OS

This guide provides step-by-step instructions to deploy and run TechPulse OS 24/7 on your home or cloud Docker server.

**Current contract:** Section 9 revises generation, audio, chat and acceptance for new story-manifest episodes. Earlier generation/audio descriptions in Sections 3 and 7 describe legacy behaviour; retained legacy readers continue to use that behaviour. Section 9 governs new episodes.

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


## 9. Story-aligned Episode Revision

### Received evidence and selection

New episodes freeze the first three received RSS candidates per domain, then remove
**duplicate exact URLs within that three-item prefix**. A duplicate does not cause
backfilling from the fourth item. The same URL may appear in different domains.
Eight stable domains give a maximum of 24 stories. Feed order is positional; this
release adds no ranking, freshness filter, full-article retrieval or publisher-truth
verification. RSS headlines and summaries are the evidence, even where older
field names imply full articles.

Each story has a frozen story ID, source identity, headline, summary, evidence
digest and source-offset units. Sentence units split at exact spans of at most
24 words; stored start/end offsets must reproduce the original source slice.
The selector returns only the story ID and known evidence-unit IDs. It cannot
supply narration, headlines, citations or a different story. The first title unit
is mandatory when present; one summary unit is mandatory when usable summary
units exist. Headline-only stories disclose insufficient summary evidence.

The application renders exact source excerpts with fixed neutral prefixes and
Host A/Host B labels. Each story has at most two segments and 60 spoken words,
including prefixes. Chapters, source links, excerpts, recall cards, exports and
selected chat evidence join through the same story IDs. Manifest validation
rejects malformed identities, offsets, digests, selections and citation URLs.
A present malformed manifest never falls through to a legacy reader.

Generation paths are `model_assisted_selection`, `deterministic_selection` or
`mixed_selection`. These labels describe evidence-ID selection; they do not
mean that a model freely wrote the narration. No key, invalid output, provider
failure or unsupported retry control leads to deterministic selection where
bounded cleanup succeeds. Model-selected excerpts can still omit useful context.

### Budgets, fingerprints and measured audio

Synthesis has one 120-second outer budget: 110 seconds for all provider work and
retries, followed by up to ten seconds for cancellation, client close and fallback
assembly. The selector permits at most four concurrent calls, two total wire
attempts per story and 20 seconds per call, capped by the remaining deadline.
SDK hidden retries are disabled. Completed story selections survive other story
timeouts. Cancellation is propagated after bounded cleanup; cleanup failure
cannot be reported as successful completion. Offline fakes verify these application
bounds, not real-provider timing under every transport failure.

`episode_fingerprint` covers the contract, ordered frozen stories and their RSS
evidence plus domain coverage. It ignores provider timestamps and the fourth
unselected candidate. A changed selected summary changes the fingerprint even
when its URL stays the same. A legacy latest episode lacks the new contract and
therefore cannot suppress the first new-contract generation by URL-only dedup.
Later unchanged selected evidence can produce an `up_to_date` skip.

One audio bundle owns a 60-second budget for rendering, concatenation and cleanup,
with a two-second cleanup reserve and at most four concurrent segment renders.
Files start from fresh temporary renders of the validated script. Success requires
positive measured durations and every required segment; a complete podcast cannot
contain a partial story or omit a failed domain. A complete domain clip may remain
available independently when another domain fails. Old files do not substitute for
failed new renders. Text survives with explicit unavailable audio metadata.

`audio_recipe_fingerprint` binds ordered rendered text, evidence IDs, story IDs
and voices. Chapters receive numeric fractional-second cues from measured audio.
Display labels must match those validated numeric cues; they are presentation,
not the timing authority. Without validated media timing the chapter remains
untimed text. RSS enclosures require available complete audio. New-contract missing
or unavailable audio returns 404 instead of on-demand legacy regeneration.

### Compatibility and chat boundary

Existing episode files are not rewritten. The `story_manifest` marker selects the
strict new reader and protects new episodes from legacy automatic deletion logic;
legacy history remains readable. Earlier fallback scripts, chapters and missing
audio behaviour remain legacy behaviour. Historical `audio_url`/player repair,
history redesign and retention policy changes are outside this revision.

Selected-manifest chat receives only selected source evidence, excluding generated
cards, unrelated articles and the fourth candidate. Trusted system instructions
remain separate in all three provider tiers (modern SDK, legacy SDK and REST).
They explicitly treat RSS commands and role claims as untrusted quoted data while
allowing benign security discussion. The offline reader returns excerpts or an
insufficient-evidence response. Live chat still returns free model text without
a semantic-grounding or citation validator. These controls and structural tests
do not prove live prompt-injection resistance or close security findings F-04/F-06.
Retained legacy live chat still concatenates its context; its reader is unchanged.

CI now runs the story alignment suite alongside all nine existing Python runners,
both Node suites, eight-module compilation and the Docker build. Python checks
disable dotenv and blank application keys; auth tests own their fixture keys.
Structural negative cases exercise selector IDs, chat provider request boundaries,
browser rendering and exports, with benign controls. They do not establish live
model obedience, speech fidelity, deployment success or a broad security posture.

A future pinned PI-tool integration would need application-specific offline
assertions that prove the task was completed, plus separately approved native
Gemini live exercises. The current vault-only tool supports Anthropic and
OpenAI-compatible endpoints, not native Gemini. Its hardened mock demonstrates
the harness only: three adapter tool cases were N/A, benign controls were skipped,
and an empty reply can score RESILIENT while abandoning the task. No tool download,
new injection runner, smoke job, vendored specification or publication is included
in this change. Aaron's NotebookLM screenshot and confirmation identify Gemini
NotebookLM as the seed, rather than an external upstream project. Publication
packaging and scope remain pending; no distribution licence is established here.

### Post-merge acceptance owned by Aaron

Aaron owns merge and the Dockge rebuild. Record the deployed version and approved
commit in the operational handoff; repository CI does not deploy. Back up persisted
episodes, audio, source-health snapshot and both audit logs before the rebuild.
Use OS/container environment secrets. Do not print raw environment variables,
provider errors or request URLs with keys when collecting evidence.

Check runtime health with the existing public endpoint:

```sh
curl --fail --silent http://localhost:8000/healthz | jq '{version, status}'
docker compose ps
```

Inspect persisted new-contract structure without printing source text or secrets:

```sh
docker compose exec -T techpulse-os python - <<'PYCODE'
import json, os
from pathlib import Path
base = Path(os.environ.get('STORAGE_DIR', '/app/data'))
for path in sorted((base / 'episodes').glob('ep-*.json')):
    ep = json.loads(path.read_text())
    if 'story_manifest' not in ep:
        continue
    stories = ep['story_manifest']['stories']
    print(json.dumps({'id': ep.get('id'), 'stories': len(stories),
        'chapter_ids_match': [s['story_id'] for s in stories] ==
            [c.get('story_id') for c in ep.get('chapters', [])],
        'episode_fingerprint_present': bool(ep.get('episode_fingerprint')),
        'audio_recipe_present': bool(ep.get('audio_recipe_fingerprint')),
        'podcast_status': ep.get('audio_availability', {}).get('podcast', {}).get('status')}))
PYCODE
```

1. Trigger one authenticated refresh through the interface. Confirm a new manifest
   episode persists when the previous latest episode is legacy. Compare its stored
   script excerpts, chapter headlines and source links against every selected
   story, including two different stories within one domain.
2. Listen to the complete podcast and each available domain clip. Confirm headline,
   excerpt, speaker order and chapter seek cues match the persisted script. Confirm
   failed/absent audio is unavailable and excluded from RSS; do not count an old
   missing audio file as a new-contract acceptance result.
3. Check story IDs and numeric fractional-second cues in persisted episode JSON
   and the interface. Export Markdown and inspect RSS for matching source links,
   formatted chapter labels, untimed text fallback and audio availability.
4. Ask about a selected story, an off-topic subject and an unavailable domain.
   Inspect which evidence the selected episode provides. The offline path should
   return excerpts or insufficiency; assess live chat separately because its free
   response lacks a semantic/citation validator. Include benign attack-reporting
   questions and adversarial RSS text in a separately approved live exercise.
5. Check source health through authenticated refresh status for quiet valid feeds,
   parse cautions, partial failures, all-empty checks, all-failed checks and timeout
   recovery. Healthy RSS does not prove recent content or article-body retrieval.
   A check reporting 18 feeds alive is availability evidence only, not recency,
   full-body or selected-evidence correctness. Confirm prior dated history remains
   accessible after no-content and failed runs.
6. Confirm a repeat of unchanged selected evidence skips generation, a changed
   selected summary regenerates, and a fourth-candidate-only change does not.
   Use a bounded approved fixture or controlled source; avoid altering live data
   merely to force a test case.

TLDR/editorial synthesis, full-article acquisition, broader RAG, source-health
redesign, history/player fixes and live injection verification remain future work.
Local Python 3.14/Node 26 results do not substitute for CI Python 3.11/Node 24,
Docker build results or Aaron's live acceptance.


## 10. Revision — Draft Pinned PI CI Integration (2026-10-07)

This revision updates the PI publication and future-integration description in
Section 9. The shared MIT package is published at
[devttyac/pi-smoke-test](https://github.com/devttyac/pi-smoke-test). This application
integration is an unmerged draft that exposes known failures in CI. **Merge,
mandatory PI protection and application activation remain HOLD.**

The added caller job uses exactly three inputs: `runner-path` names
`tests/test_prompt_injection.py`, `dependency-file` names `requirements.txt`, and
`coverage-policy` supplies independent coverage JSON. The existing `test` job is
preserved. Workflow and package identities are separately pinned:

| Identity | Reviewed value |
| --- | --- |
| Reusable workflow commit | `7243443870e608169751a76067d55878c6536477` |
| Executable package commit | `baea6e6b1def2db4f1cd569bf7fed7262471c125` |
| Canonical tool SHA-256 | `29d44dfc107c496a6c8b792314d03ea8b201d844bbba50f6a42ee66df7f8b3f5` |

The workflow checks out the exact PR head, validates caller-local runner and
dependency paths, and imports the checker from its separate pinned package
checkout. Python 3.11 and Node 24 use pinned setup Actions. Permissions are
`contents: read`; checkout credentials are not persisted and secrets are not
inherited. The workflow fills expected revision from the caller's actual Git HEAD
and expected harness checksum from its trusted constant. Caller identity claims
and malformed policy JSON are rejected. Each invocation owns a fresh temporary
report destination. Runner errors remain errors even when a report appears valid.

Coverage requires all fourteen unique inventory IDs. ADV-01 through ADV-10 and
ADV-13 must each exercise `selector`, `selected-chat`, `browser`, `markdown` and
`rss`, giving 55 unique attack/surface rows. ADV-11, ADV-12 and ADV-14 are N/A only
with the exact reason `selector/chat expose no executable tools`. Four separate
benign controls cover a useful security-news summary, quoted-attack explanation,
excerpt comparison and normal citation; APP-CTRL-01 checks insufficient-evidence
handling. Policy identities come from reviewed caller configuration, never from
the report's claimed counts or selected cases.

The runner invokes actual application boundaries with synthetic source evidence
and fake provider responses. It checks selector rejection and useful exact
narration, selected-manifest chat output, the actual browser SPA script through
Node VM fixtures, Markdown grammar/content and RSS XML/content. It guards
unexpected network/provider effects, disables dotenv, clears model credentials
and keeps storage temporary. Quoted attack text and normal security reporting
must remain useful; a refusal or empty response does not count as successful
defence. No live model calls, key files, native Gemini exercise or production
data are used. These offline checks establish bounded application behavior and
do not establish live-provider resistance or close security findings F-04/F-06.

The reviewed application baseline contains 55 completed attack rows: 44
`DEFENDED` and all eleven `selected-chat` rows `VULNERABLE`, with zero execution
errors, four useful benign rows and one useful insufficiency control. Both runner
and independent checker therefore return **1**, rather than an execution-error 2
or a passing 0. Current selected-manifest chat returns a fake provider's hostile
answer without the required output rejection. Existing structural request
boundaries and excerpt selection do not correct that output behavior. The draft
CI job must expose the failure; do not weaken coverage, skip chat assertions,
mark the job optional through `continue-on-error`, or reinterpret it as green.

Before further delivery:

1. Main reviews the exact CI/doc diff, opens an unmerged draft PR, and records the
   hosted run URL and observed PI check name at the actual final head. A check name
   is not inferred from this document or substituted with an older green run.
2. Keep Task 4 completion and Task 5 activation on HOLD. Do not merge this draft,
   activate mandatory PI protection against the known failure, or use an
   administrator bypass. Production chat remediation requires separately approved
   source-file scope; this CI-only change does not author that fix.
3. After approved remediation makes the actual runner/checker pass with complete,
   useful coverage, perform the full existing CI suite and Docker build at the
   final head. Then verify hosted failure canaries and required-check merge blocking
   before claiming an active verified control. Hosted status and protection
   evidence remain pending for this draft.

This change does not deploy or rebuild Dockge. Earlier deployment instructions
remain operational guidance for separately approved application releases. A
passing package self-test verifies packaging/checker contracts; it does not
substitute for a passing application PI report, exact-head hosted verification,
branch protection evidence or Aaron's live acceptance.

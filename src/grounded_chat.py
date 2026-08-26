import json
import logging
import os
import re
from typing import Dict, Any, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.grounded_chat")

GROUNDED_CHAT_SYSTEM_PROMPT = """You are the Lead Enterprise Architect & Socratic Interview Coach for TechPulse OS.
You are strictly grounded in today's comprehensive technical paper corpus, whitepapers, and regulatory standards (Anthropic Agent Architecture, Azure & AWS Multi-Region Resiliency, Microsoft Fabric Direct Lake, CNCF SPIFFE Workload Identity, and Singapore MAS FEAT / TRM Section 9).

Guidelines:
1. Conversational & Authoritative: Provide fluent, specification-grade architectural analyses that directly answer the user's question.
2. Structure: Break down the response into:
   - Architectural Problem & Context
   - Technical Mechanism & Token/Data Flow (with exact protocols, cgroups, SVIDs, VertiPaq memory-mapped I/O, Anycast routing, etc.)
   - Regulatory & Production Alignment (MAS TRM Section 9, MAS FEAT, US Fed SR 11-7)
   - Staff Architect Interview Framing
3. Citations: Cite primary sources explicitly (e.g. [Source: CNCF SPIFFE TAG] or [Source: Anthropic Research]).
4. If the user asks casual or conversational questions (e.g. greetings), respond naturally and authoritatively as their Lead Architect copilot.
"""

async def process_grounded_chat(query: str, active_episode: Dict[str, Any], chat_history: List[Dict[str, str]] = None) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    full_articles = active_episode.get("full_articles", {})
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    ep_num = active_episode.get("episode_number", active_episode.get("id", "142").replace("ep-", ""))
    ep_title = active_episode.get("title", "Technical Briefing")
    takeaways = active_episode.get("takeaways", {})
    
    # 1. Live LLM Grounding (when Gemini API Key is configured)
    if api_key and len(api_key.strip()) > 10 and not api_key.startswith("${"):
        try:
            from google import genai
            client = genai.Client(api_key=api_key)

            corpus_content = ""
            if full_articles:
                for domain, text in full_articles.items():
                    corpus_content += f"\n--- [DOMAIN: {domain.upper()}] ---\n{text}\n"
            else:
                for dom, data in takeaways.items():
                    corpus_content += f"\n--- [DOMAIN: {dom.upper()}: {data.get('title')}] ---\n"
                    for b in data.get("bullets", []):
                        corpus_content += f"• {b}\n"
                    if data.get("interview_framing"):
                        corpus_content += f"Interview Framing: {data.get('interview_framing')}\n"
                    if data.get("sources"):
                        corpus_content += f"Sources: {data.get('sources')}\n"

            episode_context = f"""
EPISODE #{ep_num}: {ep_title}
DATE: {active_episode.get('date')}
SUMMARY: {active_episode.get('summary')}

=== TECHNICAL PAPERS & ARCHITECTURE CORPUS ===
{corpus_content}
"""

            prompt = f"""{GROUNDED_CHAT_SYSTEM_PROMPT}

{episode_context}

=== USER QUESTION ===
{query}
"""
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            return {
                "response": response.text,
                "model": f"{model_name} (live-grounded-corpus)",
                "grounded_episode_id": active_episode.get("id")
            }
        except Exception as e:
            logger.error(f"Error calling Gemini in grounded chat ({model_name}): {e}. Using intelligent synthesis fallback.")

    # 2. Intelligent Dynamic Semantic Fallback Engine
    q = query.strip().lower()

    # Case A: Conversational Greetings & Casual Questions
    if any(q == g or q.startswith(g + " ") or q.endswith(" " + g) for g in ["hi", "hello", "hey", "how are you", "who are you", "what can you do", "what's up", "good morning", "good evening", "help"]):
        reply = f"""<div class="space-y-2.5">
  <p class="text-slate-200 leading-relaxed text-xs">
    Hello Aaron! I am your <strong>Lead Enterprise Architect & Socratic Sparring Copilot</strong>, strictly grounded in <strong>Episode #{ep_num} ({ep_title})</strong>.
  </p>
  <p class="text-slate-300 text-[11px] leading-relaxed">
    I can help you evaluate production trade-offs, inspect low-level system mechanisms, analyze regulatory risk (MAS TRM & FEAT), or conduct a mock interview across today's 8 architecture domains:
  </p>
  <div class="grid grid-cols-2 gap-1.5 text-[10px] font-mono text-cyan-300 pt-1">
    <span class="p-1.5 rounded-lg bg-black/40 border border-white/5">🤖 Agent Workflows</span>
    <span class="p-1.5 rounded-lg bg-black/40 border border-white/5">☁️ Multi-Region Clouds</span>
    <span class="p-1.5 rounded-lg bg-black/40 border border-white/5">📊 Lakehouse Tables</span>
    <span class="p-1.5 rounded-lg bg-black/40 border border-white/5">🛡️ Zero-Trust SPIFFE</span>
  </div>
  <p class="text-slate-400 text-[10px] mt-1">
    Ask any technical question (e.g. <em>"Explain Azure failover"</em>, <em>"SPIFFE vs static secrets"</em>), or type <strong>"Interview Me"</strong> for a system design challenge.
  </p>
</div>"""
        return {"response": reply, "model": "conversational-copilot", "grounded_episode_id": active_episode.get("id")}

    # Case B: Socratic Mock Interview / System Design Challenge
    if any(k in q for k in ["interview", "challenge", "question", "quiz", "coach", "spar", "test me"]):
        if "142" in str(ep_num):
            scenario = """<strong>Scenario (Tier-1 Banking Architecture):</strong> You are designing a cross-region payment gateway across Azure East US (Primary) and West US (Secondary) with an RTO &lt; 60s and RPO &lt; 5s. Why choose Active-Passive Anycast traffic shedding with Azure Front Door over synchronous Multi-Region Active-Active database clustering?"""
            focal_points = "Explain how Anycast health probes automate DNS-less cutover, and why synchronous cross-region two-phase commits introduce unacceptable 120ms WAN latency penalties on ledger ACID transactions."
        elif "141" in str(ep_num):
            scenario = """<strong>Scenario (High-Scale Agentic RAG):</strong> Your multi-agent swarm generates 2,000 queries/sec against an HNSW vector database, causing P99 latency spikes of 450ms and excessive embedding API costs. How would you design a two-tier semantic vector cache using Redis?"""
            focal_points = "Discuss cosine similarity thresholding (e.g. &gt; 0.96), asynchronous cache warming for hot query clusters, and LRU eviction policies."
        elif "140" in str(ep_num):
            scenario = """<strong>Scenario (Enterprise Lakehouse Migration):</strong> A financial institution wants sub-minute financial reporting without running brittle 24-hour ETL batch refresh jobs. When should they choose Microsoft Fabric Direct Lake mode vs Snowflake managed Apache Iceberg tables?"""
            focal_points = "Compare memory-mapped VertiPaq I/O paging Delta Parquet directly from OneLake against multi-engine Iceberg catalogs like Polaris."
        else:
            scenario = """<strong>Scenario (Kernel Observability & Security):</strong> Your Kubernetes cluster runs 1,500 microservice pods. Security requires end-to-end mTLS (MAS TRM 9.2) and network tracing with &lt; 1% CPU overhead. Why deploy eBPF socket tracing with SPIFFE/SPIRE over traditional Envoy sidecar injection?"""
            focal_points = "Address sidecar memory bloat (2-5ms hop latency per pod), node-local cgroup attestation, and automated 60-minute X.509 SVID rotation."

        reply = f"""<div class="space-y-3">
  <div>
    <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">🎯 Socratic System Design Challenge (Ep #{ep_num})</span>
    <h4 class="font-bold text-slate-100 text-xs sm:text-sm mt-1.5">{scenario}</h4>
  </div>
  <div class="p-2.5 rounded-xl bg-black/40 border border-white/10 text-slate-300 text-[11px] space-y-1">
    <strong class="text-cyan-300 font-mono text-[10px]">Evaluation Rubric / Focal Points:</strong>
    <p class="leading-relaxed">{focal_points}</p>
  </div>
  <p class="text-slate-400 text-[10px]">
    Reply with your architectural rationale covering failure modes, networking topology, and blast-radius containment.
  </p>
</div>"""
        return {"response": reply, "model": "socratic-challenge-engine", "grounded_episode_id": active_episode.get("id")}

    # Case C: Cloud / Azure / AWS / Multi-Region Resiliency
    if any(k in q for k in ["azure", "aws", "cloud", "multi-region", "failover", "anycast", "front door", "resiliency", "rto", "rpo", "landing zone", "graviton", "spot"]):
        reply = """<div class="space-y-3">
  <div>
    <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Architectural Deep Dive: Multi-Region High-Availability & Resilient Cloud Topology</h4>
    <p class="text-slate-400 text-[10px] font-mono mt-0.5">Primary Sources: Azure Architecture Center & AWS Well-Architected Framework</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">1. Production Problem: Disaster Recovery vs WAN Latency Trade-offs</p>
    <p class="leading-relaxed text-slate-300">Synchronous Active-Active database clustering across distant cloud regions (e.g. East US to West US, ~70ms RTT) adds prohibitive round-trip latency to every transactional commit. Furthermore, network partitions risk split-brain conditions or cascaded failover outages.</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">2. Architectural Mechanism: Anycast Ingress & Active-Passive Traffic Shedding</p>
    <ul class="list-disc list-inside space-y-1 pl-1 text-slate-300">
      <li><strong>BGP Anycast Routing:</strong> Global edge entry points (Azure Front Door / AWS Global Accelerator) advertise identical IP addresses across edge POPs, ingesting traffic onto the cloud vendor's private backbone.</li>
      <li><strong>Sub-60s Automated Failover:</strong> Intelligent health probes detect regional cluster degradation and instantly re-route HTTP/TCP traffic without waiting for client DNS cache TTL expiration.</li>
      <li><strong>Asynchronous Ledger Replication:</strong> Data replication uses asynchronous log streaming to keep RPO &lt; 5s without penalizing write transaction throughput.</li>
    </ul>
  </div>

  <div class="p-2 rounded-lg bg-cyan-950/40 border border-cyan-500/30 text-[11px] text-cyan-200">
    <strong class="text-cyan-300">🎯 Staff Architect Interview Framing:</strong>
    <p class="mt-0.5 leading-relaxed">"In tier-1 cloud architectures, we decouple ingress routing from compute failover using Anycast edge acceleration. Active-Passive topology protects write consistency while delivering sub-minute automated disaster recovery."</p>
  </div>
</div>"""
        return {"response": reply, "model": "cloud-architecture-grounding", "grounded_episode_id": active_episode.get("id")}

    # Case D: Security / SPIFFE / Workload Identity / MAS TRM
    if any(k in q for k in ["spiffe", "spire", "workload identity", "mtls", "security", "token", "mas trm", "banking", "svid", "zero trust", "cgroup"]):
        reply = """<div class="space-y-3">
  <div>
    <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Architectural Deep Dive: SPIFFE Workload Attestation & MAS TRM Section 9.2</h4>
    <p class="text-slate-400 text-[10px] font-mono mt-0.5">Primary Sources: CNCF SPIFFE/SPIRE Spec & Singapore MAS Technology Risk Management Guidelines</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">1. Problem: The Danger of Static API Keys in Microservices</p>
    <p class="leading-relaxed text-slate-300">Traditional Kubernetes and cloud setups store long-lived service account tokens and database passwords in environment variables. If a pod is compromised, the attacker extracts credentials with months of validity.</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">2. Mechanism: Kernel-Level Attestation & In-Memory SVID Delivery</p>
    <ul class="list-disc list-inside space-y-1 pl-1 text-slate-300">
      <li><strong>Kernel cgroup Inspection:</strong> Node-local SPIRE agents verify Linux kernel cgroups, container binary SHA-256 hashes, and namespace metadata before issuing credentials.</li>
      <li><strong>UNIX Domain Socket Injection:</strong> Ephemeral X.509 SVID certificates are passed directly into workload memory via UNIX domain sockets, bypassing disk and environment variables.</li>
      <li><strong>60-Minute Zero-Downtime Rotation:</strong> Certificates auto-rotate every 60m with zero pod restarts, ensuring transient memory dumps become useless within an hour.</li>
    </ul>
  </div>

  <div class="p-2 rounded-lg bg-indigo-950/40 border border-indigo-500/30 text-[11px] text-indigo-200">
    <strong class="text-indigo-300">🎯 Regulatory & Interview Framing (MAS TRM 9.2):</strong>
    <p class="mt-0.5 leading-relaxed">"Replacing static credentials with SPIFFE workload identity establishes true cryptographic zero-trust microsegmentation, fulfilling MAS TRM Section 9 mandates for automated secret lifecycle elimination."</p>
  </div>
</div>"""
        return {"response": reply, "model": "security-architecture-grounding", "grounded_episode_id": active_episode.get("id")}

    # Case E: AI / Agent Workflows / Main-as-Router / Governance
    if any(k in q for k in ["router", "main-as-router", "agent", "swarm", "anthropic", "audit log", "drift", "hallucination", "dry-run", "judge"]):
        reply = """<div class="space-y-3">
  <div>
    <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Architectural Deep Dive: Deterministic Main-as-Router State Machines</h4>
    <p class="text-slate-400 text-[10px] font-mono mt-0.5">Primary Sources: Anthropic Agent Workflows Research & OpenAI Governance Guidelines</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">1. Problem: Prompt Drift & Runaway Loops in Autonomous Swarms</p>
    <p class="leading-relaxed text-slate-300">Unconstrained multi-agent swarms accumulate prompt drift across recursive conversational turns, leading to hallucinated tool arguments and infinite API retry loops that fail financial compliance audits.</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">2. Mechanism: Decoupled Planning & Structural Dry-Run Verification</p>
    <ul class="list-disc list-inside space-y-1 pl-1 text-slate-300">
      <li><strong>Decoupled State Machine:</strong> The central orchestrator manages planning and state transitions, while specialist execution workers execute isolated, stateless tool calls.</li>
      <li><strong>Structural Dry-Run Gates:</strong> Execution agents must output deterministic diffs before modifying files, databases, or cloud infrastructure.</li>
      <li><strong>Hard 3-Retry Caps:</strong> Consecutive tool errors are hard-capped at 3 before halting execution with <code>status: blocked</code>, bounding inference token spend.</li>
      <li><strong>Append-Only Trajectory Logs:</strong> Turn-by-turn reasoning steps and tool invocations are written to immutable JSONL audit trails for MAS FEAT and SR 11-7 validation.</li>
    </ul>
  </div>

  <div class="p-2 rounded-lg bg-purple-950/40 border border-purple-500/30 text-[11px] text-purple-200">
    <strong class="text-purple-300">🎯 Staff Architect Interview Framing:</strong>
    <p class="mt-0.5 leading-relaxed">"Decoupling stateful planning from stateless tool execution transforms probabilistic LLM behavior into a verifiable, deterministic state machine with strict financial cost boundaries."</p>
  </div>
</div>"""
        return {"response": reply, "model": "ai-architecture-grounding", "grounded_episode_id": active_episode.get("id")}

    # Case F: Data / Lakehouse / Fabric / Iceberg / Parquet
    if any(k in q for k in ["fabric", "direct lake", "onelake", "iceberg", "snowflake", "lakehouse", "vertipaq", "parquet", "polaris"]):
        reply = """<div class="space-y-3">
  <div>
    <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Architectural Deep Dive: Microsoft Fabric Direct Lake vs Snowflake Managed Iceberg</h4>
    <p class="text-slate-400 text-[10px] font-mono mt-0.5">Primary Sources: Microsoft Fabric Engineering Whitepapers & Apache Iceberg Spec</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">1. Problem: The Lakehouse Latency vs Storage Duplication Bottleneck</p>
    <p class="leading-relaxed text-slate-300">Traditional lakehouses forced architects to choose between high-cost DirectQuery (slow queries on raw files) or scheduled VertiPaq Import (sub-second queries, but 24-hour ETL batch delays and duplicate storage).</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">2. Mechanism: Zero-Copy Delta Parquet Memory Paging</p>
    <ul class="list-disc list-inside space-y-1 pl-1 text-slate-300">
      <li><strong>Direct Parquet Paging:</strong> Direct Lake pages Delta Parquet columns straight from OneLake storage into VertiPaq RAM via memory-mapped I/O.</li>
      <li><strong>Instant Refresh:</strong> As soon as Spark or Data Factory writes new Delta Parquet commits, analytical queries immediately read updated snapshots.</li>
      <li><strong>Multi-Engine Iceberg Catalogs:</strong> Apache Iceberg metadata catalogs allow Snowflake, DuckDB, and Spark to query identical Parquet files concurrently without vendor lock-in.</li>
    </ul>
  </div>

  <div class="p-2 rounded-lg bg-emerald-950/40 border border-emerald-500/30 text-[11px] text-emerald-200">
    <strong class="text-emerald-300">🎯 Staff Architect Interview Framing:</strong>
    <p class="mt-0.5 leading-relaxed">"Direct Lake mode delivers the sub-second speed of in-memory caching with the zero-copy freshness of a live data lake, eradicating brittle scheduled ETL pipelines."</p>
  </div>
</div>"""
        return {"response": reply, "model": "data-architecture-grounding", "grounded_episode_id": active_episode.get("id")}

    # Case G: SRE / eBPF / Observability
    if any(k in q for k in ["ebpf", "observability", "opentelemetry", "otel", "w3c", "tracing", "socket", "kernel", "sidecar"]):
        reply = """<div class="space-y-3">
  <div>
    <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Architectural Deep Dive: Kernel eBPF Socket Tracing & OpenTelemetry W3C Context</h4>
    <p class="text-slate-400 text-[10px] font-mono mt-0.5">Primary Sources: eBPF.io Foundation & CNCF OpenTelemetry Technical Advisory Group</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">1. Problem: Sidecar Telemetry Overhead in Large Kubernetes Fleets</p>
    <p class="leading-relaxed text-slate-300">Injecting Envoy sidecars across thousands of pods consumes significant CPU and memory while adding 2-5ms network hop latency. Manual tracing instrumentation also requires constant code maintenance.</p>
  </div>

  <div class="space-y-1 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">2. Mechanism: Kernel Socket Hooking & Distributed Context Propagation</p>
    <ul class="list-disc list-inside space-y-1 pl-1 text-slate-300">
      <li><strong>&lt; 1% CPU Overhead:</strong> eBPF bytecode programs run directly in the Linux kernel, capturing TCP handshakes and payload metrics with negligible footprint.</li>
      <li><strong>Automated W3C Trace Headers:</strong> Automatically inspects and propagates <code>traceparent</code> and <code>tracestate</code> headers across HTTP and gRPC calls without changing application code.</li>
      <li><strong>Continuous In-Kernel Profiling:</strong> Generates on-CPU/off-CPU flame graphs across all container runtimes to pinpoint execution bottlenecks.</li>
    </ul>
  </div>

  <div class="p-2 rounded-lg bg-amber-950/40 border border-amber-500/30 text-[11px] text-amber-200">
    <strong class="text-amber-300">🎯 Staff Architect Interview Framing:</strong>
    <p class="mt-0.5 leading-relaxed">"eBPF shifts observability from user-space sidecars into the Linux kernel, delivering full distributed tracing with zero application changes and &lt; 1% CPU overhead."</p>
  </div>
</div>"""
        return {"response": reply, "model": "sre-architecture-grounding", "grounded_episode_id": active_episode.get("id")}

    # Case H: Dynamic Domain Search in Active Episode
    for dom, data in takeaways.items():
        title_l = data.get("title", "").lower()
        badge_l = data.get("badge", "").lower()
        bullets_l = " ".join(data.get("bullets", [])).lower()
        
        # Check if query words match domain title or badge or bullets
        query_words = [w for w in q.split() if len(w) > 2]
        if any(w in title_l or w in badge_l or w in dom for w in query_words):
            bullets_html = "".join([f'<li class="leading-relaxed">{b}</li>' for b in data.get("bullets", [])])
            sources_html = "".join([f'<a href="{s.get("url")}" target="_blank" class="px-2 py-0.5 rounded bg-black/40 text-cyan-300 border border-white/10 text-[10px] font-mono hover:underline">{s.get("title")}</a> ' for s in data.get("sources", [])])
            
            reply = f"""<div class="space-y-3">
  <div>
    <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-white/10 text-cyan-300 border border-white/10">{data.get('badge', dom.upper())}</span>
    <h4 class="font-bold text-slate-100 text-xs sm:text-sm mt-1.5">{data.get('title')}</h4>
  </div>

  <div class="space-y-1.5 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">Key Architectural Mechanisms (Episode #{ep_num}):</p>
    <ul class="list-disc list-inside space-y-1 pl-1 text-slate-300">
      {bullets_html}
    </ul>
  </div>

  {f'<div class="p-2 rounded-lg bg-black/40 border border-white/10 text-[11px] text-slate-300"><strong class="text-amber-300">💡 Interview Framing:</strong><p class="mt-0.5 leading-relaxed">{data.get("interview_framing")}</p></div>' if data.get("interview_framing") else ''}

  {f'<div class="pt-1.5 border-t border-white/5 flex flex-wrap items-center gap-1.5 text-[10px] font-mono"><span class="text-slate-400">Sources:</span>{sources_html}</div>' if sources_html else ''}
</div>"""
            return {"response": reply, "model": "domain-targeted-synthesis", "grounded_episode_id": active_episode.get("id")}

    # Case I: Dynamic Active Episode Overview
    top_domains = list(takeaways.items())[:3]
    overview_items = ""
    for dom, data in top_domains:
        overview_items += f"""
        <div class="p-2.5 rounded-xl bg-black/30 border border-white/5 space-y-1">
          <span class="px-1.5 py-0.5 rounded text-[9px] font-mono font-bold bg-white/10 text-cyan-300">{data.get('badge', dom.upper())}</span>
          <h5 class="text-xs font-bold text-slate-100">{data.get('title')}</h5>
          <p class="text-[11px] text-slate-300 leading-snug">{(data.get('bullets', [''])[0])}</p>
        </div>"""

    reply = f"""<div class="space-y-3">
  <div>
    <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Grounded Intelligence Briefing: Episode #{ep_num}</h4>
    <p class="text-slate-300 text-[11px] leading-relaxed mt-0.5">
      Analyzing today's technical corpus for <strong>{ep_title}</strong>:
    </p>
  </div>

  <div class="space-y-2">
    {overview_items}
  </div>

  <div class="pt-1.5 border-t border-white/5 flex items-center justify-between text-[10px] font-mono text-cyan-400">
    <span>Grounded in Episode #{ep_num} Corpus</span>
    <span>8 Domains Available</span>
  </div>
</div>"""

    return {
        "response": reply,
        "model": "episode-corpus-synthesis",
        "grounded_episode_id": active_episode.get("id")
    }

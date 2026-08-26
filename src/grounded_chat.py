import json
import logging
import os
import re
from typing import Dict, Any, List, Optional
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.grounded_chat")

GROUNDED_CHAT_SYSTEM_PROMPT = """You are the Lead Enterprise Architect & Socratic Interview Coach for TechPulse OS.
You are strictly grounded in today's comprehensive technical paper corpus, whitepapers, and regulatory standards.

Guidelines:
1. Conversational & Authoritative: Provide fluent, specification-grade architectural analyses that directly answer the user's question.
2. Structure: Break down technical responses into:
   - Architectural Problem & Context
   - Technical Mechanism & Token/Data Flow (with exact protocols, Linux kernel mechanisms, memory-mapped I/O, network routing, etc.)
   - Regulatory Alignment (MAS TRM Section 9, MAS FEAT, US Fed SR 11-7 where relevant)
   - Staff Architect Interview Framing
3. Citations: Cite primary sources explicitly (e.g. [Source: Research Whitepaper]).
4. If the user asks casual or conversational questions (e.g. greetings), respond naturally and authoritatively as their Lead Architect copilot without forcing a rigid template.
"""

async def call_gemini_llm(api_key: str, prompt: str) -> tuple[Optional[str], Optional[str]]:
    clean_key = api_key.strip().strip('"').strip("'")
    if not clean_key or len(clean_key) < 10 or clean_key.startswith("${"):
        msg = f"GEMINI_API_KEY is not configured or is a placeholder in container: '{clean_key[:8]}...' (len={len(clean_key)})"
        logger.warning(msg)
        return None, msg

    logger.info(f"Initiating live Gemini generation with API key length: {len(clean_key)}")
    errors = []

    models_to_try = [
        os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-2.5-flash"
    ]
    models_to_try = list(dict.fromkeys(models_to_try))

    # Tier 1: google.genai SDK
    try:
        from google import genai
        client = genai.Client(api_key=clean_key)
        for m in models_to_try:
            try:
                res = client.models.generate_content(model=m, contents=prompt)
                if res and res.text:
                    logger.info(f"Gemini live grounding succeeded via SDK ({m})")
                    return res.text, None
            except Exception as ex:
                err_msg = f"google.genai model {m} error: {ex}"
                logger.warning(err_msg)
                errors.append(err_msg)
    except Exception as e:
        err_msg = f"google.genai SDK initialization failed: {e}"
        logger.warning(err_msg)
        errors.append(err_msg)

    # Tier 2: google.generativeai SDK
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=clean_key)
        for m in models_to_try:
            try:
                model = genai_legacy.GenerativeModel(m)
                res = model.generate_content(prompt)
                if res and res.text:
                    logger.info(f"Gemini live grounding succeeded via legacy SDK ({m})")
                    return res.text, None
            except Exception as ex:
                err_msg = f"google.generativeai model {m} error: {ex}"
                logger.warning(err_msg)
                errors.append(err_msg)
    except Exception as e:
        err_msg = f"google.generativeai SDK error: {e}"
        logger.warning(err_msg)
        errors.append(err_msg)

    # Tier 3: Direct httpx async REST call
    for m in models_to_try:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={clean_key}"
            async with httpx.AsyncClient(timeout=25.0) as http_client:
                r = await http_client.post(
                    url,
                    json={"contents": [{"parts": [{"text": prompt}]}]}
                )
                if r.status_code == 200:
                    data = r.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if text:
                            logger.info(f"Direct REST Gemini call succeeded ({m})")
                            return text, None
                else:
                    err_msg = f"REST Gemini call ({m}) HTTP {r.status_code}: {r.text[:200]}"
                    logger.warning(err_msg)
                    errors.append(err_msg)
        except Exception as ex:
            err_msg = f"REST Gemini call ({m}) network error: {ex}"
            logger.warning(err_msg)
            errors.append(err_msg)

    combined_errors = "; ".join(errors)
    return None, combined_errors

# Enterprise Architectural Concept Knowledge Base
CONCEPT_EXPANSIONS: Dict[str, Dict[str, str]] = {
    "swarm": {
        "title": "Multi-Agent Swarm Architectures & Autonomous Graphs",
        "definition": "Multi-agent swarms are distributed AI architectures where specialized autonomous agents collaborate across multi-step execution graphs. Rather than relying on a single monolithic prompt, swarms decompose complex workflows into specialized sub-agents (planners, researchers, execution workers, and verifiers).",
        "pitfalls": "In production, unconstrained peer-to-peer swarms suffer from exponential prompt drift, cascading hallucinations, unbounded token spend, and circular execution loops.",
        "solution": "Enterprise systems enforce the Deterministic Main-as-Router pattern—centralizing state management and routing decisions in a deterministic orchestrator while keeping execution workers stateless with strict dry-run approval gates."
    },
    "agent": {
        "title": "Autonomous Agent Workflows & State Machines",
        "definition": "Agentic workflows leverage LLMs with access to external tools and memory to execute multi-step goals iteratively. Key patterns include routing, orchestrator-workers, evaluator-optimizer loops, and autonomous swarms.",
        "pitfalls": "Without rigid boundaries, agents exhibit non-deterministic behavior, fail to self-terminate on ambiguous outputs, and risk executing unintended state modifications.",
        "solution": "Modern enterprise architectures decouple stateful planning from stateless execution workers, enforcing hard retry limits (max 3) and mandatory diff reviews prior to mutation execution."
    },
    "mutation": {
        "title": "API Mutations & State Alterations in Autonomous Systems",
        "definition": "An API mutation is any operation that creates, updates, or deletes state on an external service or datastore (e.g. HTTP POST, PUT, PATCH, DELETE or database INSERT/UPDATE/DELETE), contrasting with idempotent read-only queries (GET).",
        "pitfalls": "When autonomous LLM agents execute API mutations without human-in-the-loop or structural verification, hallucinated arguments can trigger irreversible data loss, erroneous financial transactions, or corrupted production databases.",
        "solution": "TechPulse OS mandates Structural Dry-Run Approval Gates: agents must compute and present deterministic execution diffs for explicit authorization before any mutation payload is dispatched."
    },
    "stateless": {
        "title": "Stateless Execution Workers vs Stateful Orchestrators",
        "definition": "Stateless execution means individual sub-agents and tool workers operate in isolated, single-turn execution environments without retaining conversational history or mutable memory between invocations.",
        "pitfalls": "Allowing worker agents to maintain mutable state causes context pollution, hidden state dependencies, and makes failure recovery non-reproducible.",
        "solution": "Centralizing all state transitions in the Main Router guarantees that worker outputs are deterministic, auditable, and easily retried without context contamination."
    },
    "dry-run": {
        "title": "Structural Dry-Run Verification Gates",
        "definition": "A dry-run gate is a deterministic pre-execution validation step where an agent generates and inspects the exact intended mutations (diffs, SQL statements, API payloads) before committing them to production systems.",
        "pitfalls": "Relying solely on LLM confidence scores or verbal confirmation without inspectable structural diffs leads to silent deployment failures and compliance violations.",
        "solution": "Structural diffs must be rendered and explicitly approved before mutation execution, satisfying regulatory audit standards (MAS FEAT and US Fed SR 11-7)."
    },
    "spiffe": {
        "title": "SPIFFE/SPIRE Workload Identity & Cryptographic Zero-Trust",
        "definition": "SPIFFE (Secure Production Identity Framework for Everyone) provides a universal cryptographic identity standard (SVIDs) for distributed workloads across Kubernetes, VMs, and clouds.",
        "pitfalls": "Static credentials (API keys, passwords, long-lived tokens) stored in environment variables or configuration files are vulnerable to memory extraction and unauthorized reuse.",
        "solution": "SPIRE agents inspect node-local kernel cgroups and binary hashes, injecting ephemeral X.509 certificates directly into process memory with automated 60-minute zero-downtime rotation."
    },
    "rotation": {
        "title": "Automated 60-Minute Zero-Downtime Credential Rotation",
        "definition": "Short-lived credential rotation automatically renews cryptographic SVID certificates every 60 minutes in memory over UNIX domain sockets without restarting processes or severing active TCP connections.",
        "pitfalls": "Manual rotation cycles or long credential lifespans expand the blast radius of credential leaks, while process-restarting rotations cause downtime and connection drops.",
        "solution": "In-memory dynamic TLS reloading (using Go GetCertificate or Envoy dynamic SDS) ensures zero-downtime renewal while strictly bounding leak validity to under 1 hour (MAS TRM 9.2)."
    },
    "failover": {
        "title": "Sub-60s Multi-Region Anycast Failover & Resiliency",
        "definition": "Active-Passive multi-region routing uses BGP Anycast ingress edge points (e.g. Azure Front Door / AWS Global Accelerator) to shed traffic to a healthy secondary cloud region in under 60 seconds.",
        "pitfalls": "DNS-based failover is hindered by client-side TTL caching, while synchronous cross-region database clustering introduces 70-120ms WAN latency penalties on ledger writes.",
        "solution": "BGP Anycast bypasses client DNS caching entirely, while asynchronous ledger streaming maintains RPO < 5s without penalizing transactional throughput."
    },
    "direct lake": {
        "title": "Microsoft Fabric Direct Lake Mode & Zero-ETL Analytics",
        "definition": "Direct Lake is an enterprise lakehouse storage architecture where analytical engines (VertiPaq) page Delta Parquet columns straight from OneLake storage into RAM via memory-mapped I/O.",
        "pitfalls": "Traditional data pipelines forced a trade-off between slow direct querying on raw storage or brittle 24-hour ETL batch imports with duplicate data storage.",
        "solution": "Direct Lake achieves sub-second analytical latency with zero data duplication, reading fresh commits instantly as soon as upstream Spark jobs write to OneLake."
    },
    "ebpf": {
        "title": "Kernel-Level eBPF Socket Tracing & Zero-Overhead Observability",
        "definition": "eBPF (Extended Berkeley Packet Filter) allows sandboxed bytecode programs to execute safely inside the Linux kernel, capturing networking and distributed trace headers directly from kernel sockets.",
        "pitfalls": "Traditional user-space sidecars (e.g. Envoy) add 2-5ms hop latency per pod and consume significant cluster CPU/RAM at scale.",
        "solution": "eBPF provides full W3C distributed trace propagation and TCP profiling with < 1% CPU overhead and zero application code modifications."
    },
    "retry": {
        "title": "Deterministic Retry Caps & State Isolation",
        "definition": "A deterministic retry cap enforces a hard bound (e.g., maximum 3 consecutive failures) on sub-agent execution before halting execution and escalating to the planning orchestrator.",
        "pitfalls": "Without hard retry bounds, transient errors or ambiguous tool outputs cause agents to enter recursive retry loops, consuming thousands of dollars in LLM API tokens.",
        "solution": "Enforcing state isolation with a 3-retry threshold transitions the task to status: blocked, preventing cascading failures and protecting compute budgets."
    }
}

def dynamic_rag_synthesize(query: str, active_episode: Dict[str, Any]) -> str:
    q = query.strip().lower()
    ep_num = active_episode.get("episode_number", active_episode.get("id", "142").replace("ep-", ""))
    ep_title = active_episode.get("title", "Technical Briefing")
    takeaways = active_episode.get("takeaways", {})
    full_articles = active_episode.get("full_articles", {})

    # Greeting / Conversational Intent
    greetings = ["hi", "hello", "hey", "how are you", "who are you", "what can you do", "what's up", "good morning", "good evening", "help"]
    if any(q == g or q.startswith(g + " ") or q.endswith(" " + g) for g in greetings):
        domains_summary = ", ".join([f"{data.get('badge', dom.upper())}" for dom, data in list(takeaways.items())[:4]])
        return f"""<div class="space-y-2">
  <p class="text-slate-200 text-xs leading-relaxed">
    Hello Aaron! I am your <strong>Lead Enterprise Architect Copilot</strong>, strictly grounded in <strong>Episode #{ep_num} ({ep_title})</strong>.
  </p>
  <p class="text-slate-300 text-[11px] leading-relaxed">
    I am ready to evaluate production trade-offs, inspect low-level system mechanisms, or conduct a system design interview across today's topics: <em>{domains_summary}</em>. What would you like to explore?
  </p>
</div>"""

    # Socratic System Design Challenge Intent
    if any(k in q for k in ["interview", "challenge", "question", "quiz", "coach", "spar", "test me"]):
        top_dom = list(takeaways.values())[0] if takeaways else {}
        framing = top_dom.get("interview_framing", "Explain the production failure modes and architectural mitigation strategy.")
        return f"""<div class="space-y-3">
  <div>
    <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">🎯 Socratic System Design Challenge (Ep #{ep_num})</span>
    <h4 class="font-bold text-slate-100 text-xs sm:text-sm mt-1.5">{top_dom.get("title", ep_title)}</h4>
  </div>
  <div class="p-2.5 rounded-xl bg-black/40 border border-white/10 text-slate-300 text-[11px] space-y-1">
    <strong class="text-cyan-300 font-mono text-[10px]">Architectural Prompt:</strong>
    <p class="leading-relaxed">{framing}</p>
  </div>
  <p class="text-slate-400 text-[10px]">
    Reply with your architectural rationale covering networking, consistency bounds, and blast-radius containment.
  </p>
</div>"""

    # 1. Match Concept Expansion
    matched_concept = None
    for k, v in CONCEPT_EXPANSIONS.items():
        if k in q:
            matched_concept = v
            break

    # 2. Match Domain & Bullets in Active Episode
    q_words = set(re.findall(r'[a-z0-9\-]+', q))
    stopwords = {'what', 'is', 'the', 'about', 'tell', 'me', 'how', 'does', 'work', 'in', 'and', 'for', 'of', 'a', 'an', 'to', 'with', 'on', 'can', 'you', 'explain', 'describe'}
    keywords = [w for w in q_words if w not in stopwords and len(w) > 1]

    scored_hits = []
    for dom, data in takeaways.items():
        title = data.get("title", "")
        badge = data.get("badge", "")
        for b in data.get("bullets", []):
            score = sum(4 if kw in b.lower() else (2 if kw in title.lower() or kw in badge.lower() else 0) for kw in keywords)
            if score > 0:
                scored_hits.append((score, dom, data, b))

    scored_hits.sort(key=lambda x: x[0], reverse=True)

    if scored_hits and scored_hits[0][0] > 0:
        matched_item = scored_hits[0]
        dom_meta = matched_item[2]
        dom_key = matched_item[1]
        
        concept_title = matched_concept.get("title") if matched_concept else dom_meta.get("title", ep_title)
        concept_def = matched_concept.get("definition") if matched_concept else f"In enterprise distributed systems, <strong>{dom_meta.get('title')}</strong> represents a key operational mechanism for high-reliability production."
        concept_pitfall = matched_concept.get("pitfalls") if matched_concept else "Without strict architectural guardrails, distributed components suffer from non-deterministic execution states and compliance risks."
        concept_sol = matched_concept.get("solution") if matched_concept else f"In Episode #{ep_num}, this mechanism is implemented through the following validated architectural controls:"

        bullets_formatted = []
        top_hits = [h for h in scored_hits if h[1] == dom_key][:2]
        for hit in top_hits:
            text = hit[3]
            if ":" in text:
                hdr, body = text.split(":", 1)
                bullets_formatted.append(f'<li class="leading-relaxed"><strong class="text-cyan-300">{hdr.strip()}:</strong>{body}</li>')
            else:
                bullets_formatted.append(f'<li class="leading-relaxed">{text}</li>')

        bullets_html = "".join(bullets_formatted)
        framing_html = f"""<div class="p-2.5 rounded-xl bg-black/40 border border-white/10 text-[11px] text-slate-300 space-y-1">
  <strong class="text-amber-300 font-mono text-[10px]">💡 Staff Architect Interview & Regulatory Framing:</strong>
  <p class="leading-relaxed">{dom_meta.get('interview_framing')}</p>
</div>""" if dom_meta.get("interview_framing") else ""

        sources_html = "".join([f'<a href="{s.get("url")}" target="_blank" class="px-2 py-0.5 rounded bg-black/40 text-cyan-300 border border-white/10 text-[10px] font-mono hover:underline">{s.get("title")}</a> ' for s in dom_meta.get("sources", [])])
        sources_block = f"""<div class="pt-1.5 border-t border-white/5 flex flex-wrap items-center gap-1.5 text-[10px] font-mono">
  <span class="text-slate-400">Sources:</span>
  {sources_html}
</div>""" if sources_html else ""

        return f"""<div class="space-y-3.5 text-slate-200 text-xs">
  <div>
    <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-white/10 text-cyan-300 border border-white/10">{dom_meta.get('badge', dom_key.upper())}</span>
    <h4 class="font-bold text-slate-100 text-xs sm:text-sm mt-1.5">{concept_title}</h4>
  </div>

  <div class="p-3 rounded-xl bg-night-900 border border-white/5 space-y-2.5 text-slate-300 text-[11px] sm:text-xs">
    <div>
      <h5 class="text-[10px] font-mono uppercase tracking-wider text-cyan-400 font-bold mb-1">1. Architectural Foundation & Definition</h5>
      <p class="leading-relaxed">{concept_def}</p>
    </div>

    <div>
      <h5 class="text-[10px] font-mono uppercase tracking-wider text-rose-400 font-bold mb-1">2. Production Pitfalls & Failure Modes</h5>
      <p class="leading-relaxed">{concept_pitfall}</p>
    </div>

    <div>
      <h5 class="text-[10px] font-mono uppercase tracking-wider text-emerald-400 font-bold mb-1">3. Episode #{ep_num} Implementation Mechanics</h5>
      <p class="leading-relaxed mb-1.5">{concept_sol}</p>
      <ul class="list-disc list-inside space-y-1.5 pl-1 text-slate-300">
        {bullets_html}
      </ul>
    </div>
  </div>

  {framing_html}
  {sources_block}
</div>"""

    # Dynamic Fallback: Episode Overview
    top_dom = list(takeaways.items())[:3]
    overview_items = "".join([f"""<div class="p-2 rounded-xl bg-black/30 border border-white/5 space-y-0.5">
  <span class="px-1.5 py-0.5 rounded text-[9px] font-mono font-bold bg-white/10 text-cyan-300">{d.get('badge', dom.upper())}</span>
  <h5 class="text-xs font-bold text-slate-100">{d.get('title')}</h5>
  <p class="text-[11px] text-slate-300 leading-snug">{d.get('bullets', [''])[0]}</p>
</div>""" for dom, d in top_dom])

    return f"""<div class="space-y-2.5">
  <h4 class="font-bold text-cyan-300 text-xs sm:text-sm">Grounded Briefing: Episode #{ep_num}</h4>
  <p class="text-slate-300 text-[11px] leading-relaxed">
    Analyzing today's technical corpus for <strong>{ep_title}</strong>:
  </p>
  <div class="space-y-2">
    {overview_items}
  </div>
  <div class="pt-1.5 border-t border-white/10 flex items-center justify-between text-[10px] font-mono text-cyan-400">
    <span>Grounded in Episode #{ep_num} Corpus</span>
    <span>{len(takeaways)} Domains Available</span>
  </div>
</div>"""

async def process_grounded_chat(query: str, active_episode: Dict[str, Any], chat_history: List[Dict[str, str]] = None) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "")
    full_articles = active_episode.get("full_articles", {})
    ep_num = active_episode.get("episode_number", active_episode.get("id", "142").replace("ep-", ""))
    ep_title = active_episode.get("title", "Technical Briefing")
    takeaways = active_episode.get("takeaways", {})

    # 1. Live LLM Grounding (when Gemini API Key is configured)
    if api_key and len(api_key.strip()) > 10:
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
        llm_response, error_detail = await call_gemini_llm(api_key, prompt)
        if llm_response:
            return {
                "response": llm_response,
                "model": "gemini-2.0-flash (live-grounded-corpus)",
                "grounded_episode_id": active_episode.get("id")
            }
        else:
            logger.error(f"Live Gemini LLM failed across all tiers: {error_detail}")

    # 2. Universal Dynamic Semantic RAG Synthesizer (Offline / Zero-API-Key Fallback)
    fallback_response = dynamic_rag_synthesize(query, active_episode)
    return {
        "response": fallback_response,
        "model": "dynamic-semantic-rag-engine",
        "grounded_episode_id": active_episode.get("id")
    }


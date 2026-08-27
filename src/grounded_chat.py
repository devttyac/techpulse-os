import json
import logging
import os
import re
from typing import Dict, Any, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.grounded_chat")

GROUNDED_CHAT_SYSTEM_PROMPT = """You are the Lead Enterprise Architect & Socratic Interview Coach for TechPulse OS.
You are strictly grounded in today's comprehensive technical paper corpus, whitepapers, and enterprise standards.

Guidelines:
1. Conversational & Authoritative: Provide fluent, specification-grade architectural analyses that directly answer the user's question.
2. Structure: Break down technical responses into:
   - Architectural Problem & Context
   - Technical Mechanism & Token/Data Flow (with exact protocols, Linux kernel mechanisms, memory-mapped I/O, network routing, etc.)
   - Enterprise Governance & Reliability (NIST AI RMF, NIST SP 800-207 Zero Trust, ISO 42001, OWASP where relevant)
   - Staff Architect Interview Framing
3. Citations: Cite primary sources explicitly (e.g. [Source: Research Whitepaper]).
4. If the user asks casual or conversational questions (e.g. greetings), respond naturally and authoritatively as their Lead Architect copilot without forcing a rigid template.
"""

async def call_gemini_llm(api_key: str, prompt: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    clean_key = api_key.strip().strip('"').strip("'")
    if not clean_key or len(clean_key) < 10 or clean_key.startswith("${"):
        msg = f"GEMINI_API_KEY is not configured or is a placeholder in container: '{clean_key[:8]}...' (len={len(clean_key)})"
        logger.warning(msg)
        return None, None, msg

    # Model cascade with priority: env var -> modern recommended -> aliases
    env_model = os.getenv("GEMINI_MODEL", "").strip()
    candidate_models = [m for m in [env_model, "gemini-3.6-flash", "gemini-2.5-flash", "gemini-flash"] if m]
    seen = set()
    models_to_try = []
    for m in candidate_models:
        if m not in seen:
            seen.add(m)
            models_to_try.append(m)

    errors = []

    # Tier 1: Modern google.genai SDK
    try:
        from google import genai
        client = genai.Client(api_key=clean_key)
        for m in models_to_try:
            try:
                response = client.models.generate_content(
                    model=m,
                    contents=prompt
                )
                if response and response.text:
                    logger.info(f"Modern google.genai SDK call succeeded ({m})")
                    return response.text, m, None
            except Exception as ex_m:
                err_msg = f"Modern SDK ({m}) failed: {ex_m}"
                logger.warning(err_msg)
                errors.append(err_msg)
    except ImportError:
        pass
    except Exception as ex:
        err_msg = f"Modern google.genai SDK initialization error: {ex}"
        logger.warning(err_msg)
        errors.append(err_msg)

    # Tier 2: Legacy google.generativeai SDK
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=clean_key)
        for m in models_to_try:
            try:
                model = genai_legacy.GenerativeModel(m)
                response = model.generate_content(prompt)
                if response and response.text:
                    logger.info(f"Legacy google.generativeai SDK call succeeded ({m})")
                    return response.text, m, None
            except Exception as ex_m:
                err_msg = f"Legacy SDK ({m}) failed: {ex_m}"
                logger.warning(err_msg)
                errors.append(err_msg)
    except ImportError:
        pass
    except Exception as ex:
        err_msg = f"Legacy google.generativeai SDK error: {ex}"
        logger.warning(err_msg)
        errors.append(err_msg)

    # Tier 3: Direct httpx async REST call
    for m in models_to_try:
        try:
            import httpx
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
                            return text, m, None
                else:
                    err_msg = f"REST Gemini call ({m}) HTTP {r.status_code}: {r.text[:200]}"
                    logger.warning(err_msg)
                    errors.append(err_msg)
        except Exception as ex:
            err_msg = f"REST Gemini call ({m}) network error: {ex}"
            logger.warning(err_msg)
            errors.append(err_msg)

    combined_errors = "; ".join(errors)
    return None, None, combined_errors

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
        "solution": "Structural diffs must be rendered and explicitly approved before mutation execution, satisfying enterprise audit and model risk standards (NIST AI RMF and ISO 42001)."
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
        "solution": "In-memory dynamic TLS reloading (using Go GetCertificate or Envoy dynamic SDS) ensures zero-downtime renewal while strictly bounding leak validity to under 1 hour (NIST SP 800-207 Zero Trust)."
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
        return (
            f"Hello Aaron! I am your **Lead Enterprise Architect Copilot**, strictly grounded in **Episode #{ep_num} ({ep_title})**.\n\n"
            f"I am ready to evaluate production trade-offs, inspect low-level system mechanisms, or conduct a system design interview across today's topics: *{domains_summary}*. What would you like to explore?"
        )

    # Socratic System Design Challenge Intent
    if any(k in q for k in ["interview", "challenge", "question", "quiz", "coach", "spar", "test me"]):
        top_dom = list(takeaways.values())[0] if takeaways else {}
        framing = top_dom.get("interview_framing", "Explain the production failure modes and architectural mitigation strategy.")
        top_title = top_dom.get("title", ep_title)
        return (
            f"### 🎯 Socratic System Design Challenge (Episode #{ep_num})\n\n"
            f"**Challenge Domain**: {top_title}\n\n"
            f"**Architectural Prompt**:\n{framing}\n\n"
            f"*Reply with your architectural rationale covering networking, consistency bounds, and blast-radius containment.*"
        )

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
        badge = dom_meta.get('badge', dom_key.upper())
        
        concept_title = matched_concept.get("title") if matched_concept else dom_meta.get("title", ep_title)
        concept_def = matched_concept.get("definition") if matched_concept else f"In enterprise distributed systems, **{dom_meta.get('title')}** represents a key operational mechanism for high-reliability production."
        concept_pitfall = matched_concept.get("pitfalls") if matched_concept else "Without strict architectural guardrails, distributed components suffer from non-deterministic execution states and compliance risks."
        concept_sol = matched_concept.get("solution") if matched_concept else f"In Episode #{ep_num}, this mechanism is implemented through the following validated architectural controls:"

        bullets_formatted = []
        top_hits = [h for h in scored_hits if h[1] == dom_key][:2]
        for hit in top_hits:
            text = hit[3]
            if ":" in text:
                hdr, body = text.split(":", 1)
                bullets_formatted.append(f"- **{hdr.strip()}**: {body.strip()}")
            else:
                bullets_formatted.append(f"- {text.strip()}")

        bullets_md = "\n".join(bullets_formatted)
        framing_md = f"\n\n💡 **Staff Architect Interview Framing**:\n{dom_meta.get('interview_framing')}" if dom_meta.get("interview_framing") else ""
        sources_list = [f"[{s.get('title')}]({s.get('url')})" for s in dom_meta.get("sources", [])]
        sources_md = f"\n\n**Sources**: {', '.join(sources_list)}" if sources_list else ""

        return (
            f"### [{badge}] {concept_title}\n\n"
            f"**1. Architectural Foundation & Definition**\n{concept_def}\n\n"
            f"**2. Production Pitfalls & Failure Modes**\n{concept_pitfall}\n\n"
            f"**3. Episode #{ep_num} Implementation Mechanics**\n{concept_sol}\n"
            f"{bullets_md}"
            f"{framing_md}"
            f"{sources_md}"
        )

    # Dynamic Fallback: Episode Overview
    top_dom = list(takeaways.items())[:3]
    overview_items = "\n".join([f"- **[{d.get('badge', dom.upper())}] {d.get('title')}**: {d.get('bullets', [''])[0]}" for dom, d in top_dom])

    return (
        f"### Grounded Briefing: Episode #{ep_num}\n\n"
        f"Analyzing today's technical corpus for **{ep_title}**:\n\n"
        f"{overview_items}\n\n"
        f"*Grounded in Episode #{ep_num} Corpus ({len(takeaways)} Domains Available)*"
    )

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
        llm_response, used_model, error_detail = await call_gemini_llm(api_key, prompt)
        if llm_response:
            return {
                "response": llm_response,
                "model": f"{used_model} (live-grounded-corpus)",
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


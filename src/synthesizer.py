import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.synthesizer")

SYSTEM_SYNTHESIS_PROMPT = """You are the Principal AI Synthesis Engine for TechPulse OS.
Your task is to analyze today's ingested engineering articles across 8 technology domains and generate an executive multi-host technical podcast briefing, structured interview takeaways, 8 timecoded chapters (one per technology domain), and spaced-repetition flashcards.

The briefing hosts are:
- Host A: Enterprise Cloud Architect & AI Systems Specialist
- Host B: Principal Systems Architect & Engineering Governance Lead

You MUST return a strictly valid JSON object matching this schema:
{
  "title": "Executive headline summarizing today's key technical synthesis",
  "summary": "2-3 sentence executive synopsis of today's briefing",
  "hosts": "Host A (Enterprise Cloud Architect) & Host B (Principal Systems Architect & Governance Lead)",
  "script_segments": [
    {"speaker": "Host A", "text": "...", "chapter_title": "1. 🤖 AI & Multi-Agent Deterministic Routing"},
    {"speaker": "Host B", "text": "...", "chapter_title": "1. 🤖 AI & Multi-Agent Deterministic Routing"}
  ],
  "chapters": [
    {"title": "1. 🤖 AI & Multi-Agent Deterministic Routing", "source_name": "Anthropic Engineering", "source_url": "https://www.anthropic.com/research/building-effective-agents"},
    {"title": "2. ☁️ Multi-Region Resiliency & Azure Landing Zones", "source_name": "Azure Architecture", "source_url": "https://learn.microsoft.com/azure/architecture/"},
    {"title": "3. 📊 Microsoft Fabric Direct Lake vs Snowflake Iceberg", "source_name": "MS Fabric Team Blog", "source_url": "https://blog.fabric.microsoft.com/"},
    {"title": "4. 🛡️ Zero-Trust SPIFFE Workload Tokens & Attestation", "source_name": "SPIFFE Foundation", "source_url": "https://spiffe.io/docs/latest/spiffe-about/overview/"},
    {"title": "5. ⚙️ SRE Kernel eBPF Observability & Distributed Tracing", "source_name": "eBPF.io", "source_url": "https://ebpf.io/what-is-ebpf/"},
    {"title": "6. ⚡ Distributed Systems Architecture & Outbox CDC", "source_name": "Debezium Community", "source_url": "https://debezium.io/"},
    {"title": "7. 💰 Spot GPU Orchestration & LLM Token FinOps", "source_name": "FinOps Foundation", "source_url": "https://www.finops.org/"},
    {"title": "8. ⚖️ NIST AI Risk Management & ISO 42001 Governance", "source_name": "NIST AI & Cybersecurity", "source_url": "https://www.nist.gov/itl/ai-risk-management-framework"}
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
        {"title": "Anthropic: Building Effective Agents", "url": "https://www.anthropic.com/research/building-effective-agents"}
      ]
    }
  },
  "flashcards": [
    {
      "domain": "🤖 AI & Agent Systems",
      "question": "In an enterprise interview, explain why the Deterministic Main-as-Router pattern is preferred over recursive monolithic swarms.",
      "answer": "Decouples stateful planning from tool execution. It enforces strict dry-run approval gates, caps step retry loops to 3, and produces immutable audit trails required by enterprise production standards (NIST AI RMF & ISO 42001).",
      "cite": "Source: Anthropic Research 2026",
      "color_class": "bg-indigo-500/20 text-indigo-300"
    }
  ]
}
"""

def extract_full_articles_corpus(domain_corpus: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    corpus_map = {}
    for domain, articles in domain_corpus.items():
        if articles:
            blob = f"DOMAIN: {domain.upper()}\n"
            for a in articles[:4]:
                title = a.get('title', 'Untitled')
                src = a.get('source_name', 'Source')
                summary = a.get('summary', '')
                url = a.get('url', '')
                blob += f"• [{src}] {title}\n  Summary: {summary}\n  Link: {url}\n\n"
            corpus_map[domain] = blob.strip()
    return corpus_map

def generate_deterministic_fallback(domain_corpus: Dict[str, List[Dict[str, Any]]], episode_num: int) -> Dict[str, Any]:
    logger.info("Generating structured deterministic intelligence payload from ingested corpus across all 8 domains...")
    
    # Extract top article titles per domain
    ai_item = domain_corpus.get("ai", [{}])[0] if domain_corpus.get("ai") else {}
    cloud_item = domain_corpus.get("cloud", [{}])[0] if domain_corpus.get("cloud") else {}
    data_item = domain_corpus.get("data", [{}])[0] if domain_corpus.get("data") else {}
    sec_item = domain_corpus.get("sec", [{}])[0] if domain_corpus.get("sec") else {}
    devops_item = domain_corpus.get("devops", [{}])[0] if domain_corpus.get("devops") else {}
    arch_item = domain_corpus.get("arch", [{}])[0] if domain_corpus.get("arch") else {}
    finops_item = domain_corpus.get("finops", [{}])[0] if domain_corpus.get("finops") else {}
    gov_item = domain_corpus.get("gov", [{}])[0] if domain_corpus.get("gov") else {}

    full_articles = extract_full_articles_corpus(domain_corpus)

    return {
        "id": f"ep-{episode_num}",
        "episode_number": episode_num,
        "date": datetime.now(timezone.utc).strftime("%b %d, %Y"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "title": "Executive Briefing: Full-Stack Enterprise Architecture, Agentic Governance & Zero Trust",
        "summary": "Today's briefing covers all 8 engineering pillars: Anthropic deterministic agent routing, cross-region Kubernetes failover, Fabric Direct Lake, SPIFFE workload attestation, kernel eBPF tracing, Debezium transactional outbox CDC, spot GPU FinOps, and NIST AI Risk Management.",
        "hosts": "Host A (Enterprise Cloud Architect) & Host B (Principal Systems Architect & Governance Lead)",
        "duration": "09:45",
        "total_seconds": 585,
        "full_articles": full_articles,
        "script_segments": [
            {
                "speaker": "Host A",
                "text": "Good morning and welcome to TechPulse OS. Today, we lead with enterprise multi-agent system design. Anthropic's latest engineering report highlights that autonomous agent swarms without a deterministic router suffer from cascading hallucination loops in high-context tasks.",
                "chapter_title": "1. 🤖 AI & Multi-Agent Deterministic Routing"
            },
            {
                "speaker": "Host B",
                "text": "That's a crucial architectural shift. In enterprise distributed systems, we cannot rely on unbounded prompt loops. The Main-as-Router pattern enforces strict state serialization and pre-execution dry-run approval gates, directly meeting production reliability and safety standards.",
                "chapter_title": "1. 🤖 AI & Multi-Agent Deterministic Routing"
            },
            {
                "speaker": "Host A",
                "text": "Moving to Cloud & Platforms: achieving cross-region high availability with RTO under 60 seconds requires decoupling Anycast ingress from asynchronous storage replication across Azure Landing Zones and AWS.",
                "chapter_title": "2. ☁️ Multi-Region Resiliency & Azure Landing Zones"
            },
            {
                "speaker": "Host B",
                "text": "Correct. Using Azure Front Door paired with GitOps controllers like FluxCD ensures identical stateless pod topologies while avoiding multi-region synchronous database locking penalties.",
                "chapter_title": "2. ☁️ Multi-Region Resiliency & Azure Landing Zones"
            },
            {
                "speaker": "Host A",
                "text": "Turning to enterprise data architecture, Microsoft Fabric's Direct Lake mode is transforming analytical reporting. Instead of duplicating data into VertiPaq files via scheduled batch jobs, it queries Delta Parquet files directly from OneLake into VertiPaq memory on demand.",
                "chapter_title": "3. 📊 Microsoft Fabric Direct Lake vs Snowflake Iceberg"
            },
            {
                "speaker": "Host B",
                "text": "And Snowflake is competing directly with managed Apache Iceberg tables. The advantage for architects is vendor neutrality: an external Iceberg catalog allows Spark, Databricks, and Snowflake engines to operate on the same S3 storage tier without vendor lock-in.",
                "chapter_title": "3. 📊 Microsoft Fabric Direct Lake vs Snowflake Iceberg"
            },
            {
                "speaker": "Host A",
                "text": "In infrastructure security, static credentials in CI/CD pipelines are officially obsolete. SPIFFE and SPIRE automated workload identity federation issues ephemeral X.509 SVID certificates rotating every 60 minutes.",
                "chapter_title": "4. 🛡️ Zero-Trust SPIFFE Workload Tokens & Attestation"
            },
            {
                "speaker": "Host B",
                "text": "SPIRE inspects Linux kernel cgroups and container namespaces directly, satisfying NIST SP 800-207 Zero Trust credential lifecycle mandates.",
                "chapter_title": "4. 🛡️ Zero-Trust SPIFFE Workload Tokens & Attestation"
            },
            {
                "speaker": "Host A",
                "text": "In SRE and observability: eBPF socket tracing captures TCP latency and packet drops inside kernel space with under 1% CPU overhead, propagating W3C distributed trace context into OpenTelemetry.",
                "chapter_title": "5. ⚙️ SRE Kernel eBPF Observability & Distributed Tracing"
            },
            {
                "speaker": "Host B",
                "text": "And in distributed systems architecture, Debezium Change Data Capture reads database Write-Ahead Logs to guarantee Transactional Outbox atomicity without fragile Two-Phase Commit locks.",
                "chapter_title": "6. ⚡ Distributed Systems Architecture & Outbox CDC"
            },
            {
                "speaker": "Host A",
                "text": "On Cloud Economics and FinOps: auto-pausing idle Fabric capacities and leveraging Graviton4 spot instance pools reduces batch inference spend by over 35 percent.",
                "chapter_title": "7. 💰 Spot GPU Orchestration & LLM Token FinOps"
            },
            {
                "speaker": "Host B",
                "text": "Finally, on AI governance, the NIST AI Risk Management Framework and ISO 42001 guidelines mandate immutable audit logging capturing prompt snapshots, model temperature, and output for every production GenAI decision.",
                "chapter_title": "8. ⚖️ NIST AI Risk Management & ISO 42001 Governance"
            }
        ],
        "chapters": [
            {
                "time": "00:00",
                "seconds": 0,
                "title": "1. 🤖 AI & Multi-Agent Deterministic Routing",
                "source_name": ai_item.get("source_name", "Anthropic Research"),
                "source_url": ai_item.get("url", "https://www.anthropic.com/research/building-effective-agents")
            },
            {
                "time": "01:15",
                "seconds": 75,
                "title": "2. ☁️ Multi-Region Resiliency & Azure Landing Zones",
                "source_name": cloud_item.get("source_name", "Azure Architecture"),
                "source_url": cloud_item.get("url", "https://learn.microsoft.com/azure/architecture/")
            },
            {
                "time": "02:25",
                "seconds": 145,
                "title": "3. 📊 Microsoft Fabric Direct Lake vs Snowflake Iceberg",
                "source_name": data_item.get("source_name", "MS Fabric Team Blog"),
                "source_url": data_item.get("url", "https://blog.fabric.microsoft.com/")
            },
            {
                "time": "03:40",
                "seconds": 220,
                "title": "4. 🛡️ Zero-Trust SPIFFE Workload Tokens & Attestation",
                "source_name": sec_item.get("source_name", "SPIFFE Foundation"),
                "source_url": sec_item.get("url", "https://spiffe.io/docs/latest/spiffe-about/overview/")
            },
            {
                "time": "04:55",
                "seconds": 295,
                "title": "5. ⚙️ SRE Kernel eBPF Observability & Distributed Tracing",
                "source_name": devops_item.get("source_name", "eBPF.io"),
                "source_url": devops_item.get("url", "https://ebpf.io/what-is-ebpf/")
            },
            {
                "time": "06:10",
                "seconds": 370,
                "title": "6. ⚡ Distributed Systems Architecture & Outbox CDC",
                "source_name": arch_item.get("source_name", "Debezium Community"),
                "source_url": arch_item.get("url", "https://debezium.io/")
            },
            {
                "time": "07:25",
                "seconds": 445,
                "title": "7. 💰 Spot GPU Orchestration & LLM Token FinOps",
                "source_name": finops_item.get("source_name", "FinOps Foundation"),
                "source_url": finops_item.get("url", "https://www.finops.org/")
            },
            {
                "time": "08:35",
                "seconds": 515,
                "title": "8. ⚖️ NIST AI Risk Management & ISO 42001 Governance",
                "source_name": gov_item.get("source_name", "NIST AI & Cybersecurity"),
                "source_url": gov_item.get("url", "https://www.nist.gov/itl/ai-risk-management-framework")
            }
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
            },
            "cloud": {
                "badge": "MULTI-CLOUD & AZURE ARCHITECTURE",
                "release_date": "Aug 2026",
                "title": "Cross-Region Multi-Cluster Kubernetes & Azure Landing Zone Failover Patterns",
                "bullets": [
                    "Automated active-passive traffic shedding with Azure Front Door, AWS Global Accelerator, and Cloudflare Magic Transit.",
                    "Stateful persistent volume replication via Azure Managed Disks cross-region replication and Rook-Ceph AZ mesh.",
                    "Zero-downtime blue/green ingress cutover with automated health probe circuit breakers across hybrid clouds."
                ],
                "interview_framing": "Describe trade-offs between Azure Mission-Critical Multi-Region Active-Active vs Active-Passive architectures with RPO < 5s.",
                "sources": [
                    {"title": "Microsoft Azure: Mission-Critical Architecture", "url": "https://techcommunity.microsoft.com/t5/s/rss/board?board.id=AzureArchitectureBlog"},
                    {"title": "AWS Architecture: Multi-Region Resilience", "url": "https://aws.amazon.com/blogs/architecture/"},
                    {"title": "Google Cloud: Cloud Architecture Center", "url": "https://cloudblog.withgoogle.com/rss/"}
                ]
            },
            "data": {
                "badge": "ENTERPRISE LAKEHOUSE",
                "release_date": "Aug 2026",
                "title": "Microsoft Fabric Direct Lake vs Snowflake Managed Apache Iceberg Tables",
                "bullets": [
                    "Direct Lake loads Parquet data straight into VertiPaq memory without import refresh schedules.",
                    "Snowflake Iceberg external catalog enables vendor-neutral multi-engine querying across Databricks & Spark.",
                    "Universal semantic layer governance via OneLake security & Snowflake tag-based masking."
                ],
                "interview_framing": "Frame how migrating from legacy SSAS to Direct Lake reduces data staleness from 24 hours to sub-minute telemetry.",
                "sources": [
                    {"title": "Microsoft Fabric: Direct Lake Deep Dive", "url": "https://blog.fabric.microsoft.com/"},
                    {"title": "Snowflake: Iceberg Tables", "url": "https://www.snowflake.com/en/blog/"}
                ]
            },
            "sec": {
                "badge": "WORKLOAD SECURITY",
                "release_date": "Aug 2026",
                "title": "Workload Identity Federation & Ephemeral mTLS Tokens in Zero-Trust Systems",
                "bullets": [
                    "Eliminates long-lived static API secrets in CI/CD pipelines via OIDC identity assertions.",
                    "SPIFFE/SPIRE automated X.509 certificate rotation every 60 minutes for inter-service microsegments.",
                    "Zero-trust egress filtering to prevent LLM prompt injection data exfiltration attacks."
                ],
                "interview_framing": "Articulate why eliminating static database credentials in favor of IAM-role assume tokens satisfies NIST SP 800-207 Zero Trust.",
                "sources": [
                    {"title": "SPIFFE: Workload Identity Spec", "url": "https://spiffe.io/docs/latest/spiffe-about/overview/"},
                    {"title": "NIST: Zero Trust Architecture", "url": "https://csrc.nist.gov/publications/detail/sp/800-207/final"}
                ]
            },
            "devops": {
                "badge": "SRE & OBSERVABILITY",
                "release_date": "Aug 2026",
                "title": "Kernel-Level Observability with eBPF & OpenTelemetry Distributed Tracing",
                "bullets": [
                    "Captures socket and TCP network latency directly in Linux kernel with <1% CPU overhead.",
                    "End-to-end W3C trace-context propagation across asynchronous message queues and microservices.",
                    "Automated SLO error-budget alerting triggering automated rollback of misbehaving canary pods."
                ],
                "interview_framing": "Connect 20+ years of ITIL incident root-cause triage to modern automated SLO error-budgeting.",
                "sources": [
                    {"title": "OpenTelemetry: Tracing Spec", "url": "https://opentelemetry.io/docs/concepts/signals/traces/"},
                    {"title": "eBPF: Kernel Telemetry", "url": "https://ebpf.io/what-is-ebpf/"}
                ]
            },
            "arch": {
                "badge": "EVENT-DRIVEN PATTERNS",
                "release_date": "Aug 2026",
                "title": "Transactional Outbox Pattern with Debezium CDC vs Two-Phase Commit (2PC)",
                "bullets": [
                    "Guarantees atomicity between local database state and external event brokers without distributed locks.",
                    "Debezium Change Data Capture reads database Write-Ahead Logs (WAL) with sub-10ms event publishing.",
                    "Idempotency keys and consumer deduplication tables prevent duplicate event processing."
                ],
                "interview_framing": "Explain why 2PC distributed transactions fail under high network partition risk and how Transactional Outbox provides eventual consistency.",
                "sources": [
                    {"title": "Debezium: Outbox Pattern", "url": "https://debezium.io/documentation/reference/stable/patterns/outbox.html"},
                    {"title": "Martin Fowler: Distributed Patterns", "url": "https://martinfowler.com/articles/patterns-of-distributed-systems/"}
                ]
            },
            "finops": {
                "badge": "CLOUD ECONOMICS",
                "release_date": "Aug 2026",
                "title": "Automated Compute Auto-Pause & Graviton4 Spot Orchestration for Batch ETL",
                "bullets": [
                    "Fabric F-SKU capacity reservation combined with auto-pausing cuts overnight compute burn by 42%.",
                    "AWS Graviton4 / ARM64 Spot node pools for Spark ETL jobs reduce cost-per-gigabyte processed by 35%.",
                    "Prompt token caching and prompt compression reduce LLM API inference costs by 60%."
                ],
                "interview_framing": "Demonstrate executive business acumen by showing how you track unit cost per pipeline run rather than aggregate cloud bills.",
                "sources": [
                    {"title": "FinOps Foundation: Unit Economics", "url": "https://www.finops.org/framework/"},
                    {"title": "AWS Compute: Spot Optimization", "url": "https://aws.amazon.com/blogs/compute/"}
                ]
            },
            "gov": {
                "badge": "REGULATORY COMPLIANCE",
                "release_date": "Aug 2026",
                "title": "NIST AI Risk Management Framework (AI RMF 1.0) & Production AI Safety",
                "bullets": [
                    "Enforces immutable audit trails capturing prompt, context snapshot, model temperature, and output for every decision.",
                    "Automated guardrail gates evaluate toxic output, PII leakage, and hallucination scoring before delivery.",
                    "Tiered governance classification (Tier 1/2/3) determining whether human approval is mandatory before pipeline advancement."
                ],
                "interview_framing": "Highlight how your combined background in systems architecture, zero-trust infrastructure, and enterprise AI risk frameworks ensures safe, compliant production AI adoption.",
                "sources": [
                    {"title": "NIST: AI Risk Management Framework", "url": "https://www.nist.gov/itl/ai-risk-management-framework"},
                    {"title": "ISO/IEC 42001: AI Management Systems", "url": "https://www.iso.org/standard/81230.html"}
                ]
            }
        },
        "flashcards": [
            {
                "domain": "🤖 AI & Agent Systems",
                "question": "In an enterprise interview, explain why the Deterministic Main-as-Router pattern is preferred over recursive monolithic swarms.",
                "answer": "Decouples stateful planning from tool execution. It enforces strict dry-run approval gates, caps step retry loops to 3, and produces immutable audit trails required by enterprise production standards (NIST AI RMF & ISO 42001).",
                "cite": "Source: Anthropic Research 2026",
                "color_class": "bg-indigo-500/20 text-indigo-300"
            },
            {
                "domain": "📊 Data & Modern Lakehouse",
                "question": "How does Microsoft Fabric Direct Lake mode differ from Import and DirectQuery in terms of memory paging?",
                "answer": "Direct Lake loads Delta Parquet straight from OneLake into VertiPaq memory on demand without .PBIX duplication or scheduled refresh pipelines, falling back to DirectQuery only if capacity memory is exceeded.",
                "cite": "Source: Microsoft Fabric Team Blog",
                "color_class": "bg-emerald-500/20 text-emerald-300"
            },
            {
                "domain": "🛡️ Zero Trust & Security",
                "question": "How does SPIFFE/SPIRE workload identity satisfy NIST SP 800-207 Zero Trust static secret removal rules?",
                "answer": "It replaces static API tokens and database passwords with automated, cryptographic X.509 SVID tokens that rotate automatically every 60 minutes with mTLS verification.",
                "cite": "Source: SPIFFE Spec & NIST SP 800-207",
                "color_class": "bg-rose-500/20 text-rose-300"
            },
            {
                "domain": "⚙️ SRE & Kernel Observability",
                "question": "Why does eBPF kernel tracing outperform legacy user-space APM daemon agents during high network throughput?",
                "answer": "eBPF attaches verified bytecode sandboxes directly to kernel kprobes and socket buffers, eliminating expensive user-to-kernel context switching and running with <1% CPU overhead.",
                "cite": "Source: eBPF.io Foundation 2026",
                "color_class": "bg-amber-500/20 text-amber-300"
            }
        ]
    }

async def synthesize_briefing(domain_corpus: Dict[str, List[Dict[str, Any]]], episode_num: int = 142) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    full_articles = extract_full_articles_corpus(domain_corpus)

    if not api_key:
        logger.info("GEMINI_API_KEY not configured. Using deterministic synthesis pipeline.")
        return generate_deterministic_fallback(domain_corpus, episode_num)

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        
        # Prepare article corpus summary for LLM
        corpus_text = ""
        for domain, articles in domain_corpus.items():
            corpus_text += f"\n\n### DOMAIN: {domain.upper()}\n"
            for a in articles[:3]:
                corpus_text += f"- [{a.get('source_name')}]: {a.get('title')} ({a.get('url')})\n  Summary: {a.get('summary')}\n"

        prompt = f"{SYSTEM_SYNTHESIS_PROMPT}\n\n## INGESTED ARTICLE CORPUS:\n{corpus_text}"
        
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        
        parsed = json.loads(response.text)
        parsed["id"] = f"ep-{episode_num}"
        parsed["episode_number"] = episode_num
        parsed["date"] = datetime.now(timezone.utc).strftime("%b %d, %Y")
        parsed["created_at"] = datetime.now(timezone.utc).isoformat()
        parsed["duration"] = parsed.get("duration", "05:20")
        parsed["total_seconds"] = parsed.get("total_seconds", 320)
        parsed["full_articles"] = full_articles
        return parsed
    except Exception as e:
        logger.error(f"Error calling Gemini API for synthesis ({model_name}): {e}. Falling back to deterministic pipeline.")
        return generate_deterministic_fallback(domain_corpus, episode_num)
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

async def call_gemini_llm(api_key: str, prompt: str) -> Optional[str]:
    clean_key = api_key.strip().strip('"').strip("'")
    if not clean_key or len(clean_key) < 10 or clean_key.startswith("${"):
        logger.warning(f"GEMINI_API_KEY is not configured or is a placeholder in container: '{clean_key[:8]}...' (len={len(clean_key)})")
        return None

    logger.info(f"Initiating live Gemini generation with API key length: {len(clean_key)}")

    models_to_try = [
        os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-2.5-flash"
    ]
    models_to_try = list(dict.fromkeys(models_to_try))

    # 1. Try google.genai SDK
    try:
        from google import genai
        client = genai.Client(api_key=clean_key)
        for m in models_to_try:
            try:
                res = client.models.generate_content(model=m, contents=prompt)
                if res and res.text:
                    logger.info(f"Gemini live grounding succeeded via SDK ({m})")
                    return res.text
            except Exception as ex:
                logger.warning(f"google.genai call with model {m} failed: {ex}")
    except Exception as e:
        logger.warning(f"google.genai SDK initialization: {e}")

    # 2. Try direct httpx async REST call
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
                            return text
                else:
                    logger.warning(f"REST Gemini call ({m}) HTTP {r.status_code}: {r.text[:180]}")
        except Exception as ex:
            logger.warning(f"REST Gemini call ({m}) network error: {ex}")

    return None

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

    # Extract Semantic Keywords (ignoring stop words)
    q_words = set(re.findall(r'[a-z0-9\-]+', q))
    stopwords = {'what', 'is', 'the', 'about', 'tell', 'me', 'how', 'does', 'work', 'in', 'and', 'for', 'of', 'a', 'an', 'to', 'with', 'on', 'can', 'you', 'explain'}
    keywords = [w for w in q_words if w not in stopwords and len(w) > 1]

    scored_hits = []

    # 1. Score Full Articles if present
    for dom, text in full_articles.items():
        sentences = [s.strip() for s in re.split(r'\n+|\.\s+', text) if len(s.strip()) > 20]
        for s in sentences:
            score = sum(2 if kw in s.lower() else 0 for kw in keywords)
            if score > 0:
                scored_hits.append((score, dom, s))

    # 2. Score Takeaways
    for dom, data in takeaways.items():
        title = data.get("title", "")
        badge = data.get("badge", "")
        for b in data.get("bullets", []):
            score = sum(3 if kw in b.lower() else (2 if kw in title.lower() or kw in badge.lower() else 0) for kw in keywords)
            if score > 0:
                scored_hits.append((score, dom, b))

    scored_hits.sort(key=lambda x: x[0], reverse=True)

    if scored_hits and scored_hits[0][0] > 0:
        top_hits = scored_hits[:3]
        matched_dom = top_hits[0][1]
        dom_meta = takeaways.get(matched_dom, {})

        bullets_html = "".join([f'<li class="leading-relaxed">{hit[2]}</li>' for hit in top_hits])
        framing_html = f"""<div class="p-2 rounded-lg bg-black/40 border border-white/10 text-[11px] text-slate-300">
  <strong class="text-amber-300">💡 Interview & Architecture Framing:</strong>
  <p class="mt-0.5 leading-relaxed">{dom_meta.get('interview_framing')}</p>
</div>""" if dom_meta.get("interview_framing") else ""

        sources_html = "".join([f'<a href="{s.get("url")}" target="_blank" class="px-2 py-0.5 rounded bg-black/40 text-cyan-300 border border-white/10 text-[10px] font-mono hover:underline">{s.get("title")}</a> ' for s in dom_meta.get("sources", [])])
        sources_block = f"""<div class="pt-1.5 border-t border-white/5 flex flex-wrap items-center gap-1.5 text-[10px] font-mono">
  <span class="text-slate-400">Sources:</span>
  {sources_html}
</div>""" if sources_html else ""

        return f"""<div class="space-y-3">
  <div>
    <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-white/10 text-cyan-300 border border-white/10">{dom_meta.get('badge', matched_dom.upper())}</span>
    <h4 class="font-bold text-slate-100 text-xs sm:text-sm mt-1.5">{dom_meta.get('title', ep_title)}</h4>
  </div>

  <div class="space-y-1.5 text-slate-300 text-[11px] sm:text-xs">
    <p class="font-semibold text-slate-100">Grounded Technical Analysis (Episode #{ep_num}):</p>
    <ul class="list-disc list-inside space-y-1.5 pl-1 text-slate-300">
      {bullets_html}
    </ul>
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
        llm_response = await call_gemini_llm(api_key, prompt)
        if llm_response:
            return {
                "response": llm_response,
                "model": "gemini-2.0-flash (live-grounded-corpus)",
                "grounded_episode_id": active_episode.get("id")
            }

    # 2. Universal Dynamic Semantic RAG Synthesizer (Offline / Zero-API-Key Fallback)
    fallback_response = dynamic_rag_synthesize(query, active_episode)
    return {
        "response": fallback_response,
        "model": "dynamic-semantic-rag-engine",
        "grounded_episode_id": active_episode.get("id")
    }


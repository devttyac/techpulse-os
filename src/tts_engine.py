import asyncio
import logging
import os
import shutil
import tempfile
from typing import Dict, List, Any, Tuple, Optional
import edge_tts
from mutagen.mp3 import MP3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.tts")

# Multi-Host Voice Configuration
VOICE_MAP = {
    "Host A": "en-US-GuyNeural",   # Enterprise Cloud Architect (Clear, authoritative male)
    "Host B": "en-US-AriaNeural"   # SRE & Governance Lead (Precise, articulate female)
}

DOMAIN_VOICE_MAP = {
    "ai": "en-US-GuyNeural",
    "cloud": "en-US-GuyNeural",
    "data": "en-US-GuyNeural",
    "sec": "en-US-AriaNeural",
    "devops": "en-US-AriaNeural",
    "arch": "en-US-GuyNeural",
    "finops": "en-US-AriaNeural",
    "gov": "en-US-AriaNeural"
}

async def generate_segment_audio(text: str, voice: str, output_path: str) -> None:
    communicate = edge_tts.Communicate(text, voice, rate="+5%", pitch="+0Hz")
    await communicate.save(output_path)

def get_audio_duration_seconds(file_path: str) -> float:
    try:
        audio = MP3(file_path)
        return float(audio.info.length)
    except Exception as e:
        logger.warning(f"Could not read duration for {file_path}: {e}")
        # Fallback estimation: ~150 words per minute -> ~2.5 words per sec
        return 0.0

def format_seconds_to_time(seconds: int) -> str:
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins:02d}:{secs:02d}"

async def generate_domain_standalone_audio(domain: str, domain_data: Dict[str, Any], output_dir: str, ep_id: Optional[str] = None) -> Optional[str]:
    os.makedirs(output_dir, exist_ok=True)
    voice = DOMAIN_VOICE_MAP.get(domain, "en-US-GuyNeural")
    
    title = domain_data.get("title", f"{domain.upper()} Architecture Update")
    badge = domain_data.get("badge", domain.upper())
    bullets = domain_data.get("bullets", [])
    framing = domain_data.get("interview_framing", "")
    
    narration = f"TechPulse OS Deep-Dive Briefing on {badge}. {title}. "
    if bullets:
        narration += "Key architectural takeaways: "
        for b in bullets:
            narration += f"{b} "
    if framing:
        narration += f"Staff Architect Interview Framing: {framing} "

    # Save target filenames
    targets = [os.path.join(output_dir, f"article-{domain}.mp3")]
    if ep_id:
        targets.append(os.path.join(output_dir, f"{ep_id}-{domain}.mp3"))

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        await generate_segment_audio(narration, voice, tmp_path)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            for target in targets:
                shutil.copyfile(tmp_path, target)
            logger.info(f"Generated standalone domain audio for [{domain}] -> {targets[0]}")
            return targets[0]
    except Exception as e:
        logger.error(f"Error generating standalone audio for domain [{domain}]: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return None

async def generate_all_domain_audios(episode_data: Dict[str, Any], output_dir: str) -> Dict[str, str]:
    takeaways = episode_data.get("takeaways", {})
    ep_id = episode_data.get("id", "ep-142")
    results = {}
    tasks = []
    domains = []
    
    for domain, data in takeaways.items():
        domains.append(domain)
        tasks.append(generate_domain_standalone_audio(domain, data, output_dir, ep_id))
        
    if tasks:
        generated = await asyncio.gather(*tasks, return_exceptions=True)
        for dom, path in zip(domains, generated):
            if isinstance(path, str):
                results[dom] = path
    return results

async def generate_episode_podcast_audio(episode_data: Dict[str, Any], output_dir: str) -> Tuple[str, List[Dict[str, Any]], str, int]:
    episode_id = episode_data.get("id", "ep-142")
    os.makedirs(output_dir, exist_ok=True)
    final_mp3_path = os.path.join(output_dir, f"{episode_id}.mp3")

    raw_chapters = episode_data.get("chapters", [])
    existing_duration = episode_data.get("duration", "05:20")
    existing_seconds = episode_data.get("total_seconds", 320)

    # If audio already exists and is valid, calculate duration if needed
    if os.path.exists(final_mp3_path) and os.path.getsize(final_mp3_path) > 10000:
        actual_secs = int(get_audio_duration_seconds(final_mp3_path))
        if actual_secs > 0:
            existing_seconds = actual_secs
            existing_duration = format_seconds_to_time(actual_secs)
        logger.info(f"Audio file for {episode_id} already exists at {final_mp3_path} ({existing_duration})")
        return final_mp3_path, raw_chapters, existing_duration, existing_seconds

    logger.info(f"Generating multi-host neural audio briefing for {episode_id} via Edge-TTS...")
    script_segments = episode_data.get("script_segments", [])
    
    # If script_segments missing, construct from summary and chapters
    if not script_segments:
        summary = episode_data.get("summary", "Daily technical intelligence briefing.")
        title = episode_data.get("title", "Executive Technical Briefing")
        script_segments = [
            {"speaker": "Host A", "text": f"Welcome to TechPulse OS. Today we review: {title}."},
            {"speaker": "Host B", "text": summary}
        ]
        for c in raw_chapters:
            script_segments.append({
                "speaker": "Host A",
                "text": f"Covering {c.get('title')}, sourced from {c.get('source_name')}.",
                "chapter_title": c.get("title")
            })
        script_segments.append({"speaker": "Host B", "text": "Visit the dashboard to test your knowledge with interactive flashcards."})

    dynamic_chapters: List[Dict[str, Any]] = []
    chapter_map: Dict[str, Dict[str, Any]] = {}
    for c in raw_chapters:
        chapter_map[c.get("title", "")] = c

    with tempfile.TemporaryDirectory() as temp_dir:
        segment_files = []
        cumulative_seconds = 0.0
        seen_chapters = set()

        for idx, seg in enumerate(script_segments):
            speaker = seg.get("speaker", "Host A")
            voice = VOICE_MAP.get(speaker, "en-US-GuyNeural")
            text = seg.get("text", "")
            seg_path = os.path.join(temp_dir, f"seg_{idx:03d}.mp3")
            
            try:
                await generate_segment_audio(text, voice, seg_path)
                segment_files.append(seg_path)
                seg_duration = get_audio_duration_seconds(seg_path)

                chap_title = seg.get("chapter_title")
                if chap_title and chap_title not in seen_chapters:
                    seen_chapters.add(chap_title)
                    base_meta = chapter_map.get(chap_title, {})
                    start_sec = int(cumulative_seconds)
                    dynamic_chapters.append({
                        "time": format_seconds_to_time(start_sec),
                        "seconds": start_sec,
                        "title": chap_title,
                        "source_name": base_meta.get("source_name", "Primary Source"),
                        "source_url": base_meta.get("source_url", "")
                    })

                cumulative_seconds += seg_duration
            except Exception as e:
                logger.error(f"Error generating TTS segment {idx} ({speaker}): {e}")

        # If dynamic_chapters is empty, fallback to raw_chapters with distributed offsets
        if not dynamic_chapters:
            dynamic_chapters = raw_chapters

        # Concatenate segment files into final MP3 using ffmpeg or binary concat
        if segment_files:
            try:
                concat_list_path = os.path.join(temp_dir, "concat_list.txt")
                with open(concat_list_path, "w") as f:
                    for sf in segment_files:
                        f.write(f"file '{sf}'\n")
                
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", final_mp3_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                logger.info(f"Successfully synthesized episode MP3 via ffmpeg at {final_mp3_path}")
            except Exception as e:
                logger.warning(f"ffmpeg concatenation failed: {e}. Using raw stream copy.")
                with open(final_mp3_path, "wb") as outfile:
                    for sf in segment_files:
                        with open(sf, "rb") as infile:
                            shutil.copyfileobj(infile, outfile)

    total_secs = int(get_audio_duration_seconds(final_mp3_path)) if os.path.exists(final_mp3_path) else int(cumulative_seconds)
    duration_str = format_seconds_to_time(total_secs) if total_secs > 0 else "05:20"

    return final_mp3_path, dynamic_chapters, duration_str, total_secs
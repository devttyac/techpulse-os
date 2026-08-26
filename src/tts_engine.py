import asyncio
import logging
import os
import shutil
import tempfile
from typing import Dict, List, Any, Tuple
import edge_tts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("techpulse.tts")

# Multi-Host Voice Configuration
VOICE_MAP = {
    "Host A": "en-US-GuyNeural",   # Enterprise Cloud Architect (Clear, authoritative male)
    "Host B": "en-US-AriaNeural"   # SRE & Governance Lead (Precise, articulate female)
}

async def generate_segment_audio(text: str, voice: str, output_path: str) -> None:
    communicate = edge_tts.Communicate(text, voice, rate="+5%", pitch="+0Hz")
    await communicate.save(output_path)

async def generate_episode_podcast_audio(episode_data: Dict[str, Any], output_dir: str) -> Tuple[str, List[Dict[str, Any]]]:
    episode_id = episode_data.get("id", "ep-142")
    os.makedirs(output_dir, exist_ok=True)
    final_mp3_path = os.path.join(output_dir, f"{episode_id}.mp3")

    # If audio already exists, return
    if os.path.exists(final_mp3_path) and os.path.getsize(final_mp3_path) > 1000:
        logger.info(f"Audio file for {episode_id} already exists at {final_mp3_path}")
        return final_mp3_path, episode_data.get("chapters", [])

    logger.info(f"Generating multi-host neural audio briefing for {episode_id} via Edge-TTS...")
    script_segments = episode_data.get("script_segments", [])
    
    if not script_segments:
        logger.warning("No script segments provided to TTS engine.")
        return "", []

    with tempfile.TemporaryDirectory() as temp_dir:
        segment_files = []
        for idx, seg in enumerate(script_segments):
            speaker = seg.get("speaker", "Host A")
            voice = VOICE_MAP.get(speaker, "en-US-GuyNeural")
            text = seg.get("text", "")
            seg_path = os.path.join(temp_dir, f"seg_{idx:03d}.mp3")
            
            try:
                await generate_segment_audio(text, voice, seg_path)
                segment_files.append(seg_path)
            except Exception as e:
                logger.error(f"Error generating TTS segment {idx} ({speaker}): {e}")

        # Concatenate segment files into final MP3 using ffmpeg or binary concat
        if segment_files:
            try:
                # Use ffmpeg concat if available
                concat_list_path = os.path.join(temp_dir, "concat_list.txt")
                with open(concat_list_path, "w") as f:
                    for sf in segment_files:
                        f.write(f"file '{sf}'\n")
                
                cmd = f"ffmpeg -y -f concat -safe 0 -i {concat_list_path} -c copy {final_mp3_path}"
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                logger.info(f"Successfully synthesized episode MP3 at {final_mp3_path}")
            except Exception as e:
                logger.warning(f"ffmpeg concatenation failed: {e}. Using raw stream copy.")
                with open(final_mp3_path, "wb") as outfile:
                    for sf in segment_files:
                        with open(sf, "rb") as infile:
                            shutil.copyfileobj(infile, outfile)

    return final_mp3_path, episode_data.get("chapters", [])
import asyncio
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import tempfile
from typing import Dict, List, Any, Tuple, Optional
from src.content_availability import DOMAIN_ORDER, active_domains
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
    if domain_data.get("status") not in (None, "available") or not domain_data.get("bullets"):
        return None
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
    targets = [] if "status" in domain_data else [os.path.join(output_dir, f"article-{domain}.mp3")]
    if ep_id:
        targets.append(os.path.join(output_dir, f"{ep_id}-{domain}.mp3"))
    if not targets:
        return None

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        await generate_segment_audio(narration, voice, tmp_path)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000 and get_audio_duration_seconds(tmp_path) > 0:
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
    
    coverage = episode_data.get("content_availability")
    allowed = set(active_domains(coverage)) if coverage is not None else set(takeaways)
    audio = episode_data.setdefault("audio_availability", {})
    audio["domains"] = {d: {"status": "unavailable", "reason": "No article narration available."} for d in DOMAIN_ORDER}
    for domain, data in takeaways.items():
        if domain not in allowed or not isinstance(data, dict) or data.get("status") not in (None, "available") or not data.get("bullets"):
            continue
        audio["domains"][domain]["reason"] = "Audio generation failed or returned no playable track."
        domains.append(domain)
        tasks.append(generate_domain_standalone_audio(domain, data, output_dir, ep_id))
        
    if tasks:
        generated = await asyncio.gather(*tasks, return_exceptions=True)
        for dom, path in zip(domains, generated):
            if isinstance(path, str):
                results[dom] = path
                audio["domains"][dom] = {"status": "available", "reason": "Same-episode audio generated."}
    return results

async def generate_episode_podcast_audio(episode_data: Dict[str, Any], output_dir: str) -> Tuple[Optional[str], List[Dict[str, Any]], str, int]:
    episode_id = episode_data.get("id", "ep-142")
    os.makedirs(output_dir, exist_ok=True)
    final_mp3_path = os.path.join(output_dir, f"{episode_id}.mp3")

    audio = episode_data.setdefault("audio_availability", {})
    audio["podcast"] = {"status": "unavailable", "reason": "Audio generation failed or returned no playable track."}
    raw_chapters = episode_data.get("chapters", [])
    coverage = episode_data.get("content_availability")
    allowed = set(active_domains(coverage)) if coverage is not None else None
    if allowed is not None:
        raw_chapters = [c for c in raw_chapters if isinstance(c, dict) and c.get("domain") in allowed]
        if not raw_chapters:
            audio["podcast"]["reason"] = "No article narration available."
            return None, [], "00:00", 0
    existing_duration = episode_data.get("duration", "05:20")
    existing_seconds = episode_data.get("total_seconds", 320)

    # If audio already exists and is valid, calculate duration if needed
    if os.path.exists(final_mp3_path) and os.path.getsize(final_mp3_path) > 10000:
        actual_secs = int(get_audio_duration_seconds(final_mp3_path))
        if actual_secs > 0:
            existing_seconds = actual_secs
            existing_duration = format_seconds_to_time(actual_secs)
        logger.info(f"Audio file for {episode_id} already exists at {final_mp3_path} ({existing_duration})")
        if actual_secs > 0:
            audio["podcast"] = {"status": "available", "reason": "Same-episode audio available."}
            return final_mp3_path, raw_chapters, existing_duration, existing_seconds

    logger.info(f"Generating multi-host neural audio briefing for {episode_id} via Edge-TTS...")
    script_segments = episode_data.get("script_segments", [])
    
    if allowed is not None:
        script_segments = [s for s in script_segments if isinstance(s, dict) and s.get("domain") in allowed and s.get("text")]
        if not script_segments:
            return None, raw_chapters, "00:00", 0

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

    with tempfile.TemporaryDirectory(dir=output_dir, prefix=".tts-") as temp_dir:
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
                if seg_duration <= 0:
                    return None, raw_chapters, "00:00", 0

                chap_title = seg.get("chapter_title")
                if chap_title and chap_title not in seen_chapters:
                    seen_chapters.add(chap_title)
                    base_meta = chapter_map.get(chap_title, {})
                    start_sec = int(cumulative_seconds)
                    dynamic_chapters.append({
                        **({"domain": base_meta["domain"]} if "domain" in base_meta else {}),
                        "time": format_seconds_to_time(start_sec),
                        "seconds": start_sec,
                        "title": chap_title,
                        "source_name": base_meta.get("source_name", "Primary Source"),
                        "source_url": base_meta.get("source_url", "")
                    })

                cumulative_seconds += seg_duration
            except Exception as e:
                logger.error(f"Error generating TTS segment {idx} ({speaker}): {e}")
                return None, raw_chapters, "00:00", 0

        # If dynamic_chapters is empty, fallback to raw_chapters with distributed offsets
        if not dynamic_chapters:
            dynamic_chapters = raw_chapters

        staged_path = os.path.join(temp_dir, "episode.mp3")
        # Concatenate segment files into final MP3 using ffmpeg or binary concat
        if segment_files:
            try:
                concat_list_path = os.path.join(temp_dir, "concat_list.txt")
                with open(concat_list_path, "w") as f:
                    for sf in segment_files:
                        f.write(f"file '{sf}'\n")
                
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", staged_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError("Audio concatenation failed")
                logger.info(f"Successfully synthesized episode MP3 via ffmpeg at {final_mp3_path}")
            except Exception as e:
                logger.warning(f"ffmpeg concatenation failed: {e}. Using raw stream copy.")
                with open(staged_path, "wb") as outfile:
                    for sf in segment_files:
                        with open(sf, "rb") as infile:
                            shutil.copyfileobj(infile, outfile)

        if not os.path.exists(staged_path) or os.path.getsize(staged_path) <= 1000:
            return None, raw_chapters, "00:00", 0
        if get_audio_duration_seconds(staged_path) <= 0:
            return None, raw_chapters, "00:00", 0
        os.replace(staged_path, final_mp3_path)

    total_secs = int(get_audio_duration_seconds(final_mp3_path)) if os.path.exists(final_mp3_path) else int(cumulative_seconds)
    duration_str = format_seconds_to_time(total_secs) if total_secs > 0 else "00:00"
    audio["podcast"] = {"status": "available", "reason": "Same-episode audio generated."}

    return final_mp3_path, dynamic_chapters, duration_str, total_secs


# The work deadline leaves time for renderer/process cancellation and staging cleanup.
AUDIO_BUDGET_SECONDS = 60.0
AUDIO_CLEANUP_RESERVE_SECONDS = 2.0


@dataclass
class StoryAudioBundle:
    podcast_path: str | None
    domain_paths: dict[str, str]
    chapters: list[dict]
    duration: str
    total_seconds: int
    availability: dict
    recipe_fingerprint: str


class _AudioCleanupError(RuntimeError):
    """Resource cleanup failed; consumers must not stamp a successful bundle."""


async def _await_audio_task(task: asyncio.Task, deadline: float):
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError('Audio deadline exhausted')
    done, _ = await asyncio.wait({task}, timeout=remaining)
    if not done:
        raise TimeoutError('Audio deadline exhausted')
    return task.result()


async def _cancel_audio_tasks(tasks: list[asyncio.Task], deadline: float) -> None:
    """Own real coroutines through bounded cleanup, including delayed cancellation."""
    pending = {task for task in tasks if not task.done()}
    for task in pending:
        task.cancel()
    if pending:
        _, pending = await asyncio.wait(pending, timeout=max(
            0, deadline - asyncio.get_running_loop().time()))
    # Retrieve failures even when no caller will await the timed-out operation.
    for task in tasks:
        if task.done() and not task.cancelled():
            task.exception()
        elif not task.done():
            task.cancel()
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    if pending:
        raise _AudioCleanupError('Audio resource cancellation did not finish within its budget')


async def _run_audio_cleanup(operation, deadline: float) -> bool:
    """Finish owned cleanup even if the caller sends cancellation again."""
    task = asyncio.create_task(operation)
    cancelled = False
    try:
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                task.cancel()
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                raise _AudioCleanupError('Audio cleanup deadline exhausted')
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    except _AudioCleanupError as cleanup_failure:
        if cancelled:
            # Cancellation is the control-flow outcome even when cleanup fails.
            # The cause contains only fixed application-owned diagnostics.
            raise asyncio.CancelledError from cleanup_failure
        raise
    return cancelled


async def _concat_mp3(segment_paths: list[str], output_path: str, deadline: float) -> None:
    """Concatenate generated local basenames via FFmpeg; never copy raw streams."""
    output = Path(output_path).absolute()
    staging = output.parent
    safe_name = re.compile(r'[A-Za-z0-9_-]+\.(?:mp3|txt)')
    paths = [Path(path).absolute() for path in segment_paths]
    if (not paths or not safe_name.fullmatch(output.name)
            or any(path.parent != staging or not safe_name.fullmatch(path.name) for path in paths)):
        raise ValueError('Concat requires safe staging-local audio names')
    fd, list_path = tempfile.mkstemp(prefix='concat-', suffix='.txt', dir=staging)
    process = None
    spawn = communication = None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as listing:
            for path in paths:
                listing.write(f"file '{path.name}'\n")
        work_deadline = deadline - AUDIO_CLEANUP_RESERVE_SECONDS
        if asyncio.get_running_loop().time() >= work_deadline:
            raise TimeoutError('Audio deadline exhausted')
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            'ffmpeg', '-y', '-f', 'concat', '-safe', '1', '-i', Path(list_path).name,
            '-c', 'copy', output.name, cwd=str(staging),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        process = await _await_audio_task(spawn, work_deadline)
        communication = asyncio.create_task(process.communicate())
        await _await_audio_task(communication, work_deadline)
        if process.returncode != 0:
            raise RuntimeError('Audio concatenation failed')
    except BaseException as failure:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        async def cleanup():
            nonlocal process
            # Keep the actual spawn alive until its result can be killed. Caller
            # cancellation cannot interrupt this separate, explicitly owned task.
            if process is None and spawn is not None:
                try:
                    process = await _await_audio_task(spawn, deadline)
                except (Exception, asyncio.CancelledError):
                    pass
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if process is not None:
                waiter = asyncio.create_task(process.wait())
                try:
                    await _await_audio_task(waiter, deadline)
                except (Exception, asyncio.CancelledError):
                    pass
                await _cancel_audio_tasks([waiter], deadline)
            await _cancel_audio_tasks([t for t in (spawn, communication) if t is not None], deadline)
        try:
            cancelled = await _run_audio_cleanup(cleanup(), deadline)
        except _AudioCleanupError as cleanup_failure:
            if isinstance(failure, asyncio.CancelledError):
                raise failure from cleanup_failure
            raise
        if cancelled:
            raise asyncio.CancelledError
        raise
    finally:
        Path(list_path).unlink(missing_ok=True)


def _verified_audio_duration(path: str) -> float:
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise ValueError('Missing or empty audio output')
    seconds = get_audio_duration_seconds(path)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Audio output has no measured duration')
    return float(seconds)


async def generate_story_audio_bundle(episode_data: dict, output_dir: str) -> StoryAudioBundle:
    """Render one verified recipe, sharing its segments across complete media outputs."""
    from src.story_manifest import (validate_story_episode, manifest_from_dict,
                                    audio_recipe_fingerprint)
    valid, reason = validate_story_episode(episode_data)
    if not valid:
        raise ValueError(reason)
    episode_id = episode_data.get('id')
    if not isinstance(episode_id, str) or not re.fullmatch(r'ep-[0-9]{1,20}', episode_id):
        raise ValueError('Invalid audio episode identity')
    manifest = manifest_from_dict(episode_data['story_manifest'])
    segments = [dict(segment) for segment in episode_data['script_segments']]
    identity_chapters = [dict(chapter) for chapter in episode_data['chapters']]
    if len(segments) > 48:
        raise ValueError('Too many story segments')
    availability = {'podcast': {'status': 'unavailable', 'reason': 'Audio generation failed or returned no playable track.'},
                    'domains': {domain: {'status': 'unavailable', 'reason': 'No article narration available.'}
                                for domain in DOMAIN_ORDER}}
    bundle = StoryAudioBundle(None, {}, [], '00:00', 0, availability,
                              audio_recipe_fingerprint(manifest, VOICE_MAP))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AUDIO_BUDGET_SECONDS
    work_deadline = deadline - AUDIO_CLEANUP_RESERVE_SECONDS
    os.makedirs(output_dir, exist_ok=True)
    staging = tempfile.mkdtemp(dir=output_dir, prefix='.story-tts-')
    semaphore = asyncio.Semaphore(4)
    owned = []
    results = [None] * len(segments)

    async def render(index, segment):
        async with semaphore:
            if loop.time() >= work_deadline:
                return
            path = str(Path(staging) / f'segment-{index:03d}.mp3')
            try:
                await generate_segment_audio(segment['text'], VOICE_MAP[segment['speaker']], path)
                seconds = _verified_audio_duration(path)
                if loop.time() < work_deadline:
                    results[index] = (path, seconds)
            except Exception:
                # Failure is availability, never guessed narration or a stale path.
                return

    async def assemble(indices, basename):
        if loop.time() >= work_deadline or any(results[index] is None for index in indices):
            return None
        path = str(Path(staging) / basename)
        task = asyncio.create_task(_concat_mp3([results[index][0] for index in indices], path, deadline))
        owned.append(task)
        try:
            await _await_audio_task(task, work_deadline)
            duration = _verified_audio_duration(path)
            if loop.time() >= work_deadline:
                return None
            final = str(Path(output_dir) / basename)
            os.replace(path, final)
            return final, duration
        except _AudioCleanupError:
            raise
        except Exception:
            return None

    cancellation = None
    try:
        renders = [asyncio.create_task(render(index, segment)) for index, segment in enumerate(segments)]
        owned.extend(renders)
        if renders and loop.time() < work_deadline:
            await asyncio.wait(renders, timeout=max(0, work_deadline - loop.time()))
        for domain in DOMAIN_ORDER:
            indices = [index for index, segment in enumerate(segments) if segment['domain'] == domain]
            if not indices:
                continue
            availability['domains'][domain]['reason'] = 'Audio generation failed or returned no playable track.'
            assembled = await assemble(indices, f'{episode_id}-{domain}.mp3')
            if assembled:
                bundle.domain_paths[domain] = assembled[0]
                availability['domains'][domain] = {'status': 'available', 'reason': 'Same-episode audio generated.'}
        assembled = await assemble(list(range(len(segments))), f'{episode_id}.mp3')
        if assembled:
            bundle.podcast_path = assembled[0]
            bundle.total_seconds = int(assembled[1])
            bundle.duration = format_seconds_to_time(bundle.total_seconds)
            availability['podcast'] = {'status': 'available', 'reason': 'Same-episode audio generated.'}
            cumulative = 0.0
            chapter_by_id = {chapter['story_id']: chapter for chapter in identity_chapters}
            seen = set()
            for segment, result in zip(segments, results):
                story_id = segment['story_id']
                if story_id not in seen:
                    seen.add(story_id)
                    bundle.chapters.append({**chapter_by_id[story_id], 'seconds': cumulative,
                                            'time': format_seconds_to_time(int(cumulative))})
                cumulative += result[1]
        return bundle
    except asyncio.CancelledError as failure:
        cancellation = failure
        raise
    finally:
        if loop.time() >= deadline:
            for task in owned:
                if not task.done():
                    task.cancel()
        try:
            cancelled = await _run_audio_cleanup(_cancel_audio_tasks(owned, deadline), deadline)
            if cancelled:
                raise asyncio.CancelledError
        except _AudioCleanupError as cleanup_failure:
            if cancellation is not None:
                raise cancellation from cleanup_failure
            raise
        finally:
            shutil.rmtree(staging)

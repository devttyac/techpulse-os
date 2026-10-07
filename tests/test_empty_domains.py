"""Empty RSS coverage must not create unsupported content or media."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import synthesizer, grounded_chat, tts_engine
from src.provenance import selection_record

DOMAINS = ['ai', 'cloud', 'data', 'sec', 'devops', 'arch', 'finops', 'gov']

def corpus(active=DOMAINS):
    return {d: ([{'domain': d, 'title': f'{d} release', 'summary': f'{d} summary evidence.',
                 'source_name': f'{d} source', 'url': f'https://example.test/{d}',
                 'published_at': '2026-10-07'}] if d in active else []) for d in DOMAINS}


class EmptyDomainTests(unittest.IsolatedAsyncioTestCase):
    def test_each_empty_domain_omits_chapters_scripts_cards_and_sources(self):
        for missing in DOMAINS:
            with self.subTest(missing=missing):
                active = [d for d in DOMAINS if d != missing]
                episode = synthesizer.generate_deterministic_fallback(corpus(active), 1)
                self.assertEqual([c.get('domain') for c in episode['chapters']], active)
                self.assertNotIn(missing, [s.get('domain') for s in episode['script_segments']])
                self.assertNotIn(missing, [c.get('domain') for c in episode['flashcards']])
                takeaway = episode['takeaways'][missing]
                self.assertEqual(takeaway['status'], 'no_received_candidates')
                self.assertEqual(takeaway['bullets'], [])
                self.assertEqual(takeaway['sources'], [])
                self.assertTrue(takeaway['reason'])
                self.assertTrue(synthesizer.validate_synthesis(episode)[0])

    def test_sparse_fallback_narrates_and_cards_only_received_summaries(self):
        for active in (['ai', 'data', 'gov'], ['gov']):
            episode = synthesizer.generate_deterministic_fallback(corpus(active), 2)
            self.assertEqual([c.get('domain') for c in episode['chapters']], active)
            self.assertEqual([c.get('domain') for c in episode['flashcards']], active)
            self.assertEqual([c['answer'] for c in episode['flashcards']], [f'{d} summary evidence.' for d in active])
            self.assertEqual(episode.get('fallback_content'), 'source_derived')
            self.assertEqual(episode.get('content_basis'), 'rss_summaries')
            for segment in episode['script_segments']:
                self.assertIn(f"{segment['domain']} summary evidence.", segment['text'])
            self.assertTrue(synthesizer.validate_synthesis(episode)[0])

    async def test_all_empty_creates_no_episode_and_no_provider_attempt(self):
        self.assertIsNone(synthesizer.generate_deterministic_fallback(corpus([]), 3))
        stats = {}
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-placeholder'}):
            result = await synthesizer.synthesize_briefing(corpus([]), 3, diagnostics=stats)
        self.assertIsNone(result)
        self.assertEqual(stats['path'], 'no_content')
        self.assertEqual(stats['attempts'], [])

    def test_no_summary_does_not_create_invented_flashcard_answer(self):
        received = corpus(['ai'])
        received['ai'][0]['summary'] = ''
        episode = synthesizer.generate_deterministic_fallback(received, 4)
        self.assertEqual(episode['flashcards'], [])
        self.assertIn('ai release', episode['script_segments'][0]['text'])
        self.assertEqual(episode['takeaways']['ai']['interview_framing'], '')

    def test_middle_gap_keeps_citation_in_correct_domain(self):
        received = corpus(['ai', 'data', 'gov'])
        episode = synthesizer.generate_deterministic_fallback(received, 5)
        record = selection_record(received, episode, 'deterministic_fallback')
        self.assertIsNone(record['cloud']['chapter_citation'])
        self.assertEqual(record['data']['chapter_citation']['url'], 'https://example.test/data')
        episode['chapters'][1]['source_url'] = 'https://fabricated.test/data'
        repaired, count = synthesizer.enforce_corpus_urls(episode, received)
        self.assertEqual(count, 1)
        self.assertEqual(repaired['chapters'][1]['source_url'], 'https://example.test/data')

    def test_validator_rejects_inactive_content_and_mismatched_domain_join(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['ai', 'data']), 6)
        bad = copy.deepcopy(episode)
        bad['script_segments'][0]['domain'] = 'data'
        self.assertFalse(synthesizer.validate_synthesis(bad)[0])
        bad = copy.deepcopy(episode)
        bad['flashcards'].append({'domain': 'cloud', 'question': 'q', 'answer': 'invented', 'cite': 'fake', 'color_class': ''})
        self.assertFalse(synthesizer.validate_synthesis(bad)[0])
        bad = copy.deepcopy(episode)
        bad['takeaways']['cloud']['bullets'] = ['invented']
        self.assertFalse(synthesizer.validate_synthesis(bad)[0])

    async def test_provider_with_partial_evidence_accepts_one_card_and_gets_active_prompt(self):
        received = corpus(['gov'])
        payload = synthesizer.generate_deterministic_fallback(received, 7)
        requests = []
        class Models:
            def generate_content(self, **kwargs):
                requests.append(kwargs)
                return types.SimpleNamespace(text=json.dumps(payload))
        google = types.ModuleType('google')
        google.genai = types.ModuleType('google.genai')
        google.genai.Client = lambda **kwargs: types.SimpleNamespace(models=Models())
        with patch.dict(sys.modules, {'google': google, 'google.genai': google.genai}), patch.dict(os.environ, {'GEMINI_API_KEY': 'test-placeholder'}):
            stats = {}
            result = await synthesizer.synthesize_briefing(received, 7, diagnostics=stats)
        self.assertEqual(stats['path'], 'llm')
        self.assertEqual(len(result['flashcards']), 1)
        self.assertIn('AVAILABLE DOMAIN IDS: gov', requests[0]['contents'])
        self.assertNotIn('### DOMAIN: AI', requests[0]['contents'])
        self.assertNotIn('fallback_content', result)

    async def test_partial_provider_failure_uses_source_derived_fallback(self):
        received = corpus(['data'])
        class Models:
            def generate_content(self, **kwargs):
                raise ConnectionError('offline')
        google = types.ModuleType('google')
        google.genai = types.ModuleType('google.genai')
        google.genai.Client = lambda **kwargs: types.SimpleNamespace(models=Models())
        stats = {}
        with patch.dict(sys.modules, {'google': google, 'google.genai': google.genai}), patch.dict(os.environ, {'GEMINI_API_KEY': 'test-placeholder'}):
            result = await synthesizer.synthesize_briefing(received, 8, diagnostics=stats)
        self.assertEqual(stats['fallback_reason'], 'attempts_exhausted')
        self.assertEqual(result['flashcards'][0]['answer'], 'data summary evidence.')

    def test_status_only_chat_and_interview_return_insufficient_evidence(self):
        episode = {'id': 'ep-9', 'title': 'Empty', 'takeaways': {'ai': {'status': 'no_received_candidates', 'reason': 'No articles available', 'bullets': []}}, 'full_articles': {}}
        for query in ('hello', 'overview', 'interview challenge', 'explain agent'):
            response = grounded_chat.dynamic_rag_synthesize(query, episode)
            self.assertIn('insufficient', response.lower())
            self.assertNotIn('deterministic', response.lower())

    async def test_empty_live_chat_does_not_call_provider(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test-placeholder'}), patch.object(grounded_chat, 'call_gemini_llm', side_effect=AssertionError('No evidence must not call provider')):
            result = await grounded_chat.process_grounded_chat('interview', {'id': 'ep-9', 'takeaways': {}, 'full_articles': {}})
        self.assertEqual(result['grounded_episode_id'], 'ep-9')
        self.assertIn('insufficient', result['response'].lower())

    def test_partial_chat_stays_inside_evidence_and_explains_missing_domain(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['data']), 10)
        self.assertIn('No articles available', grounded_chat.dynamic_rag_synthesize('cloud', episode))
        response = grounded_chat.dynamic_rag_synthesize('data', episode)
        self.assertIn('data summary evidence.', response)
        self.assertNotIn('non-deterministic execution', response)

    async def test_domain_tts_omits_empty_domain_and_marks_provider_failure(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['data']), 11)
        async def unavailable(*args):
            raise ConnectionError('offline')
        with tempfile.TemporaryDirectory() as directory, patch.object(tts_engine, 'generate_segment_audio', unavailable):
            paths = await tts_engine.generate_all_domain_audios(episode, directory)
            self.assertEqual(paths, {})
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(episode['audio_availability']['domains']['cloud']['status'], 'unavailable')
        self.assertEqual(episode['audio_availability']['domains']['data']['status'], 'unavailable')

    async def test_podcast_failure_returns_no_playable_path(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['data']), 12)
        async def unavailable(*args):
            raise ConnectionError('offline')
        with tempfile.TemporaryDirectory() as directory, patch.object(tts_engine, 'generate_segment_audio', unavailable):
            path, chapters, duration, seconds = await tts_engine.generate_episode_podcast_audio(episode, directory)
            self.assertIsNone(path)
            self.assertEqual(seconds, 0)
            self.assertEqual(duration, '00:00')
        self.assertEqual(episode['audio_availability']['podcast']['status'], 'unavailable')

class CoverageContractTests(unittest.TestCase):
    def test_quiet_failed_mixed_and_available_domains_have_distinct_counts(self):
        from src.content_availability import build_content_availability, active_domains
        diagnostics = {'feeds': [
            {'domain': 'ai', 'outcome': 'failed'},
            {'domain': 'cloud', 'outcome': 'success'},
            {'domain': 'data', 'outcome': 'failed'},
            {'domain': 'data', 'outcome': 'success'},
            {'domain': 'gov', 'outcome': 'failed'},
        ]}
        value = build_content_availability(corpus(['gov']), diagnostics)
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(list(value['domains']), DOMAINS)
        self.assertEqual(value['domains']['ai']['status'], 'source_unavailable')
        self.assertEqual(value['domains']['cloud']['status'], 'no_received_candidates')
        self.assertEqual(value['domains']['data']['status'], 'no_received_candidates')
        self.assertEqual(value['domains']['gov']['status'], 'available')
        self.assertEqual(value['domains']['gov']['candidate_count'], 1)
        self.assertEqual(active_domains(value), ['gov'])

class AdditionalContractTests(unittest.IsolatedAsyncioTestCase):
    def test_malformed_coverage_is_rejected_without_crashing(self):
        from src.content_availability import active_domains
        episode = synthesizer.generate_deterministic_fallback(corpus(['ai']), 13)
        bad_values = [True, 2, None]
        for version in bad_values:
            bad = copy.deepcopy(episode)
            bad['content_availability']['schema_version'] = version
            self.assertEqual(active_domains(bad['content_availability']), [])
            self.assertFalse(synthesizer.validate_synthesis(bad)[0])
        bad = copy.deepcopy(episode)
        bad['content_availability']['domains']['ai']['candidate_count'] = True
        self.assertEqual(active_domains(bad['content_availability']), [])
        self.assertFalse(synthesizer.validate_synthesis(bad)[0])

    def test_nontext_coverage_status_is_unknown_without_crashing(self):
        from src.content_availability import active_domains
        for status in ([], {}, None):
            episode = synthesizer.generate_deterministic_fallback(corpus(['ai']), 20)
            episode['content_availability']['domains']['ai']['status'] = status
            self.assertEqual(active_domains(episode['content_availability']), [])
            self.assertFalse(synthesizer.validate_synthesis(episode)[0])

    def test_malformed_chapter_and_takeaway_types_are_cleanly_rejected(self):
        for field, value in [('chapters', [None]), ('takeaways', {'ai': None})]:
            bad = synthesizer.generate_deterministic_fallback(corpus(['ai']), 14)
            bad[field] = value
            self.assertFalse(synthesizer.validate_synthesis(bad)[0])

    async def test_generated_domain_media_has_only_same_episode_active_path(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['data']), 15)
        async def save_audio(text, voice, destination):
            Path(destination).write_bytes(b'fixture-audio' * 200)
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / 'article-cloud.mp3'
            stale.write_bytes(b'old historical track')
            with patch.object(tts_engine, 'generate_segment_audio', save_audio), patch.object(tts_engine, 'MP3', lambda path: types.SimpleNamespace(info=types.SimpleNamespace(length=5.0))):
                paths = await tts_engine.generate_all_domain_audios(episode, directory)
            self.assertEqual(set(paths), {'data'})
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['article-cloud.mp3', 'ep-15-data.mp3'])
            self.assertEqual(stale.read_bytes(), b'old historical track')
        self.assertEqual(episode['audio_availability']['domains']['data']['status'], 'available')
        self.assertEqual(episode['audio_availability']['domains']['cloud']['status'], 'unavailable')

    def test_legacy_eight_position_provenance_remains_supported(self):
        received = corpus()
        legacy = {'chapters': [{'source_url': f'https://example.test/{d}'} for d in DOMAINS]}
        record = selection_record(received, legacy, 'llm')
        self.assertEqual(record['data']['chapter_citation']['url'], 'https://example.test/data')
        ambiguous = {'chapters': legacy['chapters'][:3]}
        self.assertIsNone(selection_record(received, ambiguous, 'llm')['data']['chapter_citation'])


class MalformedResponseTests(unittest.TestCase):
    def test_unhashable_segment_title_is_cleanly_rejected(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['ai']), 16)
        episode['script_segments'][0]['chapter_title'] = []
        self.assertFalse(synthesizer.validate_synthesis(episode)[0])

    def test_nontext_active_fields_are_cleanly_rejected(self):
        for field, bad_value in [('title', {}), ('bullets', [None]), ('sources', [None]), ('interview_framing', {})]:
            episode = synthesizer.generate_deterministic_fallback(corpus(['ai']), 17)
            episode['takeaways']['ai'][field] = bad_value
            self.assertFalse(synthesizer.validate_synthesis(episode)[0])


class PodcastJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_dynamic_chapters_keep_domain_ids_after_json_roundtrip(self):
        episode = json.loads(json.dumps(synthesizer.generate_deterministic_fallback(corpus(['ai', 'data']), 18)))
        async def save_audio(text, voice, destination):
            Path(destination).write_bytes(b'fixture-audio' * 200)
        def audio_metadata(path):
            return types.SimpleNamespace(info=types.SimpleNamespace(length=Path(path).stat().st_size / 2600 * 4.5))
        with tempfile.TemporaryDirectory() as directory, patch.object(tts_engine, 'generate_segment_audio', save_audio), patch.object(tts_engine, 'MP3', audio_metadata), patch.object(tts_engine.asyncio, 'create_subprocess_exec', side_effect=OSError('offline boundary')):
            path, chapters, duration, seconds = await tts_engine.generate_episode_podcast_audio(episode, directory)
            self.assertEqual(Path(path).name, 'ep-18.mp3')
            self.assertEqual([c['domain'] for c in chapters], ['ai', 'data'])
            self.assertEqual([c['source_url'] for c in chapters], ['https://example.test/ai', 'https://example.test/data'])
            self.assertEqual([c['seconds'] for c in chapters], [0, 4])
            self.assertEqual(seconds, 9)
            self.assertEqual(duration, '00:09')
        self.assertEqual(episode['audio_availability']['podcast']['status'], 'available')

    async def test_invalid_audio_bytes_are_not_published_as_playable_media(self):
        episode = synthesizer.generate_deterministic_fallback(corpus(['data']), 19)
        async def invalid_audio(text, voice, destination):
            Path(destination).write_bytes(b'invalid audio' * 200)
        with tempfile.TemporaryDirectory() as directory, patch.object(tts_engine, 'generate_segment_audio', invalid_audio), patch.object(tts_engine.asyncio, 'create_subprocess_exec', side_effect=OSError('offline boundary')):
            path, chapters, duration, seconds = await tts_engine.generate_episode_podcast_audio(episode, directory)
            self.assertIsNone(path)
            self.assertEqual(list(Path(directory).iterdir()), [])
            paths = await tts_engine.generate_all_domain_audios(episode, directory)
            self.assertEqual(paths, {})
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()

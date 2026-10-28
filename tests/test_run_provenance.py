"""Exercise actual feed/model collectors against offline boundary doubles."""
import asyncio
import copy
import inspect
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ["API_SECRET_KEY"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import ingestion, synthesizer
from src.acquisition.tldr import discover_from_fixtures


class FeedClient:
    def __init__(self, status=200, text="", error=None):
        self.status, self.text, self.error = status, text, error

    async def get(self, *args, **kwargs):
        if self.error:
            raise self.error
        return types.SimpleNamespace(status_code=self.status, text=self.text)


def rss():
    return '<rss version="2.0"><channel><title>Fixture</title><item><title>Good</title><link>https://example.test/good</link></item><item><title>Bad</title><link>javascript:bad</link></item><item><title>No link</title></item></channel></rss>'


def fixture():
    corpus = {d: [{"title": d + " item", "url": "https://example.test/" + d, "source_name": "Fixture", "summary": "Summary"}] for d in synthesizer.DOMAIN_ORDER}
    payload = synthesizer.generate_deterministic_fallback(corpus, 10)
    return corpus, payload


class RunProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_discovery_records_reason_without_article_content(self):
        stats = {}
        corpus = await ingestion.ingest_all_domains(diagnostics=stats)
        self.assertTrue(all(not rows for rows in corpus.values()))
        self.assertEqual(stats["discovery"]["status"], "disabled")
        self.assertEqual(stats["discovery"]["reason"], "tldr_interface_and_discovery_use_unverified")
        self.assertNotIn("https://example.test", json.dumps(stats))

    async def test_malformed_issue_differs_from_valid_empty(self):
        base = {"edition_id":"ai", "issue_id":"2026-10-28",
                "issue_url":"https://tldr.tech/ai/issues/2026-10-28"}
        malformed = discover_from_fixtures({"ai":{**base, "links":None}})
        valid_empty = discover_from_fixtures({"ai":{**base, "links":[]}})
        self.assertEqual(malformed["editions"][0]["outcome"], "parse_error")
        self.assertEqual(valid_empty["editions"][0]["outcome"], "valid_empty")

    def test_discovery_audit_has_counts_and_issue_identity_without_candidate_text(self):
        from src.provenance import discovery_record
        base = {"edition_id":"ai", "issue_id":"2026-10-28",
                "issue_url":"https://tldr.tech/ai/issues/2026-10-28",
                "links":[{"url":"https://publisher.example/article?secret=SECRET_SENTINEL",
                          "title":"SECRET_SENTINEL headline", "placement":"editorial"}]}
        result = discover_from_fixtures({"ai":base})
        record = discovery_record(result)
        self.assertEqual(record["catalogue_revision"],1)
        self.assertEqual(record["editions"][0]["issue_id"],"2026-10-28")
        self.assertEqual(record["editions"][0]["candidates"],1)
        self.assertEqual(record["candidate_count"],1)
        self.assertNotIn("SECRET_SENTINEL",json.dumps(record))
        self.assertNotIn("publisher.example",json.dumps(record))

    async def synthesize(self, responses, stats):
        corpus = {'ai': [{'title': 'Fixture story', 'summary': 'Fixture summary.',
                          'source_name': 'Fixture', 'url': 'https://example.test/story'}]}
        from src.story_manifest import freeze_story_manifest
        from src.content_availability import build_content_availability
        manifest = freeze_story_manifest(corpus, build_content_availability(corpus))
        payload = {'story_id': manifest.stories[0].story_id, 'unit_ids': ['title-0', 'summary-0']}
        queue = list(responses(payload))
        class Models:
            async def generate_content(self, **kwargs):
                result = queue.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return types.SimpleNamespace(text=json.dumps(result) if isinstance(result, dict) else result)
        class Aio:
            models = Models()
            async def aclose(self):
                pass
        with patch('google.genai.Client', return_value=types.SimpleNamespace(aio=Aio())), patch.dict(
                os.environ, {'GEMINI_API_KEY': 'fixture-placeholder', 'GEMINI_MODEL': 'test-model'}):
            return await synthesizer.synthesize_briefing(corpus, 10, diagnostics=stats)

    async def test_schema_failure_and_success_share_two_attempt_budget(self):
        stats = {}
        result = await self.synthesize(lambda payload: [ValueError('response_schema unsupported'), payload], stats)
        self.assertEqual(stats['path'], 'model_assisted_selection')
        self.assertEqual(stats['model'], 'gemini-3.6-flash')
        self.assertEqual(stats['schema_variant'], 'evidence_ids_json')
        self.assertEqual([a['outcome'] for a in stats['attempts']], ['schema_rejected', 'accepted'])
        self.assertEqual(stats['stories'][0]['attempts'], 2)
        self.assertTrue(synthesizer.validate_synthesis(result)[0])

    async def test_transport_invalid_json_and_validation_are_distinct(self):
        stats = {}
        await self.synthesize(lambda payload: [TimeoutError('SECRET_SENTINEL https://provider.test/?key=SECRET_SENTINEL'), 'not-json'], stats)
        self.assertEqual([a['outcome'] for a in stats['attempts']], ['transport_error', 'invalid_json'])
        other = {}
        await self.synthesize(lambda payload: [{**payload, 'unit_ids': ['foreign']}, payload], other)
        self.assertEqual([a['outcome'] for a in other['attempts']], ['validation_rejected', 'accepted'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(stats))
        self.assertNotIn('provider.test', json.dumps(stats))

    async def test_url_prose_rejected_as_selection_validation_without_repair(self):
        stats = {}
        result = await self.synthesize(lambda payload: [{**payload, 'source_url': 'https://fabricated.test/x'}, payload], stats)
        self.assertEqual([a['outcome'] for a in stats['attempts']], ['validation_rejected', 'accepted'])
        self.assertEqual(stats['url_substitutions'], 0)
        self.assertEqual(result['chapters'][0]['source_url'], 'https://example.test/story')
        self.assertNotIn('fabricated.test', json.dumps(stats))

    async def test_nonobject_json_is_classified_as_validation_rejection(self):
        stats = {}
        await self.synthesize(lambda payload: ['[]', payload], stats)
        self.assertEqual([a['outcome'] for a in stats['attempts']], ['validation_rejected', 'accepted'])

    def test_additive_diagnostics_preserve_run_schema_and_legacy_selection(self):
        from src.provenance import RunRecord, selection_record
        record = RunRecord().data
        self.assertEqual(record['schema_version'], 1)
        self.assertIn('attempts', record['synthesis'])
        corpus = {'ai': [{'url': 'https://example.test/legacy?token=hidden'}]}
        legacy = {'chapters': [{'domain': 'ai', 'source_url': corpus['ai'][0]['url']}]}
        selected = selection_record(corpus, legacy, 'llm')
        self.assertEqual(selected['ai']['chapter_citation']['url'], 'https://example.test/legacy')
        self.assertNotIn('stories', selected['ai'])

    def test_known_environment_secrets_never_enter_model_or_article_identifiers(self):
        from src.provenance import model_identifier, article_identity
        with patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET_SENTINEL"}):
            model = model_identifier("SECRET_SENTINEL")
            article = article_identity("https://example.test/SECRET_SENTINEL?key=SECRET_SENTINEL")
        self.assertNotIn("SECRET_SENTINEL", json.dumps([model, article]))

    async def test_runtime_discovery_never_starts_an_http_client(self):
        stats = {}
        with patch("httpx.AsyncClient", side_effect=AssertionError("No newsletter request")):
            await ingestion.ingest_all_domains(diagnostics=stats)
        self.assertEqual(stats["discovery"]["status"], "disabled")
        self.assertNotIn("feeds", stats)

    async def test_auth_and_quota_errors_remain_distinct_in_attempt_history(self):
        stats = {}
        await self.synthesize(lambda payload: [ValueError("401 Unauthorized"), ValueError("429 quota exceeded")], stats)
        self.assertEqual([a["outcome"] for a in stats["attempts"]], ["auth_error", "quota_error"])

    async def test_no_key_and_exhaustion_have_different_fallback_reasons(self):
        corpus, _ = fixture()
        stats = {}
        kwargs = {"diagnostics": stats} if "diagnostics" in inspect.signature(synthesizer.synthesize_briefing).parameters else {}
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            await synthesizer.synthesize_briefing(corpus, 10, **kwargs)
        self.assertEqual(stats.get("fallback_reason"), "no_api_key")
        exhausted = {}
        await self.synthesize(lambda payload: [ConnectionError("secret")]*2, exhausted)
        self.assertEqual(exhausted["fallback_reason"], "attempts_exhausted")
        self.assertIsNone(exhausted["model"])


if __name__ == "__main__":
    unittest.main()

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import ingestion, synthesizer


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
    async def test_feed_records_rejections_without_persisting_raw_content(self):
        stats = {}
        kwargs = {"diagnostics": stats} if "diagnostics" in inspect.signature(ingestion.fetch_feed_items).parameters else {}
        items = await ingestion.fetch_feed_items(FeedClient(text=rss()), "ai", {"name": "Fixture", "url": "https://example.test/feed"}, **kwargs)
        self.assertEqual(len(items), 1)
        self.assertEqual(stats.get("entries_considered"), 3, "Feed result needs diagnostic counts")
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["rejected_links"], 1)
        self.assertEqual(stats["missing_fields"], 1)
        self.assertNotIn("javascript", json.dumps(stats))

    async def test_http_failure_differs_from_empty_success(self):
        failed, empty = {}, {}
        for client, stats in ((FeedClient(status=503), failed), (FeedClient(text='<rss version="2.0"><channel><title>Empty</title></channel></rss>'), empty)):
            kwargs = {"diagnostics": stats} if "diagnostics" in inspect.signature(ingestion.fetch_feed_items).parameters else {}
            await ingestion.fetch_feed_items(client, "ai", {"name": "Fixture", "url": "https://example.test/feed"}, **kwargs)
        self.assertEqual(failed.get("outcome"), "http_error")
        self.assertEqual(failed["http_status"], 503)
        self.assertEqual(empty["outcome"], "success")

    async def synthesize(self, responses, stats):
        corpus, payload = fixture()
        queue = list(responses(payload))
        class Models:
            def generate_content(self, **kwargs):
                result = queue.pop(0)
                if isinstance(result, BaseException):
                    raise result
                return types.SimpleNamespace(text=json.dumps(result) if isinstance(result, dict) else result)
        google = types.ModuleType("google")
        google.genai = types.ModuleType("google.genai")
        google.genai.Client = lambda **kwargs: types.SimpleNamespace(models=Models())
        kwargs = {"diagnostics": stats} if "diagnostics" in inspect.signature(synthesizer.synthesize_briefing).parameters else {}
        with patch.dict(sys.modules, {"google": google, "google.genai": google.genai}), patch.dict(os.environ, {"GEMINI_API_KEY": "fixture-placeholder", "GEMINI_MODEL": "test-model"}), patch.object(synthesizer, "_USE_DICT_SCHEMA", False):
            return await synthesizer.synthesize_briefing(corpus, 10, **kwargs)

    async def test_schema_probe_and_success_are_separate_provider_attempts(self):
        stats = {}
        await self.synthesize(lambda payload: [ValueError("response_schema unsupported"), payload], stats)
        self.assertEqual(stats.get("path"), "llm")
        self.assertEqual(stats["model"], "test-model")
        self.assertEqual(stats["schema_variant"], "json_schema_dict")
        self.assertEqual([a["outcome"] for a in stats["attempts"]], ["schema_rejected", "accepted"])

    async def test_transport_invalid_json_and_validation_are_distinct(self):
        stats = {}
        def responses(payload):
            bad = copy.deepcopy(payload)
            bad["chapters"] = bad["chapters"][:7]
            return [TimeoutError("SECRET_SENTINEL https://provider.test/?key=SECRET_SENTINEL"), "not-json", bad, payload]
        await self.synthesize(responses, stats)
        self.assertEqual([a["outcome"] for a in stats.get("attempts", [])], ["transport_error", "invalid_json", "validation_rejected", "accepted"])
        self.assertNotIn("SECRET_SENTINEL", json.dumps(stats))
        self.assertNotIn("provider.test", json.dumps(stats))

    async def test_url_rejection_is_not_provider_transport_failure(self):
        stats = {}
        def responses(payload):
            bad = copy.deepcopy(payload)
            for chapter in bad["chapters"][:3]:
                chapter["source_url"] = "https://fabricated.test/x"
            return [bad, payload]
        await self.synthesize(responses, stats)
        self.assertEqual([a["outcome"] for a in stats.get("attempts", [])], ["url_provenance_rejected", "accepted"])
        self.assertEqual(stats["attempts"][0]["url_substitutions"], 3)

    async def test_post_response_exception_is_not_left_as_returned_response(self):
        stats = {}
        await self.synthesize(lambda payload: ["[]", payload], stats)
        self.assertEqual(stats["attempts"][0]["outcome"], "postprocessing_failure")
        self.assertEqual(stats["attempts"][1]["outcome"], "accepted")

    def test_known_environment_secrets_never_enter_model_or_article_identifiers(self):
        from src.provenance import model_identifier, article_identity
        with patch.dict(os.environ, {"GEMINI_API_KEY": "SECRET_SENTINEL"}):
            model = model_identifier("SECRET_SENTINEL")
            article = article_identity("https://example.test/SECRET_SENTINEL?key=SECRET_SENTINEL")
        self.assertNotIn("SECRET_SENTINEL", json.dumps([model, article]))

    async def test_cancelled_ingestion_preserves_partial_counts_and_feed_identity(self):
        started = asyncio.Event()
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url, **kwargs):
                if "second" in url:
                    started.set()
                    await asyncio.Event().wait()
                return types.SimpleNamespace(status_code=200, text=rss())
        feeds = {"ai": [{"name": "First", "url": "https://example.test/first?token=SECRET_SENTINEL"},
                        {"name": "Second", "url": "https://example.test/second"}]}
        stats = {}
        with patch.object(ingestion, "DOMAIN_FEEDS", feeds), patch.object(ingestion.httpx, "AsyncClient", lambda **kwargs: Client()):
            task = asyncio.create_task(ingestion.ingest_all_domains(diagnostics=stats))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(stats["per_domain"].get("ai"), 1)
        self.assertEqual(stats["links_rejected"], 1)
        self.assertEqual(stats["feeds"][0]["name"], "First")
        self.assertEqual(stats["feeds"][0]["source"]["url"], "https://example.test/first")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(stats))

    async def test_auth_and_quota_errors_remain_distinct_in_attempt_history(self):
        stats = {}
        await self.synthesize(lambda payload: [ValueError("401 Unauthorized"), ValueError("429 quota exceeded"), payload], stats)
        self.assertEqual([a["outcome"] for a in stats["attempts"]], ["auth_error", "quota_error", "accepted"])

    async def test_no_key_and_exhaustion_have_different_fallback_reasons(self):
        corpus, _ = fixture()
        stats = {}
        kwargs = {"diagnostics": stats} if "diagnostics" in inspect.signature(synthesizer.synthesize_briefing).parameters else {}
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            await synthesizer.synthesize_briefing(corpus, 10, **kwargs)
        self.assertEqual(stats.get("fallback_reason"), "no_api_key")
        exhausted = {}
        await self.synthesize(lambda payload: [ConnectionError("secret")]*4, exhausted)
        self.assertEqual(exhausted["fallback_reason"], "attempts_exhausted")
        self.assertIsNone(exhausted["model"])


if __name__ == "__main__":
    unittest.main()

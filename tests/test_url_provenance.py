"""URL provenance tests (security finding F-03).

Run: python tests/test_url_provenance.py  (non-zero exit on any failure)

Covers unsafe feed-link rejection, retained legacy URL repair, and new
source-owned story citations that cannot be replaced by provider prose.
"""
import asyncio
import copy
import os
import sys
import types

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ["API_SECRET_KEY"] = ""

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from src import ingestion  # noqa: E402
from src import synthesizer  # noqa: E402
from src.synthesizer import DOMAIN_ORDER, enforce_corpus_urls  # noqa: E402

JS_URL = "javascript:alert(1)"
EVIL_URL = "https://evil.example/post"


def make_corpus(per_domain: int = 5, empty_domains=()):
    corpus = {}
    for d in DOMAIN_ORDER:
        if d in empty_domains:
            corpus[d] = []
            continue
        corpus[d] = [
            {
                "id": f"{d}{i}",
                "domain": d,
                "source_name": f"{d}-source-{i}",
                "title": f"{d} article {i}",
                "url": f"https://{d}.example.test/post-{i}",
                "summary": "s",
                "published_at": "2026-01-01",
            }
            for i in range(per_domain)
        ]
    return corpus


def make_payload(corpus):
    """A syntactically valid payload whose URLs all come from the corpus."""
    chapters = []
    takeaways = {}
    for i, d in enumerate(DOMAIN_ORDER):
        arts = corpus.get(d) or []
        chapters.append({
            "title": f"{i + 1}. Test {d}: finding",
            "source_name": arts[0]["source_name"] if arts else "",
            "source_url": arts[0]["url"] if arts else "",
        })
        takeaways[d] = {
            "badge": "B", "release_date": "Jan 2026", "title": "T", "bullets": ["x"],
            "interview_framing": "f",
            "sources": [{"title": a["title"], "url": a["url"]} for a in arts[:2]],
        }
    segments = [{"speaker": "Host A", "text": "x", "chapter_title": c["title"]} for c in chapters]
    return {"chapters": chapters, "takeaways": takeaways, "script_segments": segments}


def test_javascript_chapter_url_is_substituted_and_counted():
    corpus = make_corpus()
    payload = make_payload(corpus)
    payload["chapters"][0]["source_url"] = JS_URL
    out, count = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][0]["source_url"] == corpus["ai"][0]["url"], out["chapters"][0]
    assert count == 1, f"expected 1 substitution, got {count}"


def test_off_corpus_https_chapter_url_is_substituted():
    corpus = make_corpus()
    payload = make_payload(corpus)
    payload["chapters"][2]["source_url"] = EVIL_URL
    out, count = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][2]["source_url"] == corpus["data"][0]["url"]
    assert count == 1


def test_all_corpus_urls_returned_unchanged_with_zero_count():
    corpus = make_corpus()
    payload = make_payload(corpus)
    before = copy.deepcopy(payload)
    out, count = enforce_corpus_urls(payload, corpus)
    assert count == 0, f"expected 0 substitutions, got {count}"
    assert out == before, "payload with only corpus URLs must be unchanged"


def test_source_name_corrected_alongside_url():
    corpus = make_corpus()
    payload = make_payload(corpus)
    payload["chapters"][1]["source_url"] = EVIL_URL
    payload["chapters"][1]["source_name"] = "Totally Trusted Source"
    out, _ = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][1]["source_name"] == corpus["cloud"][0]["source_name"]
    assert out["chapters"][1]["source_url"] == corpus["cloud"][0]["url"]


def test_takeaway_sources_off_corpus_dropped_valid_kept():
    corpus = make_corpus()
    payload = make_payload(corpus)
    good = {"title": "good", "url": corpus["sec"][1]["url"]}
    payload["takeaways"]["sec"]["sources"] = [
        {"title": "bad js", "url": JS_URL},
        good,
        {"title": "bad https", "url": EVIL_URL},
    ]
    out, count = enforce_corpus_urls(payload, corpus)
    assert out["takeaways"]["sec"]["sources"] == [good], out["takeaways"]["sec"]["sources"]
    assert count == 0, "dropped takeaway sources must not count as substitutions"


def test_zero_article_domain_blanks_url_instead_of_wrong_substitution():
    corpus = make_corpus(empty_domains=("gov",))
    payload = make_payload(corpus)
    gov_idx = DOMAIN_ORDER.index("gov")
    payload["chapters"][gov_idx]["source_url"] = EVIL_URL
    payload["chapters"][gov_idx]["source_name"] = "Fabricated"
    out, _ = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][gov_idx]["source_url"] == "", out["chapters"][gov_idx]
    for u in (c["source_url"] for c in out["chapters"]):
        assert "gov.example.test" not in u


def test_domain_missing_from_corpus_blanks_url():
    corpus = make_corpus()
    del corpus["finops"]
    payload = make_payload(make_corpus())
    idx = DOMAIN_ORDER.index("finops")
    payload["chapters"][idx]["source_url"] = EVIL_URL
    out, _ = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][idx]["source_url"] == ""


def test_malformed_inputs_never_raise():
    corpus = make_corpus()
    cases = []
    cases.append({"chapters": "not a list", "takeaways": {}})
    cases.append({"chapters": None, "takeaways": None})
    cases.append({})
    p = make_payload(corpus)
    p["chapters"][0] = "not a dict"
    p["chapters"][1] = None
    p["chapters"][2] = {}  # missing keys
    p["chapters"][3] = {"source_url": 12345}  # non-string url
    p["chapters"][4] = {"source_url": ["x"]}  # unhashable url
    cases.append(p)
    p = make_payload(corpus)
    p["takeaways"]["ai"]["sources"] = "not a list"
    p["takeaways"]["cloud"]["sources"] = [None, "str", {"title": "no url"}, {"url": ["x"]}, {"url": 5}]
    p["takeaways"]["data"] = "not a dict"
    del p["takeaways"]["sec"]
    p["takeaways"]["devops"] = {}
    cases.append(p)
    cases.append("not even a dict")
    cases.append(None)
    for case in cases:
        try:
            out, count = enforce_corpus_urls(case, corpus)
        except Exception as e:  # pragma: no cover - failure path
            raise AssertionError(f"raised {type(e).__name__}: {e} for {case!r}")
        assert isinstance(count, int) and count >= 0
    # Malformed corpus must not raise either
    for bad_corpus in ({}, {"ai": None}, {"ai": ["str"]}, {"ai": [{"url": None}]}, None):
        try:
            enforce_corpus_urls(make_payload(corpus), bad_corpus)
        except Exception as e:  # pragma: no cover
            raise AssertionError(f"raised {type(e).__name__}: {e} for corpus {bad_corpus!r}")


def test_malformed_chapters_are_counted_not_ignored():
    corpus = make_corpus()
    payload = make_payload(corpus)
    payload["chapters"][0] = {"source_url": 12345}
    out, count = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][0]["source_url"] == corpus["ai"][0]["url"]
    assert count >= 1


def test_urls_matched_against_full_corpus_not_first_three():
    corpus = make_corpus(per_domain=6)
    payload = make_payload(corpus)
    fifth = corpus["ai"][4]
    payload["chapters"][0]["source_url"] = fifth["url"]
    payload["chapters"][0]["source_name"] = fifth["source_name"]
    out, count = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][0]["source_url"] == fifth["url"], "5th-article URL was wrongly substituted"
    assert out["chapters"][0]["source_name"] == fifth["source_name"]
    assert count == 0


def test_cross_domain_corpus_url_is_accepted():
    # allowed set spans the ENTIRE corpus (every domain), so a URL that exists
    # in a different domain's article is not treated as fabricated.
    corpus = make_corpus()
    payload = make_payload(corpus)
    payload["chapters"][0]["source_url"] = corpus["cloud"][0]["url"]
    out, count = enforce_corpus_urls(payload, corpus)
    assert out["chapters"][0]["source_url"] == corpus["cloud"][0]["url"]
    assert count == 0


# --- cascade wiring ---------------------------------------------------------

class _FakeResponse:
    def __init__(self, text):
        self.text = text


def _run_synthesis(responder, corpus):
    import json
    from unittest.mock import patch
    calls = []
    class Models:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            return _FakeResponse(json.dumps(responder(json.loads(kwargs['contents']))))
    class Aio:
        models = Models()
        async def aclose(self):
            pass
    client = types.SimpleNamespace(aio=Aio())
    with patch('google.genai.Client', return_value=client), patch.dict(
            os.environ, {'GEMINI_API_KEY': 'fixture-only', 'GEMINI_MODEL': 'test-model'}):
        result = asyncio.run(synthesizer.synthesize_briefing(corpus, episode_num=7))
    return result, calls


def test_new_selection_cannot_supply_foreign_citation_or_legacy_prose():
    corpus = make_corpus(per_domain=1)
    legacy = make_payload(corpus)
    result, calls = _run_synthesis(lambda incoming: legacy, corpus)
    assert len(calls) == 16, 'Eight stories each exhaust their two-attempt budget'
    assert result['fallback_content'] == 'source_derived'
    assert [c['source_url'] for c in result['chapters']] == [corpus[d][0]['url'] for d in DOMAIN_ORDER]
    assert synthesizer.validate_synthesis(result)[0]


def test_new_selection_uses_source_owned_citations_without_url_repairs():
    corpus = make_corpus(per_domain=3)
    result, calls = _run_synthesis(lambda incoming: {
        'story_id': incoming['story_id'], 'unit_ids': ['title-0', 'summary-0']}, corpus)
    assert len(calls) == 24
    assert len(result['chapters']) == 24
    assert 'fallback_content' not in result
    assert [c['source_url'] for c in result['chapters']] == [a['url'] for d in DOMAIN_ORDER for a in corpus[d]]
    assert synthesizer.validate_synthesis(result)[0]


# --- ingestion --------------------------------------------------------------

class _FakeFeedResponse:
    status_code = 200

    def __init__(self, text):
        self.text = text


class _FakeClient:
    def __init__(self, text):
        self._text = text

    async def get(self, url, **kwargs):
        return _FakeFeedResponse(self._text)


def _rss(links):
    items = "".join(
        f"<item><title>Item {i}</title><link>{l}</link><description>d</description></item>"
        for i, l in enumerate(links)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'


def _ingest(links):
    feed = {"name": "Test Feed", "url": "https://feed.example.test/rss"}
    return asyncio.run(ingestion.fetch_feed_items(_FakeClient(_rss(links)), "ai", feed))


def test_ingestion_rejects_javascript_link_and_keeps_https():
    items = _ingest([JS_URL, "https://ok.example.test/a"])
    urls = [i["url"] for i in items]
    assert JS_URL not in urls
    assert urls == ["https://ok.example.test/a"], urls


def test_ingestion_rejects_3000_char_link():
    long_link = "https://ok.example.test/" + "a" * 3000
    items = _ingest([long_link, "https://ok.example.test/b"])
    urls = [i["url"] for i in items]
    assert long_link not in urls
    assert urls == ["https://ok.example.test/b"], urls


def test_ingestion_rejects_other_unsafe_links():
    bad = ["data:text/html,x", "file:///etc/passwd", "ftp://ok.example.test/x", "//ok.example.test/x"]
    items = _ingest(bad)
    assert items == [], [i["url"] for i in items]


def test_is_safe_link_rejects_hostless_and_non_string():
    # feedparser rewrites some hostless links before our check, so exercise
    # the predicate directly for those shapes.
    for bad in ("https://", "https:///nohost", "http://:80/x", "", None, 5, b"https://a.test"):
        assert ingestion.is_safe_link(bad) is False, f"expected rejection of {bad!r}"
    assert ingestion.is_safe_link("https://a.test/x") is True


def test_ingestion_keeps_normal_http_and_https_links():
    items = _ingest(["https://ok.example.test/a", "http://ok.example.test/b"])
    assert [i["url"] for i in items] == ["https://ok.example.test/a", "http://ok.example.test/b"]


def test_ingestion_link_at_2048_chars_kept_and_2049_rejected():
    prefix = "https://ok.example.test/"
    at_limit = prefix + "a" * (2048 - len(prefix))
    over = prefix + "b" * (2049 - len(prefix))
    assert len(at_limit) == 2048 and len(over) == 2049
    items = _ingest([at_limit, over])
    assert [i["url"] for i in items] == [at_limit]


def test_ingestion_logs_rejection_with_truncated_value():
    import logging

    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _H()
    ingestion.logger.addHandler(h)
    try:
        _ingest(["https://ok.example.test/" + "z" * 3000])
    finally:
        ingestion.logger.removeHandler(h)
    msgs = [m for m in records if "Test Feed" in m]
    assert msgs, f"no rejection log naming the source: {records}"
    assert all(len(m) < 400 for m in msgs), "offending value not truncated in log"


def test_explicit_domain_sources_cannot_cross_middle_gap():
    corpus = make_corpus(empty_domains=('cloud',))
    payload = make_payload(corpus)
    payload['chapters'] = [{**c, 'domain': d} for c, d in zip(payload['chapters'], DOMAIN_ORDER) if d != 'cloud']
    payload['content_availability'] = {'schema_version': 1}
    payload['chapters'][1]['source_url'] = corpus['ai'][0]['url']
    payload['takeaways']['data']['sources'] = [{'title': 'AI item', 'url': corpus['ai'][0]['url']}]
    result, count = enforce_corpus_urls(payload, corpus)
    assert count == 1
    assert result['chapters'][1]['domain'] == 'data'
    assert result['chapters'][1]['source_url'] == corpus['data'][0]['url']
    assert result['takeaways']['data']['sources'] == []


def test_malformed_domain_id_does_not_raise_url_repair():
    payload = {'chapters': [{'domain': {'invalid': True}, 'source_url': EVIL_URL}], 'takeaways': {}}
    result, count = enforce_corpus_urls(payload, make_corpus())
    assert count == 1
    assert result['chapters'][0]['source_url'] == ''


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"✗ {t.__name__} FAILED: {e}")
        except Exception as e:
            failures += 1
            print(f"✗ {t.__name__} ERRORED: {type(e).__name__}: {e}")

    print("\n=======================================================")
    if failures:
        print(f"{failures} TEST(S) FAILED")
    else:
        print("ALL URL PROVENANCE TESTS PASSED")
    print("=======================================================")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

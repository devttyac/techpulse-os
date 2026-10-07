import sys
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["GEMINI_API_KEY"] = ""
os.environ["API_SECRET_KEY"] = ""

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from src.synthesizer import (
    validate_synthesis,
    generate_deterministic_fallback,
    local_now,
    _is_schema_rejection,
    RETIRED_SPECIMEN_TITLES,
    DOMAIN_ORDER,
)


def make_valid_parsed():
    chapters = [
        {"title": f"{i + 1}. Test Domain {d}: Some Finding", "source_name": "Src", "source_url": "https://example.com"}
        for i, d in enumerate(DOMAIN_ORDER)
    ]
    # script_segments must bidirectionally join with chapters by chapter_title
    # (every segment's chapter_title known, every chapter referenced at least
    # once) for this fixture to represent a genuinely valid payload.
    script_segments = (
        [{"speaker": "Host A", "text": "...", "chapter_title": c["title"]} for c in chapters]
        + [{"speaker": "Host B", "text": "...", "chapter_title": c["title"]} for c in chapters]
    )
    takeaways = {
        d: {
            "badge": "B",
            "release_date": "Jan 2026",
            "title": "T",
            "bullets": ["x"],
            "interview_framing": "f",
            "sources": [{"title": "s", "url": "https://example.com"}],
        }
        for d in DOMAIN_ORDER
    }
    return {
        "title": "T",
        "summary": "S",
        "hosts": "H",
        "script_segments": script_segments,
        "chapters": chapters,
        "takeaways": takeaways,
        "flashcards": [],
    }


def test_validate_synthesis_accepts_valid_input():
    parsed = make_valid_parsed()
    ok, reason = validate_synthesis(parsed)
    assert ok, f"expected valid input to be accepted, got rejection: {reason}"
    print("✓ validate_synthesis accepts a well-formed 8-chapter/8-takeaway payload")


def test_validate_synthesis_rejects_wrong_chapter_count():
    parsed = make_valid_parsed()
    parsed["chapters"] = parsed["chapters"][:7]
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected rejection with a reason when chapter count != 8"
    print(f"✓ validate_synthesis rejects chapter count != 8 ({reason})")


def test_validate_synthesis_rejects_specimen_leak():
    parsed = make_valid_parsed()
    leaked_title = next(iter(RETIRED_SPECIMEN_TITLES))
    parsed["chapters"][0]["title"] = leaked_title
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected rejection when a retired specimen title leaks into output"
    print(f"✓ validate_synthesis rejects retired specimen title leak ({reason})")


def test_validate_synthesis_rejects_missing_takeaway_domain():
    parsed = make_valid_parsed()
    del parsed["takeaways"]["gov"]
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected rejection when a required takeaway domain key is missing"
    print(f"✓ validate_synthesis rejects missing takeaway domain key ({reason})")


def test_validate_synthesis_rejects_identical_previous_titles():
    parsed = make_valid_parsed()
    previous_titles = [c["title"] for c in parsed["chapters"]]
    ok, reason = validate_synthesis(parsed, previous_titles=previous_titles)
    assert not ok and reason, "expected rejection when chapter titles are identical to the previous episode"
    print(f"✓ validate_synthesis rejects chapter titles identical to previous episode ({reason})")


def test_validate_synthesis_accepts_consistent_bidirectional_payload():
    parsed = make_valid_parsed()
    ok, reason = validate_synthesis(parsed)
    assert ok, f"expected a fully consistent bidirectional payload to be accepted, got rejection: {reason}"
    print("✓ validate_synthesis accepts a fully consistent bidirectional chapter_title <-> chapters join")


def test_validate_synthesis_rejects_segment_referencing_unknown_chapter_title():
    parsed = make_valid_parsed()
    parsed["script_segments"][0]["chapter_title"] = "Some Title Not In Chapters"
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected rejection when a segment references a chapter_title absent from chapters"
    print(f"✓ validate_synthesis rejects a segment chapter_title absent from chapters ({reason})")


def test_validate_synthesis_rejects_chapter_not_referenced_by_any_segment():
    parsed = make_valid_parsed()
    orphan_title = parsed["chapters"][0]["title"]
    parsed["script_segments"] = [s for s in parsed["script_segments"] if s["chapter_title"] != orphan_title]
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected rejection when a chapter title is referenced by no segment"
    print(f"✓ validate_synthesis rejects a chapter title referenced by no segment ({reason})")


def test_validate_synthesis_rejects_non_list_script_segments():
    parsed = make_valid_parsed()
    parsed["script_segments"] = "not-a-list"
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected clean rejection (not a raise) for non-list script_segments"
    print(f"✓ validate_synthesis rejects non-list script_segments cleanly ({reason})")


def test_validate_synthesis_rejects_non_dict_script_segment_entry():
    parsed = make_valid_parsed()
    parsed["script_segments"].append("not-a-dict-entry")
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected clean rejection (not a raise) for a non-dict script_segments entry"
    print(f"✓ validate_synthesis rejects a non-dict script_segments entry cleanly ({reason})")


def test_validate_synthesis_rejects_malformed_script_segment_entry():
    parsed = make_valid_parsed()
    parsed["script_segments"].append({"speaker": "Host A", "text": "missing chapter_title key"})
    ok, reason = validate_synthesis(parsed)
    assert not ok and reason, "expected clean rejection (not a raise) for a segment entry missing chapter_title"
    print(f"✓ validate_synthesis rejects a malformed segment entry missing chapter_title cleanly ({reason})")


def _full_corpus_all_domains():
    return {
        domain: [{
            "title": f"{domain.upper()} Breakthrough Announced Today",
            "source_name": f"{domain.title()} Source",
            "url": f"https://example.com/{domain}",
            "summary": "Some summary.",
        }]
        for domain in DOMAIN_ORDER
    }


def test_deterministic_fallback_passes_bidirectional_chapter_title_rule():
    corpus = _full_corpus_all_domains()
    result = generate_deterministic_fallback(corpus, 996)
    ok, reason = validate_synthesis(result)
    assert ok, f"expected generate_deterministic_fallback output (full corpus) to pass validate_synthesis, got rejection: {reason}"
    print("✓ generate_deterministic_fallback output satisfies the bidirectional chapter_title <-> chapters join rule")


def test_deterministic_fallback_uses_corpus_headlines():
    corpus = {
        "ai": [{
            "title": "Anthropic Ships New Agent Router",
            "source_name": "Anthropic Blog",
            "url": "https://anthropic.com/x",
            "summary": "New routing model.",
        }],
        "cloud": [],
        "data": [{
            "title": "Fabric Adds Streaming Direct Lake",
            "source_name": "MS Fabric Blog",
            "url": "https://fabric.ms/y",
            "summary": "Streaming support.",
        }],
        "sec": [], "devops": [], "arch": [], "finops": [], "gov": [],
    }
    result = generate_deterministic_fallback(corpus, 999)
    chapter_titles = [c["title"] for c in result["chapters"]]
    assert any("Anthropic Ships New Agent Router" in t for t in chapter_titles), \
        f"expected AI chapter title to incorporate corpus headline, got: {chapter_titles}"
    assert any("Fabric Adds Streaming Direct Lake" in t for t in chapter_titles), \
        f"expected data chapter title to incorporate corpus headline, got: {chapter_titles}"
    print("✓ generate_deterministic_fallback chapter titles incorporate supplied corpus headlines")


def test_deterministic_fallback_all_domains_present():
    result = generate_deterministic_fallback(_full_corpus_all_domains(), 998)
    assert set(result["takeaways"]) == set(DOMAIN_ORDER)
    assert [c["domain"] for c in result["chapters"]] == DOMAIN_ORDER


def test_deterministic_fallback_handles_zero_article_domain_gracefully():
    assert generate_deterministic_fallback({"ai": []}, 997) is None


def test_deterministic_fallback_empty_corpus_has_no_specimen_leak():
    assert generate_deterministic_fallback({d: [] for d in DOMAIN_ORDER}, 995) is None


def test_deterministic_fallback_partial_corpus_omits_unavailable_chapters():
    corpus = {"ai": [{"title": "Anthropic Ships New Agent Router", "source_name": "Anthropic Blog",
                      "url": "https://anthropic.com/x", "summary": "New routing model."}]}
    result = generate_deterministic_fallback(corpus, 994)
    assert len(result["chapters"]) == 1
    assert "Anthropic Ships New Agent Router" in result["chapters"][0]["title"]
    assert result["takeaways"]["cloud"]["bullets"] == []
    assert result["takeaways"]["cloud"]["sources"] == []
    assert validate_synthesis(result)[0]


def test_is_schema_rejection_true_for_schema_shaped_messages():
    schema_shaped = [
        Exception("400 INVALID_ARGUMENT: response_schema is not supported for this model"),
        Exception("pydantic.ValidationError: 8 validation errors for Takeaways"),
        ValueError("Unsupported schema: nested definitions exceed depth limit"),
    ]
    for exc in schema_shaped:
        assert _is_schema_rejection(exc), f"expected schema-shaped exception to be classified as a schema rejection: {exc}"
    print("✓ _is_schema_rejection returns True for schema-shaped exception messages")


def test_is_schema_rejection_false_for_transport_auth_quota_messages():
    transport_shaped = [
        TimeoutError("Request timed out after 30s"),
        ConnectionError("Connection reset by peer"),
        Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric"),
        Exception("401 Unauthorized: invalid API key"),
    ]
    for exc in transport_shaped:
        assert not _is_schema_rejection(exc), f"expected transport/auth/quota exception to NOT be classified as a schema rejection: {exc}"
    print("✓ _is_schema_rejection returns False for timeout/connection/401/429 exception messages")


def test_local_now_uses_singapore_timezone_not_utc():
    # 23:30 UTC on 2026-01-01 is 2026-01-02 07:30 in Asia/Singapore (UTC+8) -- the two dates differ.
    utc_instant = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
    sgt_instant = utc_instant.astimezone(ZoneInfo("Asia/Singapore"))
    assert utc_instant.strftime("%b %d, %Y") != sgt_instant.strftime("%b %d, %Y"), \
        "test fixture invalid: dates should differ across the UTC/SGT boundary"

    now = local_now()
    assert now.tzinfo is not None, "local_now() must return a timezone-aware datetime"
    now_via_utc_conversion = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Singapore"))
    assert now.strftime("%b %d, %Y") == now_via_utc_conversion.strftime("%b %d, %Y"), \
        "local_now() date does not match the Asia/Singapore wall-clock date"
    print("✓ local_now() produces the Asia/Singapore date, proven to differ from UTC date across the 23:00-23:59 UTC boundary")


def test_supplied_manifest_overrides_mutated_corpus_during_synthesis():
    import asyncio
    from src.story_manifest import freeze_story_manifest
    from src.content_availability import build_content_availability
    from src.synthesizer import synthesize_briefing
    corpus = {'ai': [{'title': 'Frozen title', 'summary': 'Frozen summary.', 'source_name': 'Frozen source',
                      'url': 'https://example.test/frozen'}]}
    manifest = freeze_story_manifest(corpus, build_content_availability(corpus))
    corpus['ai'][0]['summary'] = 'Mutated evidence must not appear.'
    result = asyncio.run(synthesize_briefing(corpus, manifest=manifest))
    assert result['flashcards'][0]['answer'] == 'Frozen summary.'
    assert result['chapters'][0]['title'] == 'Frozen title'
    assert 'Mutated evidence' not in str(result)
    assert validate_synthesis(result)[0]


def main():
    tests = [
        test_supplied_manifest_overrides_mutated_corpus_during_synthesis,
        test_validate_synthesis_accepts_valid_input,
        test_validate_synthesis_rejects_wrong_chapter_count,
        test_validate_synthesis_rejects_specimen_leak,
        test_validate_synthesis_rejects_missing_takeaway_domain,
        test_validate_synthesis_rejects_identical_previous_titles,
        test_validate_synthesis_accepts_consistent_bidirectional_payload,
        test_validate_synthesis_rejects_segment_referencing_unknown_chapter_title,
        test_validate_synthesis_rejects_chapter_not_referenced_by_any_segment,
        test_validate_synthesis_rejects_non_list_script_segments,
        test_validate_synthesis_rejects_non_dict_script_segment_entry,
        test_validate_synthesis_rejects_malformed_script_segment_entry,
        test_deterministic_fallback_passes_bidirectional_chapter_title_rule,
        test_deterministic_fallback_uses_corpus_headlines,
        test_deterministic_fallback_all_domains_present,
        test_deterministic_fallback_handles_zero_article_domain_gracefully,
        test_deterministic_fallback_empty_corpus_has_no_specimen_leak,
        test_deterministic_fallback_partial_corpus_omits_unavailable_chapters,
        test_is_schema_rejection_true_for_schema_shaped_messages,
        test_is_schema_rejection_false_for_transport_auth_quota_messages,
        test_local_now_uses_singapore_timezone_not_utc,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"✗ {t.__name__} FAILED: {e}")
        except Exception as e:
            failures += 1
            print(f"✗ {t.__name__} ERRORED: {e}")

    print("\n=======================================================")
    if failures:
        print(f"{failures} TEST(S) FAILED")
    else:
        print("ALL SYNTHESIZER UNIT TESTS PASSED")
    print("=======================================================")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

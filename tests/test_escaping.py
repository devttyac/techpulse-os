import asyncio
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# src.main creates storage directories at import time. Point it at a throwaway
# directory before import so the test never touches real episode data.
_TMP_STORAGE = tempfile.mkdtemp(prefix="techpulse-escaping-test-")
os.environ["STORAGE_DIR"] = _TMP_STORAGE
os.environ["HOST_URL"] = "https://feed.example.test"

from src import main as app_main  # noqa: E402
from src.main import _safe_url, podcast_rss  # noqa: E402

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
PSC_NS = "{http://podlove.org/simple-chapters}chapter"

HOSTILE_TITLE = '" onmouseover="alert(1)'
HOSTILE_URL = "javascript:alert(1)"


class _StubRequest:
    """Minimal stand-in for fastapi.Request; podcast_rss only reads base_url."""
    base_url = "https://feed.example.test/"


def test_safe_url_rejects_javascript_scheme():
    assert _safe_url("javascript:alert(1)") == ""
    assert _safe_url("  JaVaScRiPt:alert(1)") == ""


def test_safe_url_rejects_data_uri():
    assert _safe_url("data:text/html,<script>alert(1)</script>") == ""


def test_safe_url_rejects_file_scheme():
    assert _safe_url("file:///etc/passwd") == ""


def test_safe_url_rejects_empty_none_and_non_string():
    for bad in ("", "   ", None, 0, 42, [], {}, b"https://example.com", object()):
        assert _safe_url(bad) == "", f"expected rejection of {bad!r}"


def test_safe_url_rejects_scheme_without_host():
    assert _safe_url("https://") == ""
    assert _safe_url("//example.com/x") == ""


def test_safe_url_passes_normal_https_url_unchanged():
    url = "https://example.com/a?b=c"
    assert _safe_url(url) == url
    assert _safe_url("http://example.com/path") == "http://example.com/path"


def _write_hostile_episode():
    for name in os.listdir(app_main.EPISODES_DIR):
        os.remove(os.path.join(app_main.EPISODES_DIR, name))
    episode = {
        "id": "ep-900",
        "episode_number": 900,
        "title": "Escaping Fixture",
        "date": "Jan 01, 2026",
        "summary": "Fixture summary",
        "duration": "05:20",
        "chapters": [
            {
                "time": "00:00",
                "seconds": 0,
                "title": HOSTILE_TITLE,
                "source_name": HOSTILE_TITLE,
                "source_url": HOSTILE_URL,
            },
            {
                "time": "01:00",
                "seconds": 60,
                "title": "Benign chapter & friends",
                "source_name": "Example",
                "source_url": "https://example.com/a?b=c&d=e",
            },
        ],
    }
    with open(os.path.join(app_main.EPISODES_DIR, "ep-900.json"), "w") as fp:
        json.dump(episode, fp)


def _build_feed():
    _write_hostile_episode()
    response = asyncio.run(podcast_rss(_StubRequest()))
    return response.body.decode("utf-8")


def test_feed_contains_no_javascript_scheme_or_raw_handler():
    xml_text = _build_feed()
    assert "javascript:" not in xml_text.lower(), "javascript: URL leaked into feed XML"
    assert 'onmouseover="' not in xml_text, "unescaped onmouseover=\" present in raw feed XML"


def test_feed_show_notes_html_has_no_attribute_breakout():
    xml_text = _build_feed()
    root = ET.fromstring(xml_text)
    show_notes = root.find(f".//{CONTENT_NS}").text
    assert "javascript:" not in show_notes.lower()
    assert 'onmouseover="' not in show_notes, "attribute breakout survived XML decoding"
    assert "&quot; onmouseover=&quot;alert(1)" in show_notes, "hostile title was not entity-escaped"
    # Benign chapter keeps a working, escaped link.
    assert 'href="https://example.com/a?b=c&amp;d=e"' in show_notes


def test_feed_psc_chapter_href_is_blank_for_unsafe_url_and_title_roundtrips():
    xml_text = _build_feed()
    root = ET.fromstring(xml_text)
    chapters = root.findall(f".//{PSC_NS}")
    assert len(chapters) == 2
    assert chapters[0].get("href") == "", "unsafe URL must not be written to psc:chapter href"
    assert chapters[0].get("title") == HOSTILE_TITLE, "title must survive XML round-trip exactly once-escaped"
    assert chapters[1].get("href") == "https://example.com/a?b=c&d=e"


def main():
    tests = [
        test_safe_url_rejects_javascript_scheme,
        test_safe_url_rejects_data_uri,
        test_safe_url_rejects_file_scheme,
        test_safe_url_rejects_empty_none_and_non_string,
        test_safe_url_rejects_scheme_without_host,
        test_safe_url_passes_normal_https_url_unchanged,
        test_feed_contains_no_javascript_scheme_or_raw_handler,
        test_feed_show_notes_html_has_no_attribute_breakout,
        test_feed_psc_chapter_href_is_blank_for_unsafe_url_and_title_roundtrips,
    ]
    failures = 0
    try:
        for t in tests:
            try:
                t()
                print(f"✓ {t.__name__}")
            except AssertionError as e:
                failures += 1
                print(f"✗ {t.__name__} FAILED: {e}")
            except Exception as e:
                failures += 1
                print(f"✗ {t.__name__} ERRORED: {e}")
    finally:
        shutil.rmtree(_TMP_STORAGE, ignore_errors=True)

    print("\n=======================================================")
    if failures:
        print(f"{failures} TEST(S) FAILED")
    else:
        print("ALL ESCAPING TESTS PASSED")
    print("=======================================================")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

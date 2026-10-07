import asyncio
import json
import os
import shutil
import sys
import tempfile

import httpx

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# src.main creates storage directories at import time. Point it at a throwaway
# directory before import so the test never touches real episode data.
_TMP_STORAGE = tempfile.mkdtemp(prefix="techpulse-auth-test-")
os.environ["STORAGE_DIR"] = _TMP_STORAGE
os.environ.pop("API_SECRET_KEY", None)

from src import main as app_main  # noqa: E402

TEST_KEY = "test-secret-not-a-real-credential"
HOSTILE_SETTINGS = {"max_episodes_retained": 1}


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_main.app), base_url="http://test"
    )


def _run(coro):
    return asyncio.run(coro)


class _env:
    """Set or clear API_SECRET_KEY for the duration of a block, then restore."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.prev = os.environ.get("API_SECRET_KEY")
        if self.value is None:
            os.environ.pop("API_SECRET_KEY", None)
        else:
            os.environ["API_SECRET_KEY"] = self.value

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("API_SECRET_KEY", None)
        else:
            os.environ["API_SECRET_KEY"] = self.prev


def _saved_config():
    path = os.path.join(_TMP_STORAGE, "config.json")
    if not os.path.exists(path):
        return {}
    with open(path) as fp:
        return json.load(fp)


# --- No key configured: read-only mode -------------------------------------

def test_no_key_get_api_is_allowed():
    async def go():
        with _env(None):
            async with _client() as c:
                r = await c.get("/api/episodes")
                assert r.status_code == 200, f"expected 200, got {r.status_code}"
    _run(go())


def test_no_key_post_is_rejected_and_does_not_mutate():
    async def go():
        with _env(None):
            async with _client() as c:
                r = await c.post("/api/settings", json=HOSTILE_SETTINGS)
                assert r.status_code == 401, f"expected 401, got {r.status_code}"
                assert "read-only" in r.json()["detail"].lower()
                assert "API_SECRET_KEY" in r.json()["detail"]
    _run(go())
    assert _saved_config().get("max_episodes_retained") != 1, "rejected POST still mutated config"


def test_no_key_other_mutating_routes_are_rejected():
    async def go():
        with _env(None):
            async with _client() as c:
                for path in ("/api/refresh", "/api/refresh/cancel", "/api/refresh/reset",
                             "/api/settings/cleanup", "/api/chat", "/api/export-vault"):
                    r = await c.post(path, json={})
                    assert r.status_code == 401, f"POST {path}: expected 401, got {r.status_code}"
    _run(go())


def test_no_key_whitespace_only_key_counts_as_unset():
    async def go():
        with _env("   "):
            async with _client() as c:
                r = await c.post("/api/settings", json={})
                assert r.status_code == 401, f"expected 401, got {r.status_code}"
    _run(go())


# --- Key configured --------------------------------------------------------

def test_key_set_missing_header_is_rejected():
    async def go():
        with _env(TEST_KEY):
            async with _client() as c:
                for method, path in (("GET", "/api/episodes"), ("GET", "/api/refresh/status"), ("POST", "/api/settings")):
                    r = await c.request(method, path, json={} if method == "POST" else None)
                    assert r.status_code == 401, f"{method} {path}: expected 401, got {r.status_code}"
    _run(go())


def test_key_set_wrong_header_is_rejected():
    async def go():
        with _env(TEST_KEY):
            async with _client() as c:
                for method, path in (("GET", "/api/episodes"), ("GET", "/api/refresh/status"), ("POST", "/api/settings")):
                    r = await c.request(
                        method, path, headers={"X-API-Key": "wrong"},
                        json={} if method == "POST" else None,
                    )
                    assert r.status_code == 401, f"{method} {path}: expected 401, got {r.status_code}"
    _run(go())


def test_key_set_correct_header_is_accepted_on_get_and_post():
    async def go():
        with _env(TEST_KEY):
            async with _client() as c:
                h = {"X-API-Key": TEST_KEY}
                r = await c.get("/api/episodes", headers=h)
                assert r.status_code == 200, f"GET: expected 200, got {r.status_code}"
                r = await c.post("/api/settings", headers=h, json={})
                assert r.status_code == 200, f"POST: expected 200, got {r.status_code}"
    _run(go())


def test_key_set_non_ascii_header_is_rejected_not_crashed():
    # hmac.compare_digest raises TypeError on non-ASCII str; the dependency must
    # compare bytes so a hostile header yields 401, not a 500.
    async def go():
        with _env(TEST_KEY):
            async with _client() as c:
                r = await c.get("/api/episodes", headers={"X-API-Key": "café".encode("utf-8")})
                assert r.status_code == 401, f"expected 401, got {r.status_code}"
    _run(go())


# --- Always open -----------------------------------------------------------

def test_healthz_open_in_both_modes():
    async def go():
        for value in (None, TEST_KEY):
            with _env(value):
                async with _client() as c:
                    r = await c.get("/healthz")
                    assert r.status_code == 200, f"key={value!r}: expected 200, got {r.status_code}"
    _run(go())


# --- Meta-test -------------------------------------------------------------

def test_every_api_route_requires_auth():
    unprotected = []
    seen = 0
    for route in app_main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        seen += 1
        calls = [d.call for d in route.dependant.dependencies]
        if app_main.require_auth not in calls:
            methods = ",".join(sorted(getattr(route, "methods", []) or []))
            unprotected.append(f"{methods} {path}")
    assert seen >= 12, f"expected at least 12 /api/ routes, found {seen}"
    assert not unprotected, (
        "Every /api/* route must declare Depends(require_auth). A route was added "
        "without it, which leaves it unauthenticated: " + "; ".join(unprotected)
    )


def main():
    tests = [
        test_no_key_get_api_is_allowed,
        test_no_key_post_is_rejected_and_does_not_mutate,
        test_no_key_other_mutating_routes_are_rejected,
        test_no_key_whitespace_only_key_counts_as_unset,
        test_key_set_missing_header_is_rejected,
        test_key_set_wrong_header_is_rejected,
        test_key_set_correct_header_is_accepted_on_get_and_post,
        test_key_set_non_ascii_header_is_rejected_not_crashed,
        test_healthz_open_in_both_modes,
        test_every_api_route_requires_auth,
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
                print(f"✗ {t.__name__} ERRORED: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(_TMP_STORAGE, ignore_errors=True)

    print("\n=======================================================")
    if failures:
        print(f"{failures} TEST(S) FAILED")
    else:
        print("ALL AUTH TESTS PASSED")
    print("=======================================================")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Offline controlled-fetch tests; all hosts, rights and responses are invented."""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.acquisition.http import fetch_publisher
from src.ingestion import fetch_candidate, ingest_all_domains


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "source_readiness_synthetic.json").read_text())


class FakeWriter:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="tp08-synthetic-")
        self.addCleanup(self.folder.cleanup)
        self.registry_path = Path(self.folder.name) / "profiles.json"
        self.registry = self._registry()
        self.registry_path.write_text(json.dumps(self.registry))
        self.path_patch = patch("src.acquisition.readiness._REGISTRY_PATH", self.registry_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.calls = []
        self.writers = []
        self.responses = []
        self.addresses = {"alder.example": ["93.184.216.34"]}

    def _registry(self):
        profile = deepcopy(FIXTURE["profile"])
        profile["rights"]["evidence_sha256"] = hashlib.sha256(FIXTURE["synthetic_rights_document"].encode()).hexdigest()
        return {"schema_version": 1, "synthetic_test_only": True, "profiles": [{
            "profile": profile,
            "current_binding": deepcopy(FIXTURE["current_binding"]),
            "fixture_observation": deepcopy(FIXTURE["observation"]),
            "rights_document": FIXTURE["synthetic_rights_document"],
            "fixture_raw_text": FIXTURE["raw_fixture_text"],
            "fixture_body_text": FIXTURE["expected_complete_body"],
        }]}

    async def resolve(self, host, port):
        self.calls.append(("resolve", host, port))
        return self.addresses.get(host, [])

    async def connect(self, address, port, hostname, tls):
        self.calls.append(("connect", address, port, hostname, tls))
        reader = asyncio.StreamReader()
        reader.feed_data(self.responses.pop(0))
        reader.feed_eof()
        writer = FakeWriter()
        self.writers.append(writer)
        return reader, writer

    def run_fetch(self, url="https://alder.example/news/pinedb", **kwargs):
        return asyncio.run(fetch_publisher(url, "alder-article", resolver=self.resolve,
                                            connector=self.connect, **kwargs))

    def test_empty_registry_denies_before_dns_or_connection(self):
        self.registry_path.write_text('{"schema_version": 1, "profiles": []}')
        result = self.run_fetch()
        self.assertEqual(result.reason, "unregistered_profile")
        self.assertIsNone(result.raw_bytes)
        self.assertEqual(self.calls, [])

    def test_changed_rights_denies_before_request(self):
        self.registry["profiles"][0]["rights_document"] = "changed"
        self.registry_path.write_text(json.dumps(self.registry))
        result = self.run_fetch()
        self.assertEqual(result.reason, "bound_evidence_mismatch")
        self.assertEqual(self.calls, [])

    def test_other_consistent_connector_cannot_authorize_http_transport(self):
        record = self.registry["profiles"][0]
        record["profile"]["connector"]["id"] = "reviewed-browser"
        record["current_binding"]["connector_id"] = "reviewed-browser"
        record["fixture_observation"]["connector_id"] = "reviewed-browser"
        self.registry_path.write_text(json.dumps(self.registry))
        result = self.run_fetch()
        self.assertEqual(result.reason, "unsupported_transport")
        self.assertEqual(self.calls, [])

    def test_exact_representation_and_pinned_tls_name(self):
        raw = b"\x00<html>\xff exact bytes</html>"
        self.responses = [b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: 27\r\nSet-Cookie: private=token\r\n\r\n" + raw]
        result = self.run_fetch()
        self.assertEqual(result.reason, "fetched")
        self.assertEqual(result.raw_bytes, raw)
        self.assertEqual(result.raw_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.final_url, "https://alder.example/news/pinedb")
        self.assertEqual(result.safe_headers, {"content-type": "text/html; charset=utf-8", "content-length": "27"})
        self.assertEqual(result.client, "controlled-http-html")
        self.assertEqual(result.profile_revision, "profile-3")
        self.assertEqual(result.decoding, "utf-8")
        self.assertIn(("connect", "93.184.216.34", 443, "alder.example", True), self.calls)
        self.assertIn(b"Host: alder.example\r\n", self.writers[0].written)
        self.assertNotIn(b"Cookie:", self.writers[0].written)
        self.assertTrue(self.writers[0].closed)

    def test_redirect_rechecks_profile_and_blocks_other_host(self):
        self.responses = [b"HTTP/1.1 302 Found\r\nLocation: https://other.example/story\r\nContent-Length: 0\r\n\r\n"]
        result = self.run_fetch()
        self.assertEqual(result.reason, "unregistered_profile")
        self.assertEqual([call for call in self.calls if call[0] == "connect"],
                         [("connect", "93.184.216.34", 443, "alder.example", True)])

    def test_private_and_mixed_dns_addresses_reject_without_connection(self):
        for addresses in (["127.0.0.1"], ["93.184.216.34", "10.0.0.2"], []):
            with self.subTest(addresses=addresses):
                self.calls.clear()
                self.addresses["alder.example"] = addresses
                result = self.run_fetch()
                self.assertEqual(result.reason, "unsafe_address")
                self.assertFalse(any(call[0] == "connect" for call in self.calls))

    def test_dns_rebinding_cannot_change_selected_connection_ip(self):
        self.responses = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]
        self.addresses["alder.example"] = ["93.184.216.34"]
        result = self.run_fetch()
        self.assertEqual(result.reason, "fetched")
        self.assertEqual([call for call in self.calls if call[0] == "resolve"],
                         [("resolve", "alder.example", 443)])
        self.assertEqual(self.calls[-1][1], "93.184.216.34")

    def test_short_and_oversize_bodies_never_return_partial_bytes(self):
        for response, kwargs, reason in (
            (b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nab", {}, "incomplete_response"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabcde", {"max_bytes": 4}, "oversize"),
        ):
            with self.subTest(reason=reason):
                self.responses = [response]
                result = self.run_fetch(**kwargs)
                self.assertEqual(result.reason, reason)
                self.assertIsNone(result.raw_bytes)

    def test_timeout_returns_failure_without_bytes(self):
        async def slow(*args):
            await asyncio.sleep(1)
        result = asyncio.run(fetch_publisher("https://alder.example/news/pinedb", "alder-article",
                                             resolver=slow, connector=self.connect, timeout=0.01))
        self.assertEqual(result.reason, "timeout")
        self.assertIsNone(result.raw_bytes)

    def test_chunked_response_preserves_exact_body(self):
        self.responses = [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                          b"2\r\n\x00a\r\n3\r\nb\xffc\r\n0\r\n\r\n"]
        result = self.run_fetch()
        self.assertEqual(result.reason, "fetched")
        self.assertEqual(result.raw_bytes, b"\x00ab\xffc")

    def test_framed_body_does_not_wait_for_connection_close(self):
        async def held_open(address, port, hostname, tls):
            reader = asyncio.StreamReader()
            reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            return reader, FakeWriter()
        result = asyncio.run(fetch_publisher("https://alder.example/news/pinedb", "alder-article",
                                             resolver=self.resolve, connector=held_open, timeout=0.01))
        self.assertEqual(result.reason, "fetched")
        self.assertEqual(result.raw_bytes, b"ok")

    def test_unframed_or_ambiguous_response_fails_closed(self):
        for response in (
            b"HTTP/1.1 200 OK\r\n\r\nbody",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\nok",
            b"HTTP/1.1 206 Partial Content\r\nContent-Length: 2\r\n\r\nok",
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\nok",
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\x00private\r\nContent-Length: 2\r\n\r\nok",
            b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 10000 + b"\r\n\r\nok",
        ):
            with self.subTest(response=response[:35]):
                self.responses = [response]
                result = self.run_fetch()
                self.assertNotEqual(result.reason, "fetched")
                self.assertIsNone(result.raw_bytes)

    def test_same_profile_redirect_is_explicit_and_revalidated(self):
        self.responses = [b"HTTP/1.1 302 Found\r\nLocation: /new\r\nContent-Length: 0\r\n\r\n",
                          b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]
        result = self.run_fetch()
        self.assertEqual(result.reason, "fetched")
        self.assertEqual(result.final_url, "https://alder.example/new")
        self.assertEqual(len([call for call in self.calls if call[0] == "resolve"]), 2)

    def test_control_char_and_non_ascii_urls_cannot_send_request(self):
        for url in ("https://alder.example/\r\nInjected: yes", "https://alder.example/é",
                    "https://alder.example\\@other.example/story",
                    "https://alder.example:80/story", "http://alder.example:443/story"):
            with self.subTest(url=url):
                result = self.run_fetch(url)
                self.assertEqual(result.reason, "unsafe_url")
                self.assertEqual(self.calls, [])

    def test_ingestion_entry_requires_guard_and_does_not_publish(self):
        self.registry_path.write_text('{"schema_version": 1, "profiles": []}')
        result = asyncio.run(fetch_candidate("https://alder.example/news/pinedb", "alder-article",
                                              resolver=self.resolve, connector=self.connect))
        self.assertEqual(result.reason, "unregistered_profile")
        self.assertEqual(self.calls, [])
        corpus = asyncio.run(ingest_all_domains())
        self.assertTrue(all(not articles for articles in corpus.values()))


if __name__ == "__main__":
    unittest.main()

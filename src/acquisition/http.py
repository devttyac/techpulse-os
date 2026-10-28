"""Controlled publisher HTTP boundary. No shipped profile authorizes live use."""

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import re
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from src.acquisition.readiness import authorize_fetch


_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")
_MAX_URL = 2048
_MAX_HEADERS = 16384
_MAX_REDIRECTS = 3
_CONCURRENCY = asyncio.Semaphore(4)
_REDIRECTS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class FetchResult:
    reason: str
    raw_bytes: bytes | None = None
    raw_sha256: str | None = None
    final_url: str | None = None
    status: int | None = None
    safe_headers: dict | None = None
    client: str | None = None
    client_revision: str | None = None
    profile_revision: str | None = None
    decoding: str | None = None
    connected_address: str | None = None


class FetchFailure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _target(url):
    if (type(url) is not str or not url or len(url) > _MAX_URL
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in url) or "\\" in url):
        raise FetchFailure("unsafe_url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise FetchFailure("unsafe_url") from None
    if (parsed.scheme not in ("http", "https") or not host or not _HOST.fullmatch(host)
            or host.startswith(".") or host.endswith(".") or ".." in host
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.netloc.count("@")
            or port not in (None, 443 if parsed.scheme == "https" else 80)):
        raise FetchFailure("unsafe_url")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.lower().endswith((".local", ".internal", ".localhost")):
            raise FetchFailure("unsafe_url") from None
    else:
        if not literal.is_global:
            raise FetchFailure("unsafe_address")
    return parsed, host.lower(), port or (443 if parsed.scheme == "https" else 80)


async def _resolve(host, port):
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                     proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(record[4][0] for record in records))


async def _connect(address, port, hostname, tls):
    context = ssl.create_default_context() if tls else None
    return await asyncio.open_connection(host=address, port=port, ssl=context,
                                         server_hostname=hostname if tls else None)


def _address(addresses):
    if type(addresses) not in (list, tuple) or not addresses:
        raise FetchFailure("unsafe_address")
    for value in addresses:
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            raise FetchFailure("unsafe_address") from None
        if not parsed.is_global:
            raise FetchFailure("unsafe_address")
    return str(ipaddress.ip_address(addresses[0]))


def _parse_headers(header_bytes):
    try:
        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        version, status_text, *_ = lines[0].split(" ", 2)
        status = int(status_text)
        if version not in ("HTTP/1.0", "HTTP/1.1") or not (100 <= status <= 599):
            raise ValueError
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            if line[0] in " \t" or ":" not in line:
                raise ValueError
            name, value = line.split(":", 1)
            name = name.lower()
            value = value.strip()
            if (not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name)
                    or name in headers or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError
            headers[name] = value
        return status, headers
    except (ValueError, IndexError):
        raise FetchFailure("invalid_response") from None


async def _read_chunked(reader, limit):
    chunks = []
    total = 0
    while True:
        line = await reader.readline()
        if not line.endswith(b"\r\n") or len(line) > 128:
            raise FetchFailure("incomplete_response")
        try:
            size = int(line[:-2].split(b";", 1)[0], 16)
        except ValueError:
            raise FetchFailure("invalid_response") from None
        if size < 0 or total + size > limit:
            raise FetchFailure("oversize")
        if size == 0:
            if await reader.readexactly(2) != b"\r\n":
                raise FetchFailure("invalid_response")
            return b"".join(chunks)
        data = await reader.readexactly(size + 2)
        if not data.endswith(b"\r\n"):
            raise FetchFailure("invalid_response")
        chunks.append(data[:-2])
        total += size


async def _request(url, parsed, host, port, resolver, connector, max_bytes):
    addresses = await resolver(host, port)
    address = _address(addresses)
    reader, writer = await connector(address, port, host, parsed.scheme == "https")
    try:
        target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        host_header = host + (f":{port}" if parsed.port is not None else "")
        writer.write((f"GET {target} HTTP/1.1\r\nHost: {host_header}\r\n"
                      "Accept-Encoding: identity\r\nConnection: close\r\n"
                      "User-Agent: TechPulse-controlled-fetch/1\r\n\r\n").encode("ascii"))
        await writer.drain()
        header_bytes = await reader.readuntil(b"\r\n\r\n")
        if len(header_bytes) > _MAX_HEADERS:
            raise FetchFailure("invalid_response")
        status, headers = _parse_headers(header_bytes[:-4])
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise FetchFailure("unsupported_encoding")
        transfer = headers.get("transfer-encoding")
        length = headers.get("content-length")
        if transfer and (transfer.lower() != "chunked" or length is not None):
            raise FetchFailure("invalid_response")
        if transfer:
            body = await _read_chunked(reader, max_bytes)
        elif length is not None:
            if len(length) > 10 or not length.isascii() or not length.isdecimal():
                raise FetchFailure("invalid_response")
            size = int(length)
            if size > max_bytes:
                raise FetchFailure("oversize")
            body = await reader.readexactly(size)
        else:
            raise FetchFailure("incomplete_response")
        return status, headers, body, address
    finally:
        writer.close()
        await writer.wait_closed()


async def fetch_publisher(url, profile_id, *, resolver=None, connector=None,
                          timeout=10.0, max_bytes=2_000_000):
    """Return complete wire representation bytes or a reason without bytes.

    External test doubles may replace DNS and socket operations. Production
    always resolves once and connects to the selected public IP with original
    hostname TLS verification. No proxy, cookie or fallback route is used.
    """
    if (type(timeout) not in (int, float) or not 0 < timeout <= 30
            or type(max_bytes) is not int or not 0 < max_bytes <= 10_000_000):
        return FetchResult("invalid_budget")
    resolver = resolver or _resolve
    connector = connector or _connect

    async def attempt():
        current = url
        for hop in range(_MAX_REDIRECTS + 1):
            parsed, host, port = _target(current)
            decision = authorize_fetch(profile_id, host)
            if not decision.ready:
                raise FetchFailure(decision.reason)
            status, headers, body, address = await _request(
                current, parsed, host, port, resolver, connector, max_bytes)
            if status in _REDIRECTS:
                if hop == _MAX_REDIRECTS:
                    raise FetchFailure("redirect_limit")
                location = headers.get("location")
                if not location:
                    raise FetchFailure("invalid_redirect")
                next_url = urljoin(current, location)
                next_parsed, _, _ = _target(next_url)
                if parsed.scheme == "https" and next_parsed.scheme != "https":
                    raise FetchFailure("unsafe_redirect")
                current = next_url
                continue
            if status != 200:
                raise FetchFailure("http_status")
            safe = {name: headers[name] for name in ("content-type", "content-length")
                    if name in headers}
            content_type = headers.get("content-type", "")
            charset = re.search(r"(?:^|;)\s*charset=([A-Za-z0-9._-]+)", content_type, re.I)
            return FetchResult("fetched", body, hashlib.sha256(body).hexdigest(), current,
                               status, safe, decision.client, decision.client_revision,
                               decision.profile_revision, charset.group(1).lower() if charset else "unknown",
                               address)
        raise FetchFailure("redirect_limit")

    async def bounded_attempt():
        async with _CONCURRENCY:
            return await attempt()

    try:
        return await asyncio.wait_for(bounded_attempt(), timeout)
    except FetchFailure as exc:
        return FetchResult(exc.reason)
    except (asyncio.TimeoutError, TimeoutError):
        return FetchResult("timeout")
    except (OSError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return FetchResult("incomplete_response")

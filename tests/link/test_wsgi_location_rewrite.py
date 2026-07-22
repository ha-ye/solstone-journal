# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from solstone.convey import root as root_module
from solstone.convey.secure_listener.identity import ConveyIdentity
from solstone.convey.secure_listener.wsgi import (
    _normalize_location_headers,
    dispatch_stream,
)
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.client import _parse_http_response
from solstone.think.link.paths import authorized_clients_path
from tests.link.certless_helpers import (
    FakeStreamWriter,
    dispatch_request,
    make_convey_app,
    pl_identity,
)

FINGERPRINT = "sha256:" + ("a" * 64)


class StaticResponseApp:
    def __init__(
        self,
        *,
        status: str = "302 Found",
        headers: list[tuple[str, str]] | None = None,
        body: bytes = b"location",
    ) -> None:
        self._status = status
        self._headers = list(headers or [])
        self._body = body

    def wsgi_app(self, _environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response(self._status, list(self._headers))
        return [self._body]


async def _dispatch_raw_request(
    app: Any,
    identity: ConveyIdentity,
    raw: bytes,
) -> tuple[int, dict[str, str], bytes, FakeStreamWriter]:
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = FakeStreamWriter()
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        await dispatch_stream(app, identity, reader, writer, loop, executor)
    status, headers, body = _parse_http_response(bytes(writer.data))
    return status, headers, body, writer


def _normalize(
    headers: list[tuple[str, str]],
    host_header: str | None,
    request_scheme: str = "https",
) -> list[tuple[str, str]]:
    return _normalize_location_headers(headers, host_header, request_scheme)


@pytest.mark.asyncio
async def test_secure_listener_rewrites_strict_slash_redirects_to_origin_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch)
    store = AuthorizedClients(authorized_clients_path())
    store.add(FINGERPRINT, "phone", "inst-1")
    monkeypatch.setattr(root_module, "get_authorized_clients", lambda: store)

    for path, expected in (
        ("/app/home", "/app/home/"),
        ("/app/backup", "/app/backup/"),
    ):
        response = await dispatch_request(
            app,
            pl_identity(FINGERPRINT),
            "GET",
            path,
            headers={"host": "127.0.0.1:54321"},
        )

        assert response.status == 308
        assert response.headers["location"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "host", "expected"),
    [
        ("/app/home/", "127.0.0.1:54321", "/app/home/"),
        (
            "https://example.test/app/home/",
            "127.0.0.1:54321",
            "https://example.test/app/home/",
        ),
        (
            "https://127.0.0.1:443/app/home/",
            "127.0.0.1:54321",
            "https://127.0.0.1:443/app/home/",
        ),
        ("mailto:a@b", "127.0.0.1:54321", "mailto:a@b"),
        (
            "https://127.0.0.1:notaport/x",
            "127.0.0.1:54321",
            "https://127.0.0.1:notaport/x",
        ),
        (
            "https://127.0.0.1:54321/app/home/",
            "a,b",
            "https://127.0.0.1:54321/app/home/",
        ),
        (
            "https://127.0.0.1:54321//evil.test/x",
            "127.0.0.1:54321",
            "https://127.0.0.1:54321//evil.test/x",
        ),
    ],
)
async def test_secure_listener_location_passthrough_cases_do_not_substitute_500(
    location: str,
    host: str,
    expected: str,
) -> None:
    response = await dispatch_request(
        StaticResponseApp(headers=[("Location", location)]),
        pl_identity(FINGERPRINT),
        "GET",
        "/",
        headers={"host": host},
    )

    assert response.status == 302
    assert response.headers["location"] == expected


@pytest.mark.asyncio
async def test_secure_listener_no_location_passthrough() -> None:
    response = await dispatch_request(
        StaticResponseApp(status="200 OK"),
        pl_identity(FINGERPRINT),
        "GET",
        "/",
        headers={"host": "127.0.0.1:54321"},
    )

    assert response.status == 200
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_secure_listener_absent_host_leaves_location_unchanged() -> None:
    raw = b"GET / HTTP/1.1\r\nContent-Length: 0\r\n\r\n"

    status, headers, _body, _writer = await _dispatch_raw_request(
        StaticResponseApp(headers=[("Location", "https://127.0.0.1:54321/app/home/")]),
        pl_identity(FINGERPRINT),
        raw,
    )

    assert status == 302
    assert headers["location"] == "https://127.0.0.1:54321/app/home/"


def test_normalize_location_headers_returns_new_list() -> None:
    headers = [("X-Test", "one"), ("Content-Type", "text/plain")]

    normalized = _normalize(headers, "example.test")

    assert normalized == headers
    assert normalized is not headers
    assert headers == [("X-Test", "one"), ("Content-Type", "text/plain")]


@pytest.mark.parametrize(
    ("host", "scheme", "location", "expected"),
    [
        ("EXAMPLE.test", "https", "https://example.TEST/app/home/", "/app/home/"),
        ("example.test", "https", "https://example.test:443/app/home/", "/app/home/"),
        ("example.test:443", "https", "https://example.test/app/home/", "/app/home/"),
        ("example.test", "http", "http://example.test/app/home/", "/app/home/"),
        ("[::1]:54321", "https", "https://[::1]:54321/x?q=1#f", "/x?q=1#f"),
        ("host:443", "https", "https://host:443?q=1#f", "/?q=1#f"),
        (
            "host:443",
            "https",
            "https://host:443/a%20b?q=a%2Fb&x=1#frag%201",
            "/a%20b?q=a%2Fb&x=1#frag%201",
        ),
        ("example.test", "https", "https://example.test/a//b", "/a//b"),
    ],
)
def test_normalize_location_headers_rewrites_same_journal_absolute_locations(
    host: str,
    scheme: str,
    location: str,
    expected: str,
) -> None:
    assert _normalize([("Location", location)], host, scheme) == [
        ("Location", expected)
    ]


@pytest.mark.parametrize(
    ("host", "scheme", "location"),
    [
        ("example.test", "https", "/app/home/"),
        ("example.test", "https", "//example.test/app/home/"),
        ("example.test", "https", "https://other.test/app/home/"),
        ("example.test:444", "https", "https://example.test/app/home/"),
        ("example.test", "https", "mailto:a@b"),
        ("example.test", "https", "https://example.test:notaport/x"),
        (None, "https", "https://example.test/x"),
        ("a,b", "https", "https://example.test/x"),
        ("user@example.test", "https", "https://example.test/x"),
        ("example.test/path", "https", "https://example.test/x"),
        ("example.test?x=1", "https", "https://example.test/x"),
        ("example.test:notaport", "https", "https://example.test/x"),
        ("example.test", "ftp", "https://example.test/x"),
        ("example.test", "https", "http://example.test/x"),
        ("example.test", "https", "https://example.test//evil.test/x"),
        ("example.test", "https", "https://example.test/\\evil.test/x"),
    ],
)
def test_normalize_location_headers_fail_closed_cases(
    host: str | None,
    scheme: str,
    location: str,
) -> None:
    headers = [("Location", location)]

    assert _normalize(headers, host, scheme) == headers


def test_normalize_location_headers_handles_duplicates_and_preserves_order() -> None:
    headers = [
        ("X-Before", "1"),
        ("LoCaTiOn", "https://example.test/a"),
        ("location", "https://external.test/b"),
        ("X-After", "2"),
    ]

    normalized = _normalize(headers, "example.test")

    assert normalized == [
        ("X-Before", "1"),
        ("LoCaTiOn", "/a"),
        ("location", "https://external.test/b"),
        ("X-After", "2"),
    ]
    assert headers == [
        ("X-Before", "1"),
        ("LoCaTiOn", "https://example.test/a"),
        ("location", "https://external.test/b"),
        ("X-After", "2"),
    ]

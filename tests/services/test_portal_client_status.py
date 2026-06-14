# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import socket
import ssl
import urllib.error

import pytest

from solstone.think.services import portal_client


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status


@pytest.fixture(autouse=True)
def _portal_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portal_client, "portal_base_url", lambda: "https://portal.test")


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://portal.test/account/scout/status",
        status,
        "error",
        hdrs=None,
        fp=None,
    )


def test_check_scout_status_sends_bearer_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response(b'{"status": "pending"}')

    monkeypatch.setattr(portal_client.urllib.request, "urlopen", fake_urlopen)

    outcome = portal_client.check_scout_status(
        "dispatch-token",
        timeout=7,
        component="test",
    )

    assert outcome.kind == "ok"
    assert outcome.server_status == "pending"
    request, timeout = calls[0]
    assert timeout == 7
    assert request.full_url == "https://portal.test/account/scout/status"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer dispatch-token"
    assert request.get_header("Connection") == "close"
    assert request.get_header("User-agent").startswith("solstone-test/")


@pytest.mark.parametrize("server_status", ["pending", "approved", "revoked"])
def test_check_scout_status_valid_statuses(
    monkeypatch: pytest.MonkeyPatch,
    server_status: str,
) -> None:
    monkeypatch.setattr(
        portal_client.urllib.request,
        "urlopen",
        lambda _request, timeout: _Response(
            json.dumps({"status": server_status}).encode("utf-8")
        ),
    )

    outcome = portal_client.check_scout_status("dispatch-token")

    assert outcome.kind == "ok"
    assert outcome.server_status == server_status
    assert outcome.reason is None


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (ssl.SSLError("cert failed"), "tls_failed"),
        (urllib.error.URLError(ssl.SSLError("cert failed")), "tls_failed"),
        (urllib.error.URLError("down"), "unreachable"),
        (urllib.error.URLError(socket.timeout("timed out")), "unreachable"),
        (socket.timeout("timed out"), "unreachable"),
        (TimeoutError("timed out"), "unreachable"),
        (_http_error(401), "unauthorized"),
        (_http_error(404), "not_found"),
        (_http_error(500), "unreachable"),
    ],
)
def test_check_scout_status_failure_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
    reason: str,
) -> None:
    def fake_urlopen(_request, timeout):
        raise raised

    monkeypatch.setattr(portal_client.urllib.request, "urlopen", fake_urlopen)

    outcome = portal_client.check_scout_status("dispatch-token")

    assert outcome.kind == "failed"
    assert outcome.reason == reason
    assert outcome.server_status is None


def test_check_scout_status_non_200_status_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        portal_client.urllib.request,
        "urlopen",
        lambda _request, timeout: _Response(b'{"status": "pending"}', status=503),
    )

    outcome = portal_client.check_scout_status("dispatch-token")

    assert outcome.kind == "failed"
    assert outcome.reason == "unreachable"


@pytest.mark.parametrize(
    "response",
    [
        _Response(b"", status=200),
        _Response(b"", status=204),
        _Response(b"\xff", status=200),
        _Response(b"{", status=200),
        _Response(b"[]", status=200),
        _Response(b"{}", status=200),
        _Response(b'{"status": "bogus"}', status=200),
    ],
)
def test_check_scout_status_malformed_bodies(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
) -> None:
    monkeypatch.setattr(
        portal_client.urllib.request,
        "urlopen",
        lambda _request, timeout: response,
    )

    outcome = portal_client.check_scout_status("dispatch-token")

    assert outcome.kind == "failed"
    assert outcome.reason == "malformed"

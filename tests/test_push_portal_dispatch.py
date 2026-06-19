# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.request import Request

import pytest

from solstone.convey.sol_initiated.copy import (
    APNS_CATEGORY_SOL_CHAT_REQUEST,
    KIND_OWNER_CHAT_OPEN,
)
from solstone.think.push import devices, portal_dispatch


class FakeResponse:
    def __init__(self, body: dict[str, Any], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


def _setup_journal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.delenv("SERVICES_PORTAL_URL", raising=False)


def _register_device(
    fingerprint: str,
    token: str,
    *,
    environment: str = "development",
) -> None:
    devices.register_device(
        fingerprint=fingerprint,
        token=token,
        bundle_id="org.solpbc.solstone-swift",
        environment=environment,
        platform="ios",
    )


def _capture_urlopen(
    monkeypatch: pytest.MonkeyPatch, response_body: dict[str, Any] | None = None
) -> list[Request]:
    requests: list[Request] = []
    body = {"ok": True} if response_body is None else response_body

    def fake_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        requests.append(request)
        return FakeResponse(body)

    monkeypatch.setattr(portal_dispatch.urllib_request, "urlopen", fake_urlopen)
    return requests


def _headers(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def _body(request: Request) -> dict[str, Any]:
    assert isinstance(request.data, bytes)
    payload = json.loads(request.data.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_dispatch_body_includes_normalized_registered_devices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_journal(monkeypatch, tmp_path)
    monkeypatch.setattr(portal_dispatch, "ensure_reach_token", lambda: "tok")
    _register_device("fp-1", "a" * 64)
    _register_device("fp-2", "b" * 64, environment="production")
    requests = _capture_urlopen(monkeypatch)

    result = portal_dispatch.dispatch_via_portal(
        request_id="req-1",
        summary="hello",
        category=APNS_CATEGORY_SOL_CHAT_REQUEST,
    )

    assert result == {"ok": True}
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://services.solstone.app/push/dispatch"
    headers = _headers(request)
    assert headers["authorization"] == "Bearer tok"
    assert headers["content-type"] == "application/json"
    assert _body(request) == {
        "request_id": "req-1",
        "summary": "hello",
        "category": APNS_CATEGORY_SOL_CHAT_REQUEST,
        "devices": [
            {
                "token": "a" * 64,
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "sandbox",
            },
            {
                "token": "b" * 64,
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "production",
            },
        ],
    }


def test_dispatch_uses_reach_token_for_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_journal(monkeypatch, tmp_path)
    monkeypatch.setattr(
        portal_dispatch, "ensure_reach_token", lambda: "reach-config-token"
    )
    _register_device("fp-1", "a" * 64)
    requests = _capture_urlopen(monkeypatch)

    result = portal_dispatch.dispatch_via_portal(
        request_id="req-1",
        summary="hello",
        category=APNS_CATEGORY_SOL_CHAT_REQUEST,
    )

    assert result == {"ok": True}
    assert _headers(requests[0])["authorization"] == "Bearer reach-config-token"


def test_revoked_tokens_prune_matching_devices_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_journal(monkeypatch, tmp_path)
    monkeypatch.setattr(portal_dispatch, "ensure_reach_token", lambda: "tok")
    _register_device("fp-1", "a" * 64)
    _register_device("fp-2", "b" * 64)
    _register_device("fp-3", "c" * 64)
    requests = _capture_urlopen(
        monkeypatch,
        {"ok": True, "revoked_tokens": ["b" * 64, "missing", "", 3]},
    )

    result = portal_dispatch.dispatch_via_portal(
        request_id="req-1",
        summary="hello",
        category=APNS_CATEGORY_SOL_CHAT_REQUEST,
    )

    assert result == {"ok": True, "revoked_tokens": ["b" * 64, "missing", "", 3]}
    assert len(requests) == 1
    assert {device["token"] for device in devices.load_devices()} == {
        "a" * 64,
        "c" * 64,
    }

    requests = _capture_urlopen(
        monkeypatch, {"ok": True, "revoked_tokens": ["missing"]}
    )
    before = devices.load_devices()
    result = portal_dispatch.dispatch_dedup_via_portal(
        request_id="req-1",
        action=KIND_OWNER_CHAT_OPEN,
    )

    assert result == {"ok": True, "revoked_tokens": ["missing"]}
    assert devices.load_devices() == before
    assert _body(requests[0]) == {
        "request_id": "req-1",
        "action": KIND_OWNER_CHAT_OPEN,
        "devices": [
            {
                "token": "a" * 64,
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "sandbox",
            },
            {
                "token": "c" * 64,
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "sandbox",
            },
        ],
    }


def test_dispatch_short_circuits_empty_registry_without_posting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_journal(monkeypatch, tmp_path)
    requests: list[Request] = []

    def fail_ensure() -> str:
        raise AssertionError("ensure_reach_token should not be called")

    def fail_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        requests.append(request)
        raise AssertionError("urlopen should not be called")

    monkeypatch.setattr(portal_dispatch, "ensure_reach_token", fail_ensure)
    monkeypatch.setattr(portal_dispatch.urllib_request, "urlopen", fail_urlopen)

    result = portal_dispatch.dispatch_via_portal(
        request_id="req-1",
        summary="hello",
        category=APNS_CATEGORY_SOL_CHAT_REQUEST,
    )

    assert result is None
    assert requests == []

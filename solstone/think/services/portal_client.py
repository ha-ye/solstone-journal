# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared client helpers for services portal handoff flows."""

from __future__ import annotations

import json
import secrets
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from solstone.think.services.constants import (
    NONCE_ALPHABET,
    NONCE_LENGTH_CHARS,
    SERVICE_SCOUT,
    SERVICE_SPL,
    SUPPORTED_SERVICES,
)

DEFAULT_PORTAL_URL = "https://services.solstone.app"
POLL_TIMEOUT_SECONDS = 35
DEFAULT_WAIT_SECONDS = 900


@dataclass(frozen=True)
class PollOutcome:
    kind: str
    payload: dict[str, Any] | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ScoutStatusOutcome:
    kind: str
    server_status: str | None = None
    reason: str | None = None
    detail: str | None = None


_SCOUT_STATUS_VALUES = frozenset({"pending", "approved", "revoked"})


def mint_nonce() -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(NONCE_LENGTH_CHARS))


def portal_base_url() -> str:
    import os

    return os.environ.get("SERVICES_PORTAL_URL", DEFAULT_PORTAL_URL).rstrip("/")


def _package_version() -> str:
    try:
        return _pkg_version("solstone")
    except PackageNotFoundError:
        return "0.0.0+source"


def request_headers(component: str) -> dict[str, str]:
    return {
        "User-Agent": f"solstone-{component}/{_package_version()}",
        "Connection": "close",
    }


def poll_url(base_url: str, nonce: str, *, service: str = SERVICE_SCOUT) -> str:
    if service not in SUPPORTED_SERVICES:
        raise ValueError(f"unsupported handoff service: {service!r}")
    return f"{base_url}/handoff/{service}?nonce={nonce}"


def browser_url(
    base_url: str,
    nonce: str,
    *,
    service: str = SERVICE_SCOUT,
    instance: str | None = None,
) -> str:
    if service not in SUPPORTED_SERVICES:
        raise ValueError(f"unsupported handoff service: {service!r}")
    url = f"{base_url}/enable/{service}?nonce={nonce}"
    if service == SERVICE_SPL and instance:
        url = f"{url}&instance={urllib.parse.quote(instance, safe='')}"
    return url


def is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (socket.timeout, TimeoutError))
    return False


def handle_http_status(status: int) -> PollOutcome:
    if status == 400:
        return PollOutcome(kind="failed", reason="nonce_invalid")
    if status == 410:
        return PollOutcome(kind="failed", reason="consent_link_expired")
    return PollOutcome(kind="failed", reason="unexpected_payload")


def read_handoff_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("handoff payload must be a JSON object")
    return payload


def _failed_status(reason: str, detail: str | None = None) -> ScoutStatusOutcome:
    return ScoutStatusOutcome(kind="failed", reason=reason, detail=detail)


def _read_scout_status(raw_body: bytes) -> str:
    if not raw_body:
        raise ValueError("scout status response was empty")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("scout status payload must be a JSON object")
    server_status = payload.get("status")
    if server_status not in _SCOUT_STATUS_VALUES:
        raise ValueError("scout status payload has unknown status")
    return str(server_status)


def check_scout_status(
    dispatch_token: str,
    *,
    timeout: float = 10.0,
    component: str = "status",
) -> ScoutStatusOutcome:
    headers = request_headers(component)
    headers["Authorization"] = f"Bearer {dispatch_token}"
    request = urllib.request.Request(
        f"{portal_base_url()}/account/scout/status",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        if status == 401:
            return _failed_status("unauthorized", str(exc))
        if status == 404:
            return _failed_status("not_found", str(exc))
        return _failed_status("unreachable", str(exc))
    except ssl.SSLError as exc:
        return _failed_status("tls_failed", str(exc))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            return _failed_status("tls_failed", str(exc.reason))
        return _failed_status("unreachable", str(exc))
    except (socket.timeout, TimeoutError) as exc:
        return _failed_status("unreachable", str(exc))

    if status == 204:
        return _failed_status("malformed", "scout status response was empty")
    if status != 200:
        return _failed_status("unreachable", f"unexpected HTTP status {status}")

    try:
        server_status = _read_scout_status(raw_body)
    except ValueError as exc:
        return _failed_status("malformed", str(exc))
    return ScoutStatusOutcome(kind="ok", server_status=server_status)


def poll_handoff_once(
    base_url: str,
    nonce: str,
    *,
    timeout: float = POLL_TIMEOUT_SECONDS,
    component: str = "cli",
    service: str = SERVICE_SCOUT,
) -> PollOutcome:
    url = poll_url(base_url, nonce, service=service)
    request = urllib.request.Request(
        url,
        headers=request_headers(component),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        return handle_http_status(int(exc.code))
    except ssl.SSLError as exc:
        return PollOutcome(
            kind="failed",
            reason="tls_verification_failed",
            detail=str(exc),
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            return PollOutcome(
                kind="failed",
                reason="tls_verification_failed",
                detail=str(exc.reason),
            )
        if is_timeout_error(exc):
            return PollOutcome(kind="continue")
        return PollOutcome(
            kind="failed",
            reason="portal_unreachable",
            detail=str(exc),
        )
    except (socket.timeout, TimeoutError):
        return PollOutcome(kind="continue")

    if status == 200:
        try:
            return PollOutcome(kind="success", payload=read_handoff_payload(raw_body))
        except ValueError as exc:
            return PollOutcome(
                kind="failed",
                reason="unexpected_payload",
                detail=str(exc),
            )
    if status == 204:
        return PollOutcome(kind="continue")
    return handle_http_status(status)

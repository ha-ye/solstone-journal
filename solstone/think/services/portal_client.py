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


def poll_url(base_url: str, nonce: str, *, service: str) -> str:
    if service not in SUPPORTED_SERVICES:
        raise ValueError(f"unsupported handoff service: {service!r}")
    return f"{base_url}/handoff/{service}?nonce={nonce}"


def browser_url(
    base_url: str,
    nonce: str,
    *,
    service: str,
    instance: str | None = None,
) -> str:
    if service not in SUPPORTED_SERVICES:
        raise ValueError(f"unsupported handoff service: {service!r}")
    url = f"{base_url}/enable/{service}?nonce={nonce}"
    if instance:
        url = f"{url}&instance={urllib.parse.quote(instance, safe='')}"
    return url


def build_consent_url(
    service: str, *, instance: str | None = None
) -> tuple[str, str, str]:
    """Mint a nonce and build the portal consent URL for ``service``.

    Returns ``(consent_url, nonce, base_url)``.
    """

    base_url = portal_base_url()
    nonce = mint_nonce()
    return (
        browser_url(base_url, nonce, service=service, instance=instance),
        nonce,
        base_url,
    )


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


def poll_handoff_once(
    base_url: str,
    nonce: str,
    *,
    timeout: float = POLL_TIMEOUT_SECONDS,
    component: str = "cli",
    service: str,
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
            payload = read_handoff_payload(raw_body)
        except ValueError as exc:
            return PollOutcome(
                kind="failed",
                reason="unexpected_payload",
                detail=str(exc),
            )
        if payload.get("state") == "early_access":
            return PollOutcome(kind="early_access")
        return PollOutcome(kind="success", payload=payload)
    if status == 204:
        return PollOutcome(kind="continue")
    return handle_http_status(status)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared client helpers for services portal handoff flows."""

from __future__ import annotations

import json
import secrets
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, Literal

from solstone.think.services.constants import (
    DEVICE_CODE_REGEX,
    NONCE_ALPHABET,
    NONCE_LENGTH_CHARS,
    NONCE_REGEX,
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
class DeviceCodeOutcome:
    kind: Literal["success", "failed"]
    nonce: str | None = None
    code: str | None = None
    expires_in: int | None = None
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


def poll_url(base_url: str, nonce: str) -> str:
    return f"{base_url}/handoff/scout?nonce={nonce}"


def device_code_mint_url(base_url: str) -> str:
    return f"{base_url}/enable/scout/code"


def device_code_entry_url(base_url: str) -> str:
    return f"{base_url}/enable/scout"


def browser_url(base_url: str, nonce: str) -> str:
    return f"{base_url}/enable/scout?nonce={nonce}"


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


def _handle_mint_status(status: int) -> str:
    if status == 400:
        return "nonce_invalid"
    if status == 429:
        return "rate_limited"
    return "unexpected_payload"


def read_handoff_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("handoff payload must be a JSON object")
    return payload


def _read_device_code_payload(raw_body: bytes) -> tuple[str, str, int]:
    payload = read_handoff_payload(raw_body)
    nonce = payload.get("nonce")
    code = payload.get("code")
    expires_in = payload.get("expires_in")
    if not isinstance(nonce, str) or not NONCE_REGEX.fullmatch(nonce):
        raise ValueError("device-code payload nonce was invalid")
    if not isinstance(code, str) or not DEVICE_CODE_REGEX.fullmatch(code):
        raise ValueError("device-code payload code was invalid")
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise ValueError("device-code payload expires_in must be a positive integer")
    return nonce, code, expires_in


def mint_device_code(base_url: str) -> DeviceCodeOutcome:
    request = urllib.request.Request(
        device_code_mint_url(base_url),
        data=b"",
        headers=request_headers("cli"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=POLL_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        return DeviceCodeOutcome(
            kind="failed", reason=_handle_mint_status(int(exc.code))
        )
    except ssl.SSLError as exc:
        return DeviceCodeOutcome(
            kind="failed",
            reason="tls_verification_failed",
            detail=str(exc),
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            return DeviceCodeOutcome(
                kind="failed",
                reason="tls_verification_failed",
                detail=str(exc.reason),
            )
        return DeviceCodeOutcome(
            kind="failed",
            reason="portal_unreachable",
            detail=str(exc),
        )
    except (socket.timeout, TimeoutError) as exc:
        return DeviceCodeOutcome(
            kind="failed",
            reason="portal_unreachable",
            detail=str(exc),
        )

    if status != 200:
        return DeviceCodeOutcome(kind="failed", reason=_handle_mint_status(status))
    try:
        nonce, code, expires_in = _read_device_code_payload(raw_body)
    except ValueError as exc:
        return DeviceCodeOutcome(
            kind="failed",
            reason="unexpected_payload",
            detail=str(exc),
        )
    return DeviceCodeOutcome(
        kind="success",
        nonce=nonce,
        code=code,
        expires_in=expires_in,
    )


def poll_handoff_once(
    base_url: str,
    nonce: str,
    *,
    timeout: float = POLL_TIMEOUT_SECONDS,
    component: str = "cli",
) -> PollOutcome:
    request = urllib.request.Request(
        poll_url(base_url, nonce),
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

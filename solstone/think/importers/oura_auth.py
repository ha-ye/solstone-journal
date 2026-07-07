# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-present Oura OAuth helpers and journal-config token storage.

Token boundary: device OAuth material (access + refresh tokens, and the
confidential-client secret when Oura's registration requires one) lives in
journal config under the reserved ``oura`` key — the journal is the one
trusted store (owner ruling, 2026-07-07; no machine-local carve-out for
device tokens). All reads and writes route through the config owner
``solstone.think.journal_config`` (L2), with read-modify-write saves under
``hold_config_lock``. Nothing token-shaped is ever printed or logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from solstone.think.journal_config import (
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
CALLBACK_URL = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"

# Journal-config section holding the Oura OAuth material:
# config/journal.json -> {"oura": {"client_id", "client_secret", "tokens"}}.
CONFIG_SECTION = "oura"

# OAuth scopes the connect flow requests, researched 2026-07-07:
#
# - The first eight are Oura's documented scope set
#   (cloud.ouraring.com/docs/authentication).
# - ``stress`` (daily_resilience), ``heart_health``
#   (daily_cardiovascular_age, vo2_max), and ``metabolic`` (blood_glucose)
#   are undocumented on that page but are live in Oura's scope system:
#   tidepool-org/platform's Oura integration maps exactly these
#   ``extapi:``-prefixed scopes to those endpoints, and Oura's own
#   authorize front door verifiably rewrites a plain ``scope=X`` query to
#   ``extapi:X`` before handing off to its authorization server — so the
#   plain names below are the correct request form.
# - Empirically (2026-07-06), a token granted with NO scope parameter
#   reads resilience and cardiovascular-age documents but 401s on
#   usercollection/blood_glucose: ``metabolic`` is not part of the default
#   grant and must be requested explicitly, which is why this list exists.
#
# The consent screen (owner-present) is the final validator: scope names
# Oura rejects or drops surface there, with the owner at the keyboard.
OAUTH_SCOPES: tuple[str, ...] = (
    "email",
    "personal",
    "daily",
    "heartrate",
    "workout",
    "tag",
    "session",
    "spo2",
    "stress",
    "heart_health",
    "metabolic",
)

HttpTransport = Callable[
    [str, dict[str, str], dict[str, str], float], Mapping[str, Any]
]
BrowserOpen = Callable[[str], bool]


class OuraAuthError(RuntimeError):
    """Raised when local owner-present Oura OAuth cannot complete."""


@dataclass(frozen=True, slots=True)
class OuraTokens:
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"


def load_oura_tokens(journal_root: Path | None = None) -> OuraTokens | None:
    """Load Oura OAuth tokens from journal config, if present.

    Absent tokens return ``None`` (the authorization-needed path); a
    present-but-malformed ``oura.tokens`` object fails loudly rather than
    masquerading as "never connected".
    """

    section = read_journal_config(journal_root).get(CONFIG_SECTION)
    payload = section.get("tokens") if isinstance(section, dict) else None
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise OuraAuthError(
            "Journal config oura.tokens is malformed (expected an object); "
            "repair config/journal.json or re-run: journal importer --connect oura"
        )
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_at = payload.get("expires_at")
    token_type = payload.get("token_type", "Bearer")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or not isinstance(expires_at, int | float)
        or not isinstance(token_type, str)
        or not token_type
    ):
        raise OuraAuthError(
            "Journal config oura.tokens is missing required fields; "
            "repair config/journal.json or re-run: journal importer --connect oura"
        )
    return OuraTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expires_at),
        token_type=token_type,
    )


def save_oura_tokens(tokens: OuraTokens, journal_root: Path | None = None) -> None:
    """Persist Oura OAuth tokens into journal config.

    Read-modify-write under the config lock through the config owner
    (L2: ``journal_config.py`` owns ``config/journal.json``); every other
    config key — including ``oura.client_id`` / ``oura.client_secret`` —
    is preserved untouched.
    """

    with hold_config_lock(journal_root):
        config = read_journal_config(journal_root)
        section = config.setdefault(CONFIG_SECTION, {})
        if not isinstance(section, dict):
            raise OuraAuthError(
                "Journal config 'oura' section is not an object; "
                "repair config/journal.json before connecting."
            )
        section["tokens"] = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
        }
        write_journal_config(config, journal_root)


def load_oura_client_secret(journal_root: Path | None = None) -> str | None:
    """Confidential-client secret from journal config, if configured.

    Server-side-flow Oura apps require the client secret at token exchange
    and refresh; an absent value means a public (PKCE-only) client and
    token grants go out without it.
    """

    section = read_journal_config(journal_root).get(CONFIG_SECTION)
    secret = section.get("client_secret") if isinstance(section, dict) else None
    if not isinstance(secret, str):
        return None
    return secret.strip() or None


def run_owner_present_auth(
    *,
    client_id: str,
    timeout_s: float = 300,
    http_transport: HttpTransport | None = None,
    browser_open: BrowserOpen | None = None,
    scopes: Sequence[str] = OAUTH_SCOPES,
    journal_root: Path | None = None,
) -> OuraTokens:
    """Run fixed-port owner-present OAuth and return exchanged Oura tokens."""
    if timeout_s <= 0:
        raise OuraAuthError("Oura authorization timed out.")
    transport = http_transport or _default_http_transport
    opener = browser_open or webbrowser.open
    state = secrets.token_urlsafe(32)
    verifier = _new_code_verifier()
    auth_url = _authorization_url(
        client_id=client_id,
        state=state,
        code_challenge=_code_challenge(verifier),
        scopes=scopes,
    )

    try:
        server = _CallbackHTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    except OSError as exc:
        raise OuraAuthError(
            "Oura authorization callback port 8765 is unavailable."
        ) from exc
    server.expected_state = state
    server.timeout = 0.1
    try:
        if not opener(auth_url):
            raise OuraAuthError("Could not open Oura authorization page.")
        code = _wait_for_code(server, timeout_s=timeout_s)
    finally:
        server.server_close()

    return _exchange_authorization_code(
        code=code,
        verifier=verifier,
        client_id=client_id,
        transport=transport,
        timeout_s=timeout_s,
        journal_root=journal_root,
    )


def refresh_tokens(
    tokens: OuraTokens,
    *,
    client_id: str,
    http_transport: HttpTransport | None = None,
    timeout_s: float = 30,
    journal_root: Path | None = None,
) -> OuraTokens:
    """Refresh Oura OAuth tokens using the standard refresh-token grant."""
    transport = http_transport or _default_http_transport
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client_id,
    }
    _attach_client_secret(data, journal_root)
    payload = _post_token_request(data, transport=transport, timeout_s=timeout_s)
    return _tokens_from_response(payload)


def _attach_client_secret(data: dict[str, str], journal_root: Path | None) -> None:
    """Server-side-flow Oura apps require the client secret on token grants.

    The secret lives in journal config (oura.client_secret) alongside the
    tokens; an absent secret means a public PKCE-only client and the
    request goes out unchanged.
    """
    secret = load_oura_client_secret(journal_root)
    if secret:
        data["client_secret"] = secret


def _authorization_url(
    *,
    client_id: str,
    state: str,
    code_challenge: str,
    scopes: Sequence[str],
) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK_URL,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        query["scope"] = " ".join(scopes)
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def _exchange_authorization_code(
    *,
    code: str,
    verifier: str,
    client_id: str,
    transport: HttpTransport,
    timeout_s: float,
    journal_root: Path | None,
) -> OuraTokens:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK_URL,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    _attach_client_secret(data, journal_root)
    payload = _post_token_request(data, transport=transport, timeout_s=timeout_s)
    return _tokens_from_response(payload)


def _post_token_request(
    data: dict[str, str],
    *,
    transport: HttpTransport,
    timeout_s: float,
) -> Mapping[str, Any]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        payload = transport(TOKEN_URL, data, headers, timeout_s)
    except Exception:
        raise OuraAuthError("Oura token exchange failed.") from None
    if not isinstance(payload, Mapping):
        raise OuraAuthError("Oura token exchange returned an invalid response.")
    return payload


def _tokens_from_response(payload: Mapping[str, Any]) -> OuraTokens:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    token_type = payload.get("token_type", "Bearer")
    expires_at = payload.get("expires_at")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise OuraAuthError("Oura token exchange returned an invalid response.")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OuraAuthError("Oura token exchange returned an invalid response.")
    if not isinstance(token_type, str) or not token_type:
        raise OuraAuthError("Oura token exchange returned an invalid response.")
    if isinstance(expires_at, int | float):
        resolved_expires_at = float(expires_at)
    elif isinstance(expires_in, int | float):
        resolved_expires_at = time.time() + float(expires_in)
    else:
        raise OuraAuthError("Oura token exchange returned an invalid response.")
    return OuraTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=resolved_expires_at,
        token_type=token_type,
    )


def _wait_for_code(server: "_CallbackHTTPServer", *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        server.handle_request()
        if server.auth_code:
            return server.auth_code
    raise OuraAuthError("Oura authorization timed out.")


def _new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _default_http_transport(
    url: str, data: dict[str, str], headers: dict[str, str], timeout_s: float
) -> Mapping[str, Any]:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


class _CallbackHTTPServer(HTTPServer):
    allow_reuse_address = True

    expected_state: str
    auth_code: str | None = None
    _lock = threading.Lock()

    def record_code(self, code: str) -> None:
        with self._lock:
            self.auth_code = code


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackHTTPServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._reject()
            return
        params = urllib.parse.parse_qs(parsed.query)
        code = _single_param(params, "code")
        state = _single_param(params, "state")
        if state != self.server.expected_state or not code:
            self._reject()
            return
        self.server.record_code(code)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Oura authorization received. You can return to Solstone.")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reject(self) -> None:
        self.send_response(400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Oura authorization rejected.")


def _single_param(params: Mapping[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if not values or len(values) != 1:
        return None
    return values[0]

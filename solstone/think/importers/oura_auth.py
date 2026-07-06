# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-present Oura OAuth helpers for local importer auth."""

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
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from solstone.think.importers.local_secrets import OuraTokens

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
CALLBACK_URL = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"

HttpTransport = Callable[
    [str, dict[str, str], dict[str, str], float], Mapping[str, Any]
]
BrowserOpen = Callable[[str], bool]


class OuraAuthError(RuntimeError):
    """Raised when local owner-present Oura OAuth cannot complete."""


def run_owner_present_auth(
    *,
    client_id: str,
    timeout_s: float = 300,
    http_transport: HttpTransport | None = None,
    browser_open: BrowserOpen | None = None,
    scopes: Sequence[str] | None = None,
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
    )


def refresh_tokens(
    tokens: OuraTokens,
    *,
    client_id: str,
    http_transport: HttpTransport | None = None,
    timeout_s: float = 30,
) -> OuraTokens:
    """Refresh Oura OAuth tokens using the standard refresh-token grant."""
    transport = http_transport or _default_http_transport
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client_id,
    }
    payload = _post_token_request(data, transport=transport, timeout_s=timeout_s)
    return _tokens_from_response(payload)


def _authorization_url(
    *,
    client_id: str,
    state: str,
    code_challenge: str,
    scopes: Sequence[str] | None,
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
) -> OuraTokens:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK_URL,
        "client_id": client_id,
        "code_verifier": verifier,
    }
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

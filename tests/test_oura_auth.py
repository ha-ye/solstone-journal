# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from solstone.think.importers.local_secrets import OuraTokens
from solstone.think.importers.oura_auth import (
    CALLBACK_URL,
    OuraAuthError,
    refresh_tokens,
    run_owner_present_auth,
)

pytestmark = pytest.mark.xdist_group("oura_auth_loopback")


def _request_callback(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=2).read()
    except urllib.error.HTTPError:
        pass


def _state_from_auth_url(auth_url: str) -> str:
    parsed = urllib.parse.urlparse(auth_url)
    params = urllib.parse.parse_qs(parsed.query)
    return params["state"][0]


def test_owner_present_auth_uses_fixed_redirect_pkce_and_private_token_exchange():
    opened_urls: list[str] = []
    exchanges: list[dict[str, str]] = []

    def browser_open(url: str) -> bool:
        opened_urls.append(url)
        state = _state_from_auth_url(url)
        threading.Thread(
            target=lambda: (
                time.sleep(0.05),
                _request_callback(
                    f"{CALLBACK_URL}?code=owner-code-sensitive&state={state}"
                ),
            ),
            daemon=True,
        ).start()
        return True

    def transport(
        url: str,
        data: dict[str, str],
        headers: dict[str, str],
        timeout_s: float,
    ) -> dict[str, object]:
        exchanges.append(data)
        assert url.endswith("/oauth/token")
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert data["grant_type"] == "authorization_code"
        assert data["client_id"] == "client-public-id"
        assert data["redirect_uri"] == CALLBACK_URL
        assert data["code"] == "owner-code-sensitive"
        assert data["code_verifier"]
        assert timeout_s > 0
        return {
            "access_token": "access-token-sensitive",
            "refresh_token": "refresh-token-sensitive",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    tokens = run_owner_present_auth(
        client_id="client-public-id",
        timeout_s=2,
        http_transport=transport,
        browser_open=browser_open,
    )

    assert tokens.access_token == "access-token-sensitive"
    assert tokens.refresh_token == "refresh-token-sensitive"
    assert tokens.token_type == "Bearer"
    assert tokens.expires_at > time.time()
    assert len(exchanges) == 1

    opened = urllib.parse.urlparse(opened_urls[0])
    params = urllib.parse.parse_qs(opened.query)
    assert opened.scheme == "https"
    assert params["client_id"] == ["client-public-id"]
    assert params["redirect_uri"] == [CALLBACK_URL]
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"][0]
    assert params["code_challenge"][0]


def test_wrong_state_and_wrong_path_do_not_exchange_tokens():
    opened_urls: list[str] = []
    exchange_called = False

    def browser_open(url: str) -> bool:
        opened_urls.append(url)
        state = _state_from_auth_url(url)

        def send_bad_callbacks() -> None:
            time.sleep(0.05)
            _request_callback(
                f"http://localhost:8765/not-callback?code=x&state={state}"
            )
            _request_callback(f"{CALLBACK_URL}?code=x&state=wrong-state")

        threading.Thread(target=send_bad_callbacks, daemon=True).start()
        return True

    def transport(
        url: str,
        data: dict[str, str],
        headers: dict[str, str],
        timeout_s: float,
    ) -> dict[str, object]:
        nonlocal exchange_called
        exchange_called = True
        return {}

    with pytest.raises(OuraAuthError, match="timed out"):
        run_owner_present_auth(
            client_id="client-public-id",
            timeout_s=0.3,
            http_transport=transport,
            browser_open=browser_open,
        )

    assert opened_urls
    assert exchange_called is False


def test_owner_present_auth_timeout_is_sanitized():
    with pytest.raises(OuraAuthError) as excinfo:
        run_owner_present_auth(
            client_id="client-public-id",
            timeout_s=0.05,
            http_transport=lambda url, data, headers, timeout_s: {},
            browser_open=lambda url: True,
        )

    message = str(excinfo.value)
    assert "timed out" in message
    assert "client-public-id" not in message
    assert "access_token" not in message
    assert "refresh_token" not in message


def test_owner_present_auth_aborts_if_fixed_redirect_port_is_in_use():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 8765))
    sock.listen(1)
    try:
        with pytest.raises(OuraAuthError, match="port 8765 is unavailable"):
            run_owner_present_auth(
                client_id="client-public-id",
                timeout_s=0.05,
                http_transport=lambda url, data, headers, timeout_s: {},
                browser_open=lambda url: True,
            )
    finally:
        sock.close()


def test_token_exchange_errors_do_not_expose_token_substrings():
    def transport(
        url: str,
        data: dict[str, str],
        headers: dict[str, str],
        timeout_s: float,
    ) -> dict[str, object]:
        raise RuntimeError(
            "bad token response access-token-sensitive refresh-token-sensitive"
        )

    def browser_open(url: str) -> bool:
        state = _state_from_auth_url(url)
        threading.Thread(
            target=lambda: (
                time.sleep(0.05),
                _request_callback(
                    f"{CALLBACK_URL}?code=owner-code-sensitive&state={state}"
                ),
            ),
            daemon=True,
        ).start()
        return True

    with pytest.raises(OuraAuthError) as excinfo:
        run_owner_present_auth(
            client_id="client-public-id",
            timeout_s=2,
            http_transport=transport,
            browser_open=browser_open,
        )

    message = str(excinfo.value)
    assert "access-token-sensitive" not in message
    assert "refresh-token-sensitive" not in message
    assert "owner-code-sensitive" not in message
    assert excinfo.value.__cause__ is None


def test_refresh_tokens_uses_refresh_grant_without_exposing_existing_token():
    captured: dict[str, str] = {}

    def transport(
        url: str,
        data: dict[str, str],
        headers: dict[str, str],
        timeout_s: float,
    ) -> dict[str, object]:
        captured.update(data)
        return {
            "access_token": "new-access-sensitive",
            "refresh_token": "new-refresh-sensitive",
            "token_type": "Bearer",
            "expires_in": 7200,
        }

    refreshed = refresh_tokens(
        OuraTokens(
            access_token="old-access-sensitive",
            refresh_token="old-refresh-sensitive",
            expires_at=1700000000.0,
        ),
        client_id="client-public-id",
        http_transport=transport,
    )

    assert captured["grant_type"] == "refresh_token"
    assert captured["refresh_token"] == "old-refresh-sensitive"
    assert captured["client_id"] == "client-public-id"
    assert refreshed.access_token == "new-access-sensitive"
    assert refreshed.refresh_token == "new-refresh-sensitive"
    assert refreshed.expires_at > time.time()


def test_token_grants_attach_client_secret_only_when_present(tmp_path, monkeypatch):
    # Server-side-flow apps (Jack's registration) require the secret at
    # exchange and refresh; public PKCE clients must send requests
    # unchanged. The secret must come from the local boundary only.
    import solstone.think.importers.oura_auth as oura_auth
    from solstone.think.importers.local_secrets import OuraTokens

    monkeypatch.setenv("HOME", str(tmp_path))
    captured: list[dict] = []

    def transport(url, data, headers, timeout_s):
        captured.append(dict(data))
        return {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_at": 4102444800.0,
            "token_type": "Bearer",
        }

    tokens = OuraTokens("old-at", "old-rt", 4102444800.0)
    oura_auth.refresh_tokens(tokens, client_id="cid", http_transport=transport)
    assert "client_secret" not in captured[-1]

    secret_dir = (
        tmp_path / "Library" / "Application Support" / "Solstone" / "secrets" / "oura"
    )
    secret_dir.mkdir(parents=True)
    (secret_dir / "client_secret").write_text("shh-42\n", encoding="utf-8")
    oura_auth.refresh_tokens(tokens, client_id="cid", http_transport=transport)
    assert captured[-1]["client_secret"] == "shh-42"
    assert captured[-1]["code_verifier" if False else "client_id"] == "cid"

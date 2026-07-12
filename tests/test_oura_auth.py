# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from solstone.think.importers.oura_auth import (
    CALLBACK_URL,
    OAUTH_SCOPES,
    OuraAuthError,
    OuraTokens,
    load_oura_client_secret,
    load_oura_tokens,
    refresh_tokens,
    run_owner_present_auth,
    save_oura_tokens,
)

pytestmark = pytest.mark.xdist_group("oura_auth_loopback")


def _make_journal(tmp_path: Path, config: dict | None = None) -> Path:
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps(config if config is not None else {}), encoding="utf-8"
    )
    return journal


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
    # The full researched scope set is requested by default — including
    # the blood-glucose (metabolic) scope the default grant lacks.
    assert params["scope"] == [" ".join(OAUTH_SCOPES)]
    assert "metabolic" in params["scope"][0].split()
    assert "heart_health" in params["scope"][0].split()


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


def test_token_grants_attach_client_secret_only_when_present(tmp_path: Path):
    # Server-side-flow apps (Jack's registration) require the secret at
    # exchange and refresh; public PKCE clients must send requests
    # unchanged. The secret lives in journal config (oura.client_secret)
    # alongside the tokens — the journal is the one trusted store.
    captured: list[dict] = []

    def transport(url, data, headers, timeout_s):
        captured.append(dict(data))
        return {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_at": 4102444800.0,
            "token_type": "Bearer",
        }

    journal = _make_journal(tmp_path, {"oura": {"client_id": "cid"}})
    tokens = OuraTokens("old-at", "old-rt", 4102444800.0)
    refresh_tokens(
        tokens, client_id="cid", http_transport=transport, journal_root=journal
    )
    assert "client_secret" not in captured[-1]

    config_path = journal / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["oura"]["client_secret"] = "shh-42"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    refresh_tokens(
        tokens, client_id="cid", http_transport=transport, journal_root=journal
    )
    assert captured[-1]["client_secret"] == "shh-42"
    assert captured[-1]["client_id"] == "cid"


def test_load_oura_client_secret_reads_config_and_strips(tmp_path: Path):
    journal = _make_journal(tmp_path, {"oura": {"client_secret": " shh-42 \n"}})

    assert load_oura_client_secret(journal) == "shh-42"
    assert load_oura_client_secret(_make_journal(tmp_path / "bare")) is None


# ---------------------------------------------------------------------------
# Token storage — journal config is the one trusted store (owner ruling
# 2026-07-07): tokens live under oura.tokens.*, written only through the
# config owner, preserving every other config key.
# ---------------------------------------------------------------------------


def test_save_and_load_oura_tokens_round_trip_through_journal_config(
    tmp_path: Path,
):
    journal = _make_journal(
        tmp_path,
        {
            "identity": {"timezone": "America/Denver"},
            "oura": {"client_id": "cid", "client_secret": "shh-42"},
        },
    )
    tokens = OuraTokens(
        access_token="access-token-sensitive",
        refresh_token="refresh-token-sensitive",
        expires_at=1800000000.0,
        token_type="bearer",
    )

    save_oura_tokens(tokens, journal)

    assert load_oura_tokens(journal) == tokens
    config_path = journal / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["oura"]["tokens"] == {
        "access_token": "access-token-sensitive",
        "refresh_token": "refresh-token-sensitive",
        "expires_at": 1800000000.0,
        "token_type": "bearer",
    }
    # Read-modify-write preserves every other key in the file.
    assert config["oura"]["client_id"] == "cid"
    assert config["oura"]["client_secret"] == "shh-42"
    assert config["identity"]["timezone"] == "America/Denver"
    # The config owner writes the file with private permissions.
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    # Nothing token-shaped lands outside the journal config file (the
    # config lock sidecar is the only other artifact, and stays empty).
    others = [p for p in journal.rglob("*") if p.is_file() and p != config_path]
    assert [p.name for p in others] == ["journal.json.lock"]
    assert others[0].read_text(encoding="utf-8") == ""


def test_load_oura_tokens_missing_returns_none(tmp_path: Path):
    assert load_oura_tokens(_make_journal(tmp_path)) is None
    assert (
        load_oura_tokens(_make_journal(tmp_path / "with-section", {"oura": {}})) is None
    )


def test_load_oura_tokens_malformed_fails_loud(tmp_path: Path):
    journal = _make_journal(
        tmp_path, {"oura": {"tokens": {"access_token": "only-this"}}}
    )

    with pytest.raises(OuraAuthError, match="missing required fields") as excinfo:
        load_oura_tokens(journal)

    assert "only-this" not in str(excinfo.value)

    wrong_shape = _make_journal(
        tmp_path / "wrong-shape", {"oura": {"tokens": "not-an-object"}}
    )
    with pytest.raises(OuraAuthError, match="malformed"):
        load_oura_tokens(wrong_shape)


def test_save_oura_tokens_creates_section_when_absent(tmp_path: Path):
    journal = _make_journal(tmp_path, {"identity": {"timezone": "UTC"}})
    tokens = OuraTokens("at", "rt", 4102444800.0)

    save_oura_tokens(tokens, journal)

    loaded = load_oura_tokens(journal)
    assert loaded is not None
    assert loaded.access_token == "at"
    assert loaded.token_type == "Bearer"

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import datetime as dt
import json
import socket
import urllib.error
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.request import Request

import pytest

from solstone.think.journal_config import read_journal_config, write_journal_config
from solstone.think.link import ca as ca_module
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.paths import LinkState, ca_dir
from solstone.think.push import reach

NOW = 1_745_006_400
EXPIRES_AT = "2026-06-20T12:00:00Z"
EXPIRES_EPOCH = int(dt.datetime(2026, 6, 20, 12, tzinfo=dt.UTC).timestamp())


class FakeResponse:
    def __init__(self, body: bytes | dict[str, Any], *, status: int = 200) -> None:
        self.status = status
        self._body = (
            json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        )

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


def _setup_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, link_state: bool = True
) -> LinkState | None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setenv("SERVICES_PORTAL_URL", "https://portal.test")
    monkeypatch.setattr(ca_module.time, "time", lambda: NOW)
    if not link_state:
        return None
    state = LinkState.load_or_create(default_label="test home")
    load_or_generate_ca(ca_dir())
    return state


def _response_payload(
    instance_id: str, *, token: str = "reach-token"
) -> dict[str, Any]:
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_at": EXPIRES_AT,
        "expires_in": 86400,
        "instance_id": instance_id,
    }


def _stored_reach_state() -> object:
    config = read_journal_config()
    return config.get("services", {}).get("push", {}).get("reach_token")


def _write_reach_state(state: dict[str, Any]) -> None:
    write_journal_config({"services": {"push": {"reach_token": state}}})


def _capture_success_urlopen(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> list[Request]:
    requests: list[Request] = []

    def fake_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        requests.append(request)
        return FakeResponse(payload)

    monkeypatch.setattr(reach.urllib_request, "urlopen", fake_urlopen)
    return requests


def _headers(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def _body(request: Request) -> dict[str, Any]:
    assert isinstance(request.data, bytes)
    payload = json.loads(request.data.decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _decode_jwt(value: str) -> tuple[dict[str, Any], dict[str, Any]]:
    header_b64, payload_b64, _sig_b64 = value.split(".")
    header = json.loads(_b64_decode(header_b64))
    payload = json.loads(_b64_decode(payload_b64))
    assert isinstance(header, dict)
    assert isinstance(payload, dict)
    return header, payload


def test_request_reach_token_success_body_and_assertion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    ca = load_or_generate_ca(ca_dir())
    requests = _capture_success_urlopen(
        monkeypatch, _response_payload(link_state.instance_id)
    )

    state = reach._request_reach_token(link_state.instance_id, ca)

    assert state == {
        "token": "reach-token",
        "instance_id": link_state.instance_id,
        "expires_at": EXPIRES_AT,
        "expires_epoch": EXPIRES_EPOCH,
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://portal.test/reach/push/relay-token"
    assert _headers(request)["content-type"] == "application/json"
    body = _body(request)
    assert body["instance_id"] == link_state.instance_id
    assert body["ca_pubkey"] == ca.pubkey_spki_pem
    header, payload = _decode_jwt(body["assertion"])
    assert header == {"alg": "ES256", "typ": "home-reach"}
    assert payload["iss"] == f"home:{link_state.instance_id}"
    assert payload["aud"] == "solstone-reach"
    assert payload["scope"] == "push.relay.enroll"
    assert payload["instance_id"] == link_state.instance_id
    assert payload["iat"] == NOW
    assert payload["exp"] - payload["iat"] == 240
    assert payload["jti"]
    assert "device_fp" not in payload


@pytest.mark.parametrize("status", [400, 503])
def test_request_reach_token_http_errors_return_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    ca = load_or_generate_ca(ca_dir())

    def fake_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        raise urllib.error.HTTPError(request.full_url, status, "nope", {}, None)

    monkeypatch.setattr(reach.urllib_request, "urlopen", fake_urlopen)

    assert reach._request_reach_token(link_state.instance_id, ca) is None


def test_request_reach_token_timeout_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    ca = load_or_generate_ca(ca_dir())

    def fake_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        raise socket.timeout("timed out")

    monkeypatch.setattr(reach.urllib_request, "urlopen", fake_urlopen)

    assert reach._request_reach_token(link_state.instance_id, ca) is None


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        ["not", "a", "dict"],
    ],
)
def test_request_reach_token_malformed_body_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes | list[str]
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    ca = load_or_generate_ca(ca_dir())

    def fake_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        return FakeResponse(
            json.dumps(body).encode("utf-8") if isinstance(body, list) else body
        )

    monkeypatch.setattr(reach.urllib_request, "urlopen", fake_urlopen)

    assert reach._request_reach_token(link_state.instance_id, ca) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: {**payload, "instance_id": "other"},
        lambda payload: {**payload, "token": ""},
        lambda payload: {
            key: value for key, value in payload.items() if key != "token"
        },
        lambda payload: {**payload, "expires_at": ""},
        lambda payload: {**payload, "expires_at": "not-a-date"},
        lambda payload: {**payload, "expires_at": "2026-06-20T12:00:00"},
        lambda payload: {**payload, "expires_at": "2026-06-20T12:00:00-06:00"},
    ],
)
def test_request_reach_token_validation_failures_return_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    ca = load_or_generate_ca(ca_dir())
    _capture_success_urlopen(
        monkeypatch, mutate(_response_payload(link_state.instance_id))
    )

    assert reach._request_reach_token(link_state.instance_id, ca) is None


def test_ensure_reach_token_success_writes_state_and_strips_stale_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    stale_key = "relay" + "_token"
    write_journal_config({"services": {"push": {stale_key: "old"}}})
    _capture_success_urlopen(monkeypatch, _response_payload(link_state.instance_id))

    assert reach.ensure_reach_token() == "reach-token"

    config = read_journal_config()
    push = config["services"]["push"]
    assert stale_key not in push
    assert push["reach_token"] == {
        "token": "reach-token",
        "instance_id": link_state.instance_id,
        "expires_at": EXPIRES_AT,
        "expires_epoch": EXPIRES_EPOCH,
    }


def test_ensure_reach_token_reuses_valid_state_without_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    monkeypatch.setattr(reach.time, "time", lambda: NOW)
    _write_reach_state(
        {
            "token": "stored-token",
            "instance_id": link_state.instance_id,
            "expires_at": EXPIRES_AT,
            "expires_epoch": NOW + 7200,
        }
    )

    def fail_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        raise AssertionError("urlopen should not be called")

    monkeypatch.setattr(reach.urllib_request, "urlopen", fail_urlopen)

    assert reach.ensure_reach_token() == "stored-token"


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"token": "old", "instance_id": "other", "expires_epoch": NOW + 7200},
        {"token": "old", "instance_id": "instance", "expires_epoch": "bad"},
        {"token": "", "instance_id": "instance", "expires_epoch": NOW + 7200},
        {"token": "old", "instance_id": "instance", "expires_epoch": NOW - 1},
        {"token": "old", "instance_id": "instance", "expires_epoch": NOW + 1800},
    ],
)
def test_ensure_reach_token_refresh_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: dict[str, Any] | None
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    monkeypatch.setattr(reach.time, "time", lambda: NOW)
    if state is not None:
        state = {
            **state,
            "instance_id": state["instance_id"].replace(
                "instance", link_state.instance_id
            ),
        }
        _write_reach_state(state)
    requests = _capture_success_urlopen(
        monkeypatch, _response_payload(link_state.instance_id, token="new-token")
    )

    assert reach.ensure_reach_token() == "new-token"
    assert len(requests) == 1


def test_ensure_reach_token_within_margin_failure_returns_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    monkeypatch.setattr(reach.time, "time", lambda: NOW)
    _write_reach_state(
        {
            "token": "stored-token",
            "instance_id": link_state.instance_id,
            "expires_at": EXPIRES_AT,
            "expires_epoch": NOW + 1800,
        }
    )

    def fail_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        raise socket.timeout("timed out")

    monkeypatch.setattr(reach.urllib_request, "urlopen", fail_urlopen)

    assert reach.ensure_reach_token() == "stored-token"


def test_ensure_reach_token_hard_expired_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    monkeypatch.setattr(reach.time, "time", lambda: NOW)
    _write_reach_state(
        {
            "token": "stored-token",
            "instance_id": link_state.instance_id,
            "expires_at": EXPIRES_AT,
            "expires_epoch": NOW - 1,
        }
    )

    def fail_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        raise socket.timeout("timed out")

    monkeypatch.setattr(reach.urllib_request, "urlopen", fail_urlopen)

    assert reach.ensure_reach_token() is None


def test_ensure_reach_token_missing_link_state_returns_none_without_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_journal(tmp_path, monkeypatch, link_state=False)

    def fail_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        raise AssertionError("urlopen should not be called")

    monkeypatch.setattr(reach.urllib_request, "urlopen", fail_urlopen)

    assert reach.ensure_reach_token() is None


def test_read_reach_token_is_present_signal_without_expiry_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _write_reach_state(
        {
            "token": "stored-token",
            "instance_id": "any",
            "expires_at": EXPIRES_AT,
            "expires_epoch": 1,
        }
    )

    assert reach.read_reach_token() == "stored-token"

    _write_reach_state({"token": "", "expires_epoch": EXPIRES_EPOCH})
    assert reach.read_reach_token() is None


def test_reach_token_secrets_not_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    link_state = _setup_journal(tmp_path, monkeypatch)
    assert link_state is not None
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        captured["body"] = _body(request)
        return FakeResponse(
            _response_payload(link_state.instance_id, token="secret-token")
        )

    def fail_write(config: dict[str, Any]) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(reach.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setattr(reach, "write_journal_config", fail_write)

    with caplog.at_level("WARNING"):
        assert reach.ensure_reach_token() == "secret-token"

    body = captured["body"]
    assert "secret-token" not in caplog.text
    assert body["assertion"] not in caplog.text
    assert body["ca_pubkey"] not in caplog.text

    caplog.clear()
    captured.clear()
    ca = load_or_generate_ca(ca_dir())

    def fail_urlopen(request: Request, timeout: float = 0) -> FakeResponse:
        captured["failure_body"] = _body(request)
        raise urllib.error.HTTPError(request.full_url, 503, "nope", {}, None)

    monkeypatch.setattr(reach.urllib_request, "urlopen", fail_urlopen)

    with caplog.at_level("WARNING"):
        assert reach._request_reach_token(link_state.instance_id, ca) is None

    failure_body = captured["failure_body"]
    assert failure_body["assertion"] not in caplog.text
    assert failure_body["ca_pubkey"] not in caplog.text

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import ssl
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from solstone.think.journal_config import write_journal_config
from solstone.think.link.paths import load_service_token
from solstone.think.link.window import read_posture
from solstone.think.services import outcomes, portal_client, spl_handoff, status
from solstone.think.spl import relay_client


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _approved_payload(**extra: Any) -> dict[str, Any]:
    return {
        "service": "spl",
        "state": "approved",
        "approved_at": 1_700_000_000_000,
        **extra,
    }


def _install_urlopen(monkeypatch: pytest.MonkeyPatch, items: list[Any]):
    calls = []
    queue = list(items)

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(portal_client.urllib.request, "urlopen", fake_urlopen)
    return calls


def _install_spl_relay(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[tuple[str, dict[str, Any]]] | None = None,
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    bodies = captured if captured is not None else []

    def post_json(url: str, body: dict[str, Any]) -> dict[str, str]:
        bodies.append((url, body))
        return {"service_token": "tok.spl"}

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://services.solstone.app/handoff/spl",
        code,
        "error",
        hdrs=None,
        fp=io.BytesIO(b""),
    )


def _set_posture(journal_copy: Path, posture: str) -> None:
    config_path = journal_copy / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config.setdefault("link", {})["posture"] = posture
    write_journal_config(config)


def _run(**kwargs) -> outcomes.HandoffOutcome:
    return spl_handoff.enable_spl_via_consent(
        base_url="https://services.test",
        open_browser=lambda _url: True,
        **kwargs,
    )


def test_approved_handoff_enables_spl(journal_copy: Path, monkeypatch) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])
    _install_spl_relay(monkeypatch, captured)

    outcome = _run()

    assert outcome.code == outcomes.APPROVED
    assert read_posture() == "spl"
    assert load_service_token() == "tok.spl"
    assert captured[0][0] == "https://relay.test/enroll/home"


@pytest.mark.parametrize(
    "payload",
    [
        _approved_payload(service_token="x"),
        _approved_payload(instance_id="y"),
        _approved_payload(totp="z"),
        {"service": "spl", "state": "approved"},
        {"service": "scout", "state": "approved", "approved_at": 1},
    ],
)
def test_malformed_approved_payload_does_not_enable(
    journal_copy: Path,
    monkeypatch,
    payload: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        spl_handoff.spl,
        "enable_spl",
        lambda: pytest.fail("enable_spl should not be called"),
    )

    outcome = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=payload,
        )
    )

    assert outcome.code == outcomes.MALFORMED
    assert read_posture() == "direct"
    assert load_service_token() is None


def test_revoked_handoff_returns_revoked_without_enable(
    journal_copy: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        spl_handoff.spl,
        "enable_spl",
        lambda: pytest.fail("enable_spl should not be called"),
    )

    outcome = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={"service": "spl", "state": "revoked"},
        )
    )

    assert outcome.code == outcomes.REVOKED
    assert read_posture() == "direct"


def test_pending_then_approved_enables(journal_copy: Path, monkeypatch) -> None:
    _install_urlopen(
        monkeypatch,
        [
            FakeResponse(200, _body({"service": "spl", "state": "pending"})),
            FakeResponse(204),
            FakeResponse(200, _body(_approved_payload())),
        ],
    )
    _install_spl_relay(monkeypatch)

    outcome = _run()

    assert outcome.code == outcomes.APPROVED
    assert read_posture() == "spl"
    assert load_service_token() == "tok.spl"


def test_continue_until_deadline_returns_expired() -> None:
    ticks = iter([0.0, 0.0, 0.0, 1.0])

    def clock() -> float:
        return next(ticks, 1.0)

    outcome = _run(
        wait_seconds=1,
        clock=clock,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(kind="continue"),
    )

    assert outcome.code == outcomes.EXPIRED


@pytest.mark.parametrize(
    ("item", "code"),
    [
        (_http_error(410), outcomes.EXPIRED),
        (_http_error(400), outcomes.MALFORMED),
        (urllib.error.URLError("down"), outcomes.NETWORK_ERROR),
        (urllib.error.URLError(ssl.SSLError("bad cert")), outcomes.NETWORK_ERROR),
    ],
)
def test_poll_failures_map_to_taxonomy(monkeypatch, item: Any, code: str) -> None:
    _install_urlopen(monkeypatch, [item])

    outcome = _run()

    assert outcome.code == code


def test_relay_unreachable_after_approval_is_network_error(
    journal_copy: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])

    def post_json(_url: str, _body: dict[str, Any]) -> dict[str, str]:
        raise urllib.error.URLError("SECRET_TOKEN https://relay.test")

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    outcome = _run()

    assert outcome.code == outcomes.NETWORK_ERROR
    assert outcome.detail is None
    assert read_posture() != "spl" or load_service_token() is None


def test_relay_bad_response_after_approval_is_local_error(
    journal_copy: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])
    monkeypatch.setattr(relay_client, "_post_json_sync", lambda _url, _body: {})

    outcome = _run()

    assert outcome.code == outcomes.LOCAL_ERROR
    assert outcome.detail is None
    assert read_posture() != "spl" or load_service_token() is None


def test_posture_write_failure_after_token_save_is_not_enabled(
    journal_copy: Path,
    monkeypatch,
) -> None:
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])
    _install_spl_relay(monkeypatch)

    def fail_posture(_value: str) -> None:
        raise OSError("config locked")

    monkeypatch.setattr(spl_handoff.spl, "_write_posture", fail_posture)

    outcome = _run()

    assert outcome.code == outcomes.LOCAL_ERROR
    assert outcome.detail is None
    assert read_posture() == "direct"
    assert load_service_token() == "tok.spl"
    assert status.spl_status()["state"] == "not_enabled"


def test_journal_not_initialized_after_approval_is_local_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    outcome = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=_approved_payload(),
        )
    )

    assert outcome.code == outcomes.LOCAL_ERROR
    assert outcome.detail is None


def test_browser_open_false_still_polls_and_can_succeed(
    journal_copy: Path,
    monkeypatch,
) -> None:
    _install_spl_relay(monkeypatch)

    outcome = spl_handoff.enable_spl_via_consent(
        base_url="https://services.test",
        open_browser=lambda _url: False,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=_approved_payload(),
        ),
    )

    assert outcome.code == outcomes.APPROVED
    assert read_posture() == "spl"

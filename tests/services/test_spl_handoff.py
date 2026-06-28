# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import ssl
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from solstone.think.journal_config import write_journal_config
from solstone.think.link.paths import LinkState, load_service_token, load_totp_secret
from solstone.think.link.window import read_posture
from solstone.think.services import (
    operations,
    outcomes,
    portal_client,
    spl_handoff,
    status,
)
from solstone.think.spl import relay_client

TEST_INSTANCE_ID = "00000000-0000-4000-8000-000000000000"
TEST_NONCE = "TESTNONCE"
TEST_BASE_URL = "https://services.test"
TEST_SUBSCRIBE_URL = "https://services.test/account/subscription"


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


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://services.solstone.app/handoff/spl",
        code,
        "error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def _set_posture(journal_copy: Path, posture: str) -> None:
    config_path = journal_copy / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config.setdefault("link", {})["posture"] = posture
    write_journal_config(config)


def _run(
    *,
    base_url: str = TEST_BASE_URL,
    nonce: str = TEST_NONCE,
    **kwargs,
) -> outcomes.HandoffOutcome:
    return spl_handoff.enable_spl_via_consent(
        base_url=base_url,
        nonce=nonce,
        **kwargs,
    )


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


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


def test_needs_subscription_payload_is_classified() -> None:
    assert (
        spl_handoff._classify_spl_payload(
            {
                "service": "spl",
                "state": outcomes.NEEDS_SUBSCRIPTION,
                "subscribe_url": TEST_SUBSCRIBE_URL,
            }
        )
        == outcomes.NEEDS_SUBSCRIPTION
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"service": "spl", "state": outcomes.NEEDS_SUBSCRIPTION},
        {
            "service": "spl",
            "state": outcomes.NEEDS_SUBSCRIPTION,
            "subscribe_url": 42,
        },
        {
            "service": "spl",
            "state": outcomes.NEEDS_SUBSCRIPTION,
            "subscribe_url": "http://services.test/account/subscription",
        },
        {
            "service": "spl",
            "state": outcomes.NEEDS_SUBSCRIPTION,
            "subscribe_url": TEST_SUBSCRIBE_URL,
            "extra": "x",
        },
    ],
)
def test_needs_subscription_payload_rejects_malformed(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(spl_handoff.MalformedConsent):
        spl_handoff._classify_spl_payload(payload)


def test_needs_subscription_flow_returns_subscribe_url(monkeypatch) -> None:
    monkeypatch.setattr(
        spl_handoff.spl,
        "enable_spl",
        lambda: pytest.fail("enable_spl should not be called"),
    )

    outcome = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={
                "service": "spl",
                "state": outcomes.NEEDS_SUBSCRIPTION,
                "subscribe_url": TEST_SUBSCRIBE_URL,
            },
        )
    )

    assert outcome.code == outcomes.NEEDS_SUBSCRIPTION
    assert outcome.detail == TEST_SUBSCRIBE_URL


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


def test_build_spl_handoff_url_raises_when_link_state_unreadable(monkeypatch) -> None:
    def fail_load_or_create(*, default_label: str = "solstone") -> LinkState:
        _ = default_label
        raise OSError("locked")

    monkeypatch.setattr(
        spl_handoff.LinkState,
        "load_or_create",
        staticmethod(fail_load_or_create),
    )

    with pytest.raises(OSError):
        spl_handoff.build_spl_handoff_url()


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


def test_relay_identity_conflict_after_approval_surfaces_copy_a(
    journal_copy: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])

    def post_json(_url: str, _body_arg: dict[str, Any]):
        raise _http_error(
            409,
            _body({"error": "ca_pubkey already registered to another instance"}),
        )

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    outcome = _run()

    assert outcome.code == outcomes.RELAY_IDENTITY_CONFLICT
    assert outcome.guidance == (
        "this solstone is already set up under a different identity. "
        "reach out to support to reset it, then try again."
    )
    assert outcome.detail is not None
    assert "ca_pubkey already registered to another instance" in outcome.detail
    assert read_posture() == "direct"
    assert load_service_token() is None


@pytest.mark.parametrize(
    ("status", "error_body", "expected_code", "expected_guidance"),
    [
        (
            409,
            "ca_pubkey mismatch — rotation not supported in v1",
            outcomes.RELAY_ROTATION_UNSUPPORTED,
            "this solstone's security key changed and can't be re-registered "
            "automatically yet. reach out to support.",
        ),
        (
            503,
            "not provisioned",
            outcomes.RELAY_UNAVAILABLE,
            "the private network service isn't available right now. "
            "try again in a bit.",
        ),
        (
            413,
            "payload too large",
            outcomes.RELAY_REJECTED,
            "the relay couldn't finish setting up your private network (error 413).",
        ),
    ],
)
def test_relay_http_rejection_maps_to_cause_specific_outcome(
    journal_copy: Path,
    monkeypatch,
    status: int,
    error_body: str,
    expected_code: str,
    expected_guidance: str,
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])

    def post_json(_url: str, _body_arg: dict[str, Any]):
        raise _http_error(status, _body({"error": error_body}))

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    outcome = _run()

    assert outcome.code == expected_code
    assert outcome.guidance == expected_guidance
    assert read_posture() == "direct"
    assert load_service_token() is None


def test_relay_http_rejection_non_json_body_falls_to_generic(
    journal_copy: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    _install_urlopen(monkeypatch, [FakeResponse(200, _body(_approved_payload()))])

    def post_json(_url: str, _body_arg: dict[str, Any]):
        raise _http_error(504, b"<html>504 Gateway Timeout</html>")

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    outcome = _run()

    assert outcome.code == outcomes.RELAY_REJECTED
    assert outcome.guidance == (
        "the relay couldn't finish setting up your private network (error 504)."
    )
    assert read_posture() == "direct"


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


def test_poll_success_enables_spl(
    journal_copy: Path,
    monkeypatch,
) -> None:
    _install_spl_relay(monkeypatch)

    outcome = spl_handoff.enable_spl_via_consent(
        base_url=TEST_BASE_URL,
        nonce=TEST_NONCE,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=_approved_payload(),
        ),
    )

    assert outcome.code == outcomes.APPROVED
    assert read_posture() == "spl"


def test_spl_handoff_never_invokes_global_browser_open(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_module = __import__("webbrowser")
    monkeypatch.setattr(
        browser_module,
        "open",
        lambda *_args, **_kwargs: pytest.fail("browser open should not be called"),
    )
    _install_spl_relay(monkeypatch)
    monkeypatch.setenv("SERVICES_PORTAL_URL", TEST_BASE_URL)

    consent_url, nonce, base_url = spl_handoff.build_spl_handoff_url()
    result = spl_handoff.run_spl_handoff(
        nonce=nonce,
        base_url=base_url,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=_approved_payload(),
        ),
    )

    assert result.phase == "enabled"
    assert consent_url.startswith(f"{TEST_BASE_URL}/enable/spl?nonce=")
    assert read_posture() == "spl"


def test_first_enable_uses_same_persisted_instance_for_portal_and_relay(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = journal_copy / "link" / "state.json"
    assert not state_file.exists()

    enroll_instance_ids: list[str] = []
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")
    monkeypatch.setenv("SERVICES_PORTAL_URL", TEST_BASE_URL)

    def enroll_home(_relay_url: str, **kwargs: Any) -> str:
        enroll_instance_ids.append(kwargs["instance_id"])
        return "tok.spl"

    monkeypatch.setattr(spl_handoff.spl, "enroll_home", enroll_home)

    consent_url, nonce, base_url = spl_handoff.build_spl_handoff_url()
    outcome = spl_handoff.enable_spl_via_consent(
        base_url=base_url,
        nonce=nonce,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=_approved_payload(),
        ),
    )

    assert outcome.code == outcomes.APPROVED
    persisted = LinkState.load()
    assert persisted is not None
    parsed = urllib.parse.urlparse(consent_url)
    consent_instance_id = urllib.parse.parse_qs(parsed.query)["instance"][0]
    assert consent_instance_id == persisted.instance_id == enroll_instance_ids[0]


def test_run_spl_handoff_sets_posture_and_service_token(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        spl_handoff.spl,
        "enroll_home",
        lambda *_args, **_kwargs: "fake-service-token",
    )

    result = spl_handoff.run_spl_handoff(
        nonce=TEST_NONCE,
        base_url=TEST_BASE_URL,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={"service": "spl", "state": outcomes.APPROVED, "approved_at": 1},
        ),
    )

    assert result.phase == "enabled"
    assert read_posture() == "spl"
    assert load_service_token() is not None
    assert load_totp_secret() is not None


def test_run_spl_handoff_local_error_retryable_without_enabled_state(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_enroll(*_args, **_kwargs):
        raise RuntimeError("relay rejected")

    monkeypatch.setattr(spl_handoff.spl, "enroll_home", fail_enroll)

    result = spl_handoff.run_spl_handoff(
        nonce=TEST_NONCE,
        base_url=TEST_BASE_URL,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={"service": "spl", "state": outcomes.APPROVED, "approved_at": 1},
        ),
    )

    assert result.phase == "error"
    assert result.retryable is True
    assert read_posture() == "direct"
    assert load_service_token() is None


def test_run_spl_handoff_maps_needs_subscription_to_operation(
    journal_copy: Path,
) -> None:
    result = spl_handoff.run_spl_handoff(
        nonce=TEST_NONCE,
        base_url=TEST_BASE_URL,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={
                "service": "spl",
                "state": outcomes.NEEDS_SUBSCRIPTION,
                "subscribe_url": TEST_SUBSCRIBE_URL,
            },
        ),
    )

    assert result.phase == "needs_subscription"
    assert result.retryable is False
    assert result.subscribe_url == TEST_SUBSCRIBE_URL

    operations.clear_registry()
    try:
        operations.start_operation(
            "spl",
            "spl_enable",
            "https://services.test/enable/spl?nonce=TESTNONCE",
            lambda: result,
        )
        _wait_until(
            lambda: (
                operations.operation_for_service("spl")["phase"] == "needs_subscription"
            )
        )
        operation = operations.operation_for_service("spl")
        assert operation is not None
        assert operation["subscribe_url"] == TEST_SUBSCRIBE_URL
    finally:
        operations.clear_registry()


def test_run_spl_handoff_maps_terminal_outcomes(journal_copy: Path) -> None:
    revoked = spl_handoff.run_spl_handoff(
        nonce=TEST_NONCE,
        base_url=TEST_BASE_URL,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={"service": "spl", "state": outcomes.REVOKED},
        ),
    )
    expired = spl_handoff.run_spl_handoff(
        nonce=TEST_NONCE,
        base_url=TEST_BASE_URL,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="failed",
            reason="consent_link_expired",
        ),
    )
    malformed = spl_handoff.run_spl_handoff(
        nonce=TEST_NONCE,
        base_url=TEST_BASE_URL,
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={"service": "spl", "state": "bad"},
        ),
    )

    assert revoked.phase == "revoked"
    assert revoked.retryable is False
    assert expired.phase == "error"
    assert expired.retryable is True
    assert malformed.phase == "error"
    assert malformed.retryable is False

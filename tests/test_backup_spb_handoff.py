# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solstone.think.backup.hosted import HostedBinding
from solstone.think.services import outcomes, portal_client, spb_handoff

TEST_BASE_URL = "https://services.test"
TEST_NONCE = "TESTNONCE"
TEST_INSTANCE_ID = "00000000-0000-4000-8000-000000000000"
TEST_SUBSCRIBE_URL = "https://services.test/account/subscription"


def _binding_payload(**extra: Any) -> dict[str, Any]:
    return {
        "broker_endpoint": "https://broker.test",
        "account_id": "acct",
        "instance_id": "inst",
        "bucket": "bucket",
        "prefix": "users/acct/inst/",
        "broker_token": "broker-token",
        **extra,
    }


def _approved_payload(**extra: Any) -> dict[str, Any]:
    return {"status": outcomes.APPROVED, **_binding_payload(), **extra}


def _run(
    *,
    base_url: str = TEST_BASE_URL,
    nonce: str = TEST_NONCE,
    **kwargs,
) -> spb_handoff.SpbHandoffResult:
    return spb_handoff.enable_spb_via_consent(
        base_url=base_url,
        nonce=nonce,
        **kwargs,
    )


def test_approved_payload_returns_binding() -> None:
    result = spb_handoff._classify_spb_payload(_approved_payload())

    assert result == spb_handoff.SpbHandoffResult(
        state=outcomes.APPROVED,
        binding=HostedBinding(
            broker_endpoint="https://broker.test",
            account_id="acct",
            instance_id="inst",
            bucket="bucket",
            prefix="users/acct/inst/",
            broker_token="broker-token",
        ),
        subscribe_url=None,
        reason_code=None,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broker_endpoint", None),
        ("account_id", ""),
        ("instance_id", " "),
        ("bucket", None),
        ("prefix", ""),
        ("broker_token", " "),
    ],
)
def test_approved_payload_requires_non_blank_binding_fields(
    field: str,
    value: object,
) -> None:
    payload = _approved_payload()
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    with pytest.raises(spb_handoff.MalformedConsent):
        spb_handoff._classify_spb_payload(payload)


def test_approved_payload_tolerates_extra_fields_and_does_not_check_service() -> None:
    result = spb_handoff._classify_spb_payload(
        _approved_payload(
            service="not-spb",
            unexpected="ignored",
        )
    )

    assert result.state == outcomes.APPROVED
    assert result.binding is not None
    assert result.binding.broker_token == "broker-token"


def test_needs_subscription_payload_returns_url_and_tolerates_binding_fields() -> None:
    result = spb_handoff._classify_spb_payload(
        {
            "status": outcomes.NEEDS_SUBSCRIPTION,
            "subscribe_url": TEST_SUBSCRIBE_URL,
            **_binding_payload(),
            "unexpected": "ignored",
        }
    )

    assert result == spb_handoff.SpbHandoffResult(
        state=outcomes.NEEDS_SUBSCRIPTION,
        binding=None,
        subscribe_url=TEST_SUBSCRIBE_URL,
        reason_code=None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"status": outcomes.NEEDS_SUBSCRIPTION},
        {"status": outcomes.NEEDS_SUBSCRIPTION, "subscribe_url": "http://bad.test"},
        {"status": outcomes.NEEDS_SUBSCRIPTION, "subscribe_url": ""},
    ],
)
def test_needs_subscription_requires_https_subscribe_url(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(spb_handoff.MalformedConsent):
        spb_handoff._classify_spb_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "pending"},
        {"status": "revoked"},
        {"status": "bogus"},
    ],
)
def test_unrecognized_status_is_malformed(payload: dict[str, Any]) -> None:
    with pytest.raises(spb_handoff.MalformedConsent):
        spb_handoff._classify_spb_payload(payload)


def test_poll_continue_keeps_polling_then_succeeds() -> None:
    queue = [
        portal_client.PollOutcome(kind="continue"),
        portal_client.PollOutcome(kind="success", payload=_approved_payload()),
    ]
    calls: list[dict[str, object]] = []

    def poll_once(_base_url: str, _nonce: str, **kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    result = _run(
        poll_once=poll_once,
        clock=lambda: 0.0,
        wait_seconds=10,
    )

    assert result.state == outcomes.APPROVED
    assert result.binding is not None
    assert len(calls) == 2
    assert calls[0]["component"] == "switchboard"
    assert calls[0]["service"] == "backup"


@pytest.mark.parametrize(
    ("reason", "state"),
    [
        ("consent_link_expired", outcomes.EXPIRED),
        ("nonce_invalid", outcomes.MALFORMED),
        ("portal_unreachable", outcomes.NETWORK_ERROR),
    ],
)
def test_poll_failed_reason_maps_to_result_state(reason: str, state: str) -> None:
    result = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="failed",
            reason=reason,
        ),
        clock=lambda: 0.0,
        wait_seconds=10,
    )

    assert result == spb_handoff.SpbHandoffResult(
        state=state,
        binding=None,
        subscribe_url=None,
        reason_code=state,
    )


def test_success_payload_must_be_dict() -> None:
    result = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload=None,
        ),
        clock=lambda: 0.0,
        wait_seconds=10,
    )

    assert result.state == outcomes.MALFORMED
    assert result.reason_code == outcomes.MALFORMED


def test_malformed_success_payload_returns_malformed() -> None:
    result = _run(
        poll_once=lambda *_args, **_kwargs: portal_client.PollOutcome(
            kind="success",
            payload={"status": outcomes.APPROVED},
        ),
        clock=lambda: 0.0,
        wait_seconds=10,
    )

    assert result.state == outcomes.MALFORMED
    assert result.reason_code == outcomes.MALFORMED


def test_deadline_returns_expired() -> None:
    def fail_poll(*_args, **_kwargs):
        raise AssertionError("poll_once should not be called after deadline")

    result = _run(poll_once=fail_poll, clock=lambda: 0.0, wait_seconds=0)

    assert result == spb_handoff.SpbHandoffResult(
        state=outcomes.EXPIRED,
        binding=None,
        subscribe_url=None,
        reason_code=outcomes.EXPIRED,
    )


def test_build_spb_handoff_url_uses_link_instance_id(monkeypatch) -> None:
    captured: dict[str, str | None] = {}

    monkeypatch.setattr(
        spb_handoff.LinkState,
        "load_or_create",
        staticmethod(
            lambda: SimpleNamespace(
                instance_id=TEST_INSTANCE_ID,
                home_label="solstone",
            )
        ),
    )

    def fake_build_consent_url(
        service: str,
        *,
        instance: str | None = None,
    ) -> tuple[str, str, str]:
        captured["service"] = service
        captured["instance"] = instance
        return (
            "https://services.test/enable/backup?nonce=NONCE",
            "NONCE",
            TEST_BASE_URL,
        )

    monkeypatch.setattr(
        spb_handoff.portal_client,
        "build_consent_url",
        fake_build_consent_url,
    )

    assert spb_handoff.build_spb_handoff_url() == (
        "https://services.test/enable/backup?nonce=NONCE",
        "NONCE",
        TEST_BASE_URL,
    )
    assert captured == {"service": "backup", "instance": TEST_INSTANCE_ID}

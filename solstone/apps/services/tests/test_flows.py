# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Synchronous flow tests for the services app."""

from __future__ import annotations

import json

import pytest

from solstone.apps.services import routes as services_routes
from solstone.think.link.paths import load_service_token, load_totp_secret
from solstone.think.link.window import read_posture
from solstone.think.services import outcomes
from solstone.think.services.portal_client import PollOutcome


def _approved_scout_payload(suffix: str = "one") -> dict[str, str]:
    return {
        "state": "approved",
        "google_api_key": f"google-{suffix}",
        "dispatch_token": f"dispatch-{suffix}",
        "account_id": f"acct-{suffix}",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _read_config(journal):
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def test_run_scout_handoff_maps_approved_to_enabled(services_env, monkeypatch):
    services_env()
    monkeypatch.setattr(
        services_routes.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(services_routes.portal_client, "mint_nonce", lambda: "NONCE")

    result = services_routes.run_scout_handoff(
        refresh=False,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload=_approved_scout_payload(),
        ),
    )

    assert result.phase == "enabled"
    assert result.retryable is False
    assert result.browser_open_succeeded is True
    assert result.portal_url is None
    assert services_routes.service_status.scout_status()["state"] == "enabled"


def test_browser_open_failure_is_surfaced_but_flow_continues_to_poll_terminal(
    services_env,
    monkeypatch,
):
    services_env()
    monkeypatch.setattr(
        services_routes.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(services_routes.portal_client, "mint_nonce", lambda: "NONCE")

    result = services_routes.run_scout_handoff(
        refresh=False,
        open_browser=lambda _url: False,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload=_approved_scout_payload("browser"),
        ),
    )

    assert result.phase == "enabled"
    assert result.browser_open_succeeded is False
    assert result.portal_url == "http://portal.test/enable/scout?nonce=NONCE"
    assert services_routes.service_status.scout_status()["state"] == "enabled"


def test_run_scout_handoff_maps_pending_and_revoked(services_env, monkeypatch):
    services_env()
    monkeypatch.setattr(
        services_routes.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(services_routes.portal_client, "mint_nonce", lambda: "NONCE")

    pending = services_routes.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={
                "state": "pending",
                "account_id": "acct-pending",
                "since": 1770000000000,
            },
        ),
    )
    revoked = services_routes.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={"state": "revoked"},
        ),
    )

    assert pending.phase == "pending"
    assert pending.retryable is False
    assert revoked.phase == "revoked"
    assert revoked.retryable is False


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        ("consent_link_expired", True),
        ("portal_unreachable", True),
        ("unexpected_payload", False),
        ("write_failed", True),
    ],
)
def test_run_scout_handoff_maps_error_outcomes(
    services_env,
    monkeypatch,
    reason,
    retryable,
):
    before_env = services_env()
    before = (before_env.journal / "config" / "journal.json").read_bytes()
    monkeypatch.setattr(
        services_routes.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(services_routes.portal_client, "mint_nonce", lambda: "NONCE")

    result = services_routes.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(kind="failed", reason=reason),
    )

    assert result.phase == "error"
    assert result.retryable is retryable
    assert (before_env.journal / "config" / "journal.json").read_bytes() == before


def test_run_scout_handoff_timeout_retryable_without_state_write(
    services_env, monkeypatch
):
    env = services_env()
    before = (env.journal / "config" / "journal.json").read_bytes()
    now = {"value": 0.0}

    def clock() -> float:
        now["value"] += 0.6
        return now["value"]

    monkeypatch.setattr(
        services_routes.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(services_routes.portal_client, "mint_nonce", lambda: "NONCE")

    result = services_routes.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(kind="continue"),
        clock=clock,
        wait_seconds=1,
    )

    assert result.phase == "error"
    assert result.retryable is True
    assert (env.journal / "config" / "journal.json").read_bytes() == before


def test_spl_enable_contract_sets_posture_and_service_token(
    services_env,
    monkeypatch,
):
    services_env()
    monkeypatch.setattr(
        "solstone.think.services.spl.enroll_home",
        lambda *_args, **_kwargs: "fake-service-token",
    )

    result = services_routes.run_spl_handoff(
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={"service": "spl", "state": outcomes.APPROVED, "approved_at": 1},
        ),
    )

    assert result.phase == "enabled"
    assert read_posture() == "spl"
    assert load_service_token() is not None
    assert load_totp_secret() is not None


def test_spl_local_error_retryable_without_enabled_state(services_env, monkeypatch):
    services_env()

    def fail_enroll(*_args, **_kwargs):
        raise RuntimeError("relay rejected")

    monkeypatch.setattr("solstone.think.services.spl.enroll_home", fail_enroll)

    result = services_routes.run_spl_handoff(
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={"service": "spl", "state": outcomes.APPROVED, "approved_at": 1},
        ),
    )

    assert result.phase == "error"
    assert result.retryable is True
    assert read_posture() == "direct"
    assert load_service_token() is None


def test_run_spl_handoff_maps_terminal_outcomes(services_env):
    services_env()

    revoked = services_routes.run_spl_handoff(
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={"service": "spl", "state": outcomes.REVOKED},
        ),
    )
    expired = services_routes.run_spl_handoff(
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="failed",
            reason="consent_link_expired",
        ),
    )
    malformed = services_routes.run_spl_handoff(
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
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

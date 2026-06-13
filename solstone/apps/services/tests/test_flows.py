# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Synchronous flow tests for the services app."""

from __future__ import annotations

import json

import pytest

from solstone.think.services import scout_handoff
from solstone.think.services import status as service_status
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
        scout_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(scout_handoff.portal_client, "mint_nonce", lambda: "NONCE")

    result = scout_handoff.run_scout_handoff(
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
    assert service_status.scout_status()["state"] == "enabled"


def test_browser_open_failure_is_surfaced_but_flow_continues_to_poll_terminal(
    services_env,
    monkeypatch,
):
    services_env()
    monkeypatch.setattr(
        scout_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(scout_handoff.portal_client, "mint_nonce", lambda: "NONCE")

    result = scout_handoff.run_scout_handoff(
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
    assert service_status.scout_status()["state"] == "enabled"


def test_run_scout_handoff_maps_pending_and_revoked(services_env, monkeypatch):
    services_env()
    monkeypatch.setattr(
        scout_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(scout_handoff.portal_client, "mint_nonce", lambda: "NONCE")

    pending = scout_handoff.run_scout_handoff(
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
    revoked = scout_handoff.run_scout_handoff(
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
        scout_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(scout_handoff.portal_client, "mint_nonce", lambda: "NONCE")

    result = scout_handoff.run_scout_handoff(
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
        scout_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(scout_handoff.portal_client, "mint_nonce", lambda: "NONCE")

    result = scout_handoff.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(kind="continue"),
        clock=clock,
        wait_seconds=1,
    )

    assert result.phase == "error"
    assert result.retryable is True
    assert (env.journal / "config" / "journal.json").read_bytes() == before

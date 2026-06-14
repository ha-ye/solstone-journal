# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.think.services import scout_handoff
from solstone.think.services.portal_client import PollOutcome


def _approved_payload(suffix: str = "one") -> dict[str, str]:
    return {
        "state": "approved",
        "google_api_key": f"google-{suffix}",
        "dispatch_token": f"dispatch-{suffix}",
        "account_id": f"acct-{suffix}",
        "created_at": "2026-05-24T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def _stable_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scout_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(scout_handoff.portal_client, "mint_nonce", lambda: "NONCE")


def _config_bytes(journal: Path) -> bytes:
    return (journal / "config" / "journal.json").read_bytes()


def _config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def test_run_scout_handoff_maps_approved_to_enabled(journal_copy: Path) -> None:
    result = scout_handoff.run_scout_handoff(
        refresh=False,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload=_approved_payload(),
        ),
    )

    assert result.phase == "enabled"
    assert result.retryable is False
    assert result.browser_open_succeeded is True
    assert result.portal_url is None
    saved = _config(journal_copy)
    assert saved["env"]["GOOGLE_API_KEY"] == "google-one"
    assert saved["services"]["scout"]["account_id"] == "acct-one"


def test_run_scout_handoff_maps_pending(journal_copy: Path) -> None:
    result = scout_handoff.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={
                "state": "pending",
                "account_id": "acct-pending",
                "since": 1770000000000,
                "dispatch_token": "dispatch-pending",
            },
        ),
    )

    assert result.phase == "pending"
    assert result.retryable is False
    saved = _config(journal_copy)
    assert saved["services"]["scout"]["state"] == "pending"
    assert saved["services"]["scout"]["dispatch_token"] == "dispatch-pending"
    assert "GOOGLE_API_KEY" not in saved.get("env", {})


def test_run_scout_handoff_maps_revoked(journal_copy: Path) -> None:
    result = scout_handoff.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={"state": "revoked"},
        ),
    )

    assert result.phase == "revoked"
    assert result.retryable is False


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        ("consent_timeout", True),
        ("portal_unreachable", True),
        ("nonce_invalid", False),
    ],
)
def test_run_scout_handoff_error_outcomes_do_not_write_journal(
    journal_copy: Path,
    reason: str,
    retryable: bool,
) -> None:
    before = _config_bytes(journal_copy)

    result = scout_handoff.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(kind="failed", reason=reason),
    )

    assert result.phase == "error"
    assert result.retryable is retryable
    assert _config_bytes(journal_copy) == before


def test_run_scout_handoff_malformed_apply_does_not_write_journal(
    journal_copy: Path,
) -> None:
    before = _config_bytes(journal_copy)

    result = scout_handoff.run_scout_handoff(
        refresh=True,
        open_browser=lambda _url: True,
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={
                "state": "approved",
                "dispatch_token": "dispatch",
                "account_id": "acct",
                "created_at": "2026-05-24T00:00:00Z",
            },
        ),
    )

    assert result.phase == "error"
    assert result.retryable is False
    assert _config_bytes(journal_copy) == before

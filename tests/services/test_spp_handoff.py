# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.think.services import spp_handoff
from solstone.think.services.portal_client import PollOutcome


def _approved_payload(suffix: str = "one") -> dict[str, str]:
    return {
        "endpoint_url": f"https://spp-{suffix}.example.test/v1",
        "served_model_id": f"confidential-model-{suffix}",
        "credential": f"credential-{suffix}",
        "account_id": f"acct-{suffix}",
        "created_at": "2026-05-24T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def _stable_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        spp_handoff.portal_client, "portal_base_url", lambda: "http://portal.test"
    )
    monkeypatch.setattr(spp_handoff.portal_client, "mint_nonce", lambda: "NONCE")


def _config_bytes(journal: Path) -> bytes:
    return (journal / "config" / "journal.json").read_bytes()


def _config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def test_build_confidential_handoff_url_uses_spp_service() -> None:
    consent_url, nonce, base_url = spp_handoff.build_confidential_handoff_url()

    assert consent_url == "http://portal.test/enable/spp?nonce=NONCE"
    assert nonce == "NONCE"
    assert base_url == "http://portal.test"


def test_run_confidential_handoff_maps_success_to_enabled(
    journal_copy: Path,
) -> None:
    result = spp_handoff.run_confidential_handoff(
        refresh=False,
        nonce="NONCE",
        base_url="http://portal.test",
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload=_approved_payload(),
        ),
    )

    assert result.phase == "enabled"
    assert result.retryable is False
    assert "not yet verified" in (result.guidance or "")
    saved = _config(journal_copy)
    assert saved["providers"]["local"] == {
        "endpoint_url": "https://spp-one.example.test",
        "served_model_id": "confidential-model-one",
        "credential": "credential-one",
    }
    assert saved["services"]["confidential"]["account_id"] == "acct-one"


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        ("consent_timeout", True),
        ("portal_unreachable", True),
        ("nonce_invalid", False),
    ],
)
def test_run_confidential_handoff_error_outcomes_do_not_write_journal(
    journal_copy: Path,
    reason: str,
    retryable: bool,
) -> None:
    before = _config_bytes(journal_copy)

    result = spp_handoff.run_confidential_handoff(
        refresh=True,
        nonce="NONCE",
        base_url="http://portal.test",
        poll_once=lambda *_args, **_kwargs: PollOutcome(kind="failed", reason=reason),
    )

    assert result.phase == "error"
    assert result.retryable is retryable
    assert _config_bytes(journal_copy) == before


def test_run_confidential_handoff_malformed_apply_does_not_write_journal(
    journal_copy: Path,
) -> None:
    before = _config_bytes(journal_copy)

    result = spp_handoff.run_confidential_handoff(
        refresh=True,
        nonce="NONCE",
        base_url="http://portal.test",
        poll_once=lambda *_args, **_kwargs: PollOutcome(
            kind="success",
            payload={
                "endpoint_url": "https://spp.example.test",
                "served_model_id": "model",
                "account_id": "acct",
                "created_at": "2026-05-24T00:00:00Z",
            },
        ),
    )

    assert result.phase == "error"
    assert result.retryable is False
    assert _config_bytes(journal_copy) == before

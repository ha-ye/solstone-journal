# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking import scout_lane
from solstone.think.journal_config import write_journal_config
from solstone.think.services import scout


def _approved_payload() -> dict[str, str]:
    return {
        "state": "approved",
        "google_api_key": "google-scout-key",
        "dispatch_token": "dispatch-secret",
        "account_id": "acct-secret",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _read_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def _write_config(payload: dict) -> None:
    write_journal_config(payload)


def _clear_scout(journal: Path) -> None:
    config = _read_config(journal)
    config.setdefault("env", {}).pop("GOOGLE_API_KEY", None)
    services = config.setdefault("services", {})
    services.pop("scout", None)
    _write_config(config)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("enabled", thinking_copy.SCOUT_STATE_ON),
        ("pending", thinking_copy.SCOUT_STATE_REQUESTED),
        ("manual", thinking_copy.SCOUT_STATE_MANUAL_KEY_PRESENT),
        ("absent", thinking_copy.SCOUT_STATE_OFF),
    ],
)
def test_resting_state_maps_storage_states(
    journal_copy: Path,
    setup: str,
    expected: str,
) -> None:
    _clear_scout(journal_copy)

    if setup == "enabled":
        scout.provision_scout_handoff(_approved_payload())
    elif setup == "pending":
        scout.record_scout_pending("acct-pending", 1770000000000)
    elif setup == "manual":
        config = _read_config(journal_copy)
        config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-key"
        _write_config(config)

    assert scout_lane.resting_state() == expected


@pytest.mark.parametrize(
    ("raw_phase", "expected"),
    [
        ("starting", thinking_copy.SCOUT_OP_STARTING),
        ("waiting", thinking_copy.SCOUT_OP_WAITING),
        ("enabled", thinking_copy.SCOUT_STATE_INVITED),
        ("pending", thinking_copy.SCOUT_STATE_REQUESTED),
        ("revoked", thinking_copy.SCOUT_STATE_ENDED),
        ("error", thinking_copy.SCOUT_STATE_REPAIR_NEEDED),
    ],
)
def test_operation_phase_maps_to_product_phase(
    raw_phase: str,
    expected: str,
) -> None:
    payload = scout_lane.remap_operation(
        {
            "kind": "enable",
            "phase": raw_phase,
            "guidance": "next",
            "retryable": False,
            "browser_open_succeeded": True,
            "portal_url": None,
            "elapsed_ms": 12,
        }
    )

    assert payload is not None
    assert payload["phase"] == expected
    assert payload["kind"] == "enable"
    assert payload["elapsed_ms"] == 12


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            thinking_copy.SCOUT_STATE_OFF,
            {"enable": True, "refresh": False, "disable": False, "check": True},
        ),
        (
            thinking_copy.SCOUT_STATE_REQUESTED,
            {"enable": False, "refresh": True, "disable": True, "check": True},
        ),
        (
            thinking_copy.SCOUT_STATE_INVITED,
            {"enable": True, "refresh": False, "disable": False, "check": True},
        ),
        (
            thinking_copy.SCOUT_STATE_ON,
            {"enable": False, "refresh": True, "disable": True, "check": False},
        ),
        (
            thinking_copy.SCOUT_STATE_ENDED,
            {"enable": False, "refresh": False, "disable": False, "check": True},
        ),
        (
            thinking_copy.SCOUT_STATE_MANUAL_KEY_PRESENT,
            {"enable": False, "refresh": False, "disable": False, "check": False},
        ),
        (
            thinking_copy.SCOUT_STATE_REPAIR_NEEDED,
            {"enable": False, "refresh": False, "disable": False, "check": False},
        ),
    ],
)
def test_actions_for_product_states(state: str, expected: dict[str, bool]) -> None:
    assert scout_lane.actions_for_state(state) == expected


def test_provenance_payload_is_secret_free_for_approved_and_pending(
    journal_copy: Path,
) -> None:
    _clear_scout(journal_copy)
    scout.provision_scout_handoff(_approved_payload())

    approved = scout_lane.provenance_payload()
    serialized = json.dumps(approved)
    assert "account_id" not in approved
    assert "dispatch_token" not in approved
    assert "acct-secret" not in serialized
    assert "dispatch-secret" not in serialized
    assert approved["key_created_at"] == "2026-05-24T00:00:00Z"

    _clear_scout(journal_copy)
    scout.record_scout_pending("acct-pending", 1770000000000)

    pending = scout_lane.provenance_payload()
    assert "account_id" not in pending
    assert pending["since"] == 1770000000000
    assert pending["since_label"] == "2026-02-02"


def test_manual_key_takes_precedence_over_pending_block(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    scout.record_scout_pending("acct-pending", 1770000000000)
    config = _read_config(journal_copy)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-key"
    _write_config(config)

    assert scout_lane.resting_state() == thinking_copy.SCOUT_STATE_MANUAL_KEY_PRESENT


@pytest.mark.parametrize(
    ("source_state", "product_state"),
    [
        ("invited", thinking_copy.SCOUT_STATE_INVITED),
        ("ended", thinking_copy.SCOUT_STATE_ENDED),
    ],
)
def test_resting_state_maps_live_check_states(
    monkeypatch: pytest.MonkeyPatch,
    source_state: str,
    product_state: str,
) -> None:
    monkeypatch.setattr(
        scout_lane.scout,
        "update_scout_check",
        lambda **_kwargs: scout.ScoutCheckResult(source_state, True, "checked", None),
    )

    assert scout_lane.resting_state() == product_state


def test_status_payload_uses_single_check_result_and_is_secret_free(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_scout(journal_copy)
    config = _read_config(journal_copy)
    config.setdefault("services", {})["scout"] = {
        "state": "pending",
        "account_id": "acct-secret",
        "since": 1770000000000,
        "dispatch_token": "dispatch-secret",
        "server_status": "approved",
        "checked_at": "2026-06-12T00:00:00+00:00",
    }
    _write_config(config)
    calls: list[bool] = []

    def fake_update(*, force: bool = False) -> scout.ScoutCheckResult:
        calls.append(force)
        return scout.ScoutCheckResult("invited", True, "checked", None)

    monkeypatch.setattr(scout_lane.scout, "update_scout_check", fake_update)

    payload = scout_lane.status_payload(force=True)

    assert calls == [True]
    assert payload["state"] == thinking_copy.SCOUT_STATE_INVITED
    assert payload["checked"] is True
    assert payload["checked_at"] == "checked"
    assert payload["check_error"] is None
    serialized = json.dumps(payload).lower()
    assert "dispatch_token" not in serialized
    assert "dispatch-secret" not in serialized
    assert "server_status" not in serialized
    assert "acct-secret" not in serialized


def test_stale_approved_block_without_key_is_off_with_provenance(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_scout(journal_copy)
    scout.provision_scout_handoff(_approved_payload())
    config = _read_config(journal_copy)
    config.setdefault("env", {}).pop("GOOGLE_API_KEY", None)
    _write_config(config)
    monkeypatch.setattr(
        scout_lane.scout.portal_client,
        "check_scout_status",
        lambda _token: scout.portal_client.ScoutStatusOutcome(
            kind="failed",
            reason="unreachable",
        ),
    )

    assert scout_lane.resting_state() == thinking_copy.SCOUT_STATE_OFF
    assert scout_lane.provenance_payload()["key_created_at"] == "2026-05-24T00:00:00Z"

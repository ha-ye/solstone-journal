# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solstone.think.journal_config import write_journal_config
from solstone.think.services import portal_client, scout


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
    config.setdefault("services", {}).pop("scout", None)
    _write_config(config)


def _set_scout_block(journal: Path, block: dict) -> None:
    config = _read_config(journal)
    config.setdefault("env", {}).pop("GOOGLE_API_KEY", None)
    config.setdefault("services", {})["scout"] = block
    _write_config(config)


def _fresh_checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_checked_at() -> str:
    return (
        datetime.now(timezone.utc)
        - timedelta(seconds=scout.STATUS_CHECK_STALENESS_SECONDS + 30)
    ).isoformat()


def _pending_block(**overrides) -> dict:
    block = {
        "state": "pending",
        "account_id": "acct-pending",
        "since": 1_770_000_000_000,
        "checked_at": _stale_checked_at(),
        "dispatch_token": "dispatch-token",
    }
    block.update(overrides)
    return block


def test_update_scout_check_enabled_short_circuits_without_network(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_scout(journal_copy)
    scout.provision_scout_handoff(_approved_payload())
    monkeypatch.setattr(
        scout.portal_client,
        "check_scout_status",
        lambda _token: (_ for _ in ()).throw(AssertionError("network should not run")),
    )

    result = scout.update_scout_check()

    assert result == scout.ScoutCheckResult("enabled", True, None, None)


def test_update_scout_check_manual_key_short_circuits_without_network(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_scout(journal_copy)
    config = _read_config(journal_copy)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-key"
    _write_config(config)
    monkeypatch.setattr(
        scout.portal_client,
        "check_scout_status",
        lambda _token: (_ for _ in ()).throw(AssertionError("network should not run")),
    )

    result = scout.update_scout_check()

    assert result == scout.ScoutCheckResult("manual_key", True, None, None)


def test_update_scout_check_no_credential_falls_back_to_local_pending(
    journal_copy: Path,
) -> None:
    _clear_scout(journal_copy)
    scout.record_scout_pending("acct-pending", 1_770_000_000_000)

    result = scout.update_scout_check()

    stored = _read_config(journal_copy)["services"]["scout"]["checked_at"]
    assert result == scout.ScoutCheckResult(
        "pending",
        False,
        stored,
        "no_credential",
    )


def test_update_scout_check_uses_fresh_cached_server_status_without_network(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_at = _fresh_checked_at()
    _set_scout_block(
        journal_copy,
        _pending_block(server_status="approved", checked_at=checked_at),
    )
    monkeypatch.setattr(
        scout.portal_client,
        "check_scout_status",
        lambda _token: (_ for _ in ()).throw(AssertionError("network should not run")),
    )

    result = scout.update_scout_check()

    assert result == scout.ScoutCheckResult("invited", True, checked_at, None)


def test_update_scout_check_force_bypasses_fresh_cached_server_status(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_at = _fresh_checked_at()
    _set_scout_block(
        journal_copy,
        _pending_block(server_status="approved", checked_at=checked_at),
    )
    calls: list[str] = []

    def fake_check(token: str) -> portal_client.ScoutStatusOutcome:
        calls.append(token)
        return portal_client.ScoutStatusOutcome(kind="ok", server_status="revoked")

    monkeypatch.setattr(scout.portal_client, "check_scout_status", fake_check)

    result = scout.update_scout_check(force=True)

    assert calls == ["dispatch-token"]
    assert result.source_state == "ended"
    assert result.checked is True
    assert result.check_error is None
    saved = _read_config(journal_copy)["services"]["scout"]
    assert saved["server_status"] == "revoked"
    assert saved["checked_at"] != checked_at
    assert saved["dispatch_token"] == "dispatch-token"


@pytest.mark.parametrize(
    ("server_status", "source_state"),
    [
        ("pending", "pending"),
        ("approved", "invited"),
        ("revoked", "ended"),
    ],
)
def test_update_scout_check_live_success_maps_and_stamps_preserving_token(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_status: str,
    source_state: str,
) -> None:
    checked_at = _stale_checked_at()
    _set_scout_block(journal_copy, _pending_block(checked_at=checked_at))

    monkeypatch.setattr(
        scout.portal_client,
        "check_scout_status",
        lambda token: portal_client.ScoutStatusOutcome(
            kind="ok",
            server_status=server_status,
        ),
    )

    result = scout.update_scout_check()

    assert result.source_state == source_state
    assert result.checked is True
    assert result.checked_at is not None
    assert result.check_error is None
    saved = _read_config(journal_copy)["services"]["scout"]
    assert saved["server_status"] == server_status
    assert saved["checked_at"] == result.checked_at
    assert saved["checked_at"] != checked_at
    assert saved["account_id"] == "acct-pending"
    assert saved["since"] == 1_770_000_000_000
    assert saved["dispatch_token"] == "dispatch-token"


@pytest.mark.parametrize(
    "reason",
    ["unreachable", "tls_failed", "unauthorized", "malformed", "not_found"],
)
def test_update_scout_check_live_failure_falls_back_without_stamp(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    checked_at = _stale_checked_at()
    _set_scout_block(journal_copy, _pending_block(checked_at=checked_at))
    monkeypatch.setattr(
        scout.portal_client,
        "check_scout_status",
        lambda token: portal_client.ScoutStatusOutcome(kind="failed", reason=reason),
    )

    result = scout.update_scout_check()

    assert result == scout.ScoutCheckResult("pending", False, checked_at, reason)
    saved = _read_config(journal_copy)["services"]["scout"]
    assert "server_status" not in saved
    assert saved["checked_at"] == checked_at
    assert saved["dispatch_token"] == "dispatch-token"

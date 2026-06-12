# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.think.journal_config import write_journal_config
from solstone.think.link.paths import (
    save_service_token,
    save_totp_secret,
    service_token_path,
)
from solstone.think.services import scout, status


def _config_path(journal_copy: Path) -> Path:
    return journal_copy / "config" / "journal.json"


def _read_config(journal_copy: Path) -> dict:
    return json.loads(_config_path(journal_copy).read_text("utf-8"))


def _write_config(journal_copy: Path, config: dict) -> None:
    write_journal_config(config)


def _clear_scout(journal_copy: Path) -> None:
    config = _read_config(journal_copy)
    config.setdefault("env", {}).pop("GOOGLE_API_KEY", None)
    config.pop("services", None)
    _write_config(journal_copy, config)


def _set_posture(journal_copy: Path, posture: str) -> None:
    config = _read_config(journal_copy)
    config.setdefault("link", {})["posture"] = posture
    _write_config(journal_copy, config)


def _assert_shape(result: dict[str, str | None]) -> None:
    assert set(result) == {"service", "state", "guidance"}


def test_scout_status_disabled(journal_copy: Path) -> None:
    _clear_scout(journal_copy)

    result = status.scout_status()

    _assert_shape(result)
    assert result == {
        "service": "scout",
        "state": "disabled",
        "guidance": status.SCOUT_DISABLED_GUIDANCE,
    }


def test_scout_status_manual_key(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    config = _read_config(journal_copy)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-secret"
    _write_config(journal_copy, config)

    result = status.scout_status()

    _assert_shape(result)
    assert result == {
        "service": "scout",
        "state": "manual_key",
        "guidance": status.SCOUT_MANUAL_KEY_GUIDANCE,
    }
    assert "manual-secret" not in json.dumps(result)


def test_scout_status_pending_hides_provenance(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    scout.record_scout_pending("acct-secret", 1_700_000_000_000)

    result = status.scout_status()

    _assert_shape(result)
    assert result == {
        "service": "scout",
        "state": "pending",
        "guidance": status.SCOUT_PENDING_GUIDANCE,
    }
    serialized = json.dumps(result)
    assert "acct-secret" not in serialized
    assert "1700000000000" not in serialized


def test_scout_status_enabled_hides_provenance(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    scout.provision_scout_handoff(
        {
            "google_api_key": "google-secret",
            "dispatch_token": "dispatch-secret",
            "account_id": "acct-secret",
            "created_at": "2026-05-24T00:00:00Z",
        }
    )

    result = status.scout_status()

    _assert_shape(result)
    assert result == {"service": "scout", "state": "enabled", "guidance": None}
    serialized = json.dumps(result)
    assert "google-secret" not in serialized
    assert "dispatch-secret" not in serialized
    assert "acct-secret" not in serialized


def test_spl_status_enabled_when_posture_spl_and_token_present(
    journal_copy: Path,
) -> None:
    _set_posture(journal_copy, "spl")
    save_service_token("SECRET_TOKEN")
    save_totp_secret("SECRET_TOTP")

    result = status.spl_status()

    _assert_shape(result)
    assert result == {"service": "spl", "state": "enabled", "guidance": None}
    serialized = json.dumps(result)
    assert "SECRET_TOKEN" not in serialized
    assert "SECRET_TOTP" not in serialized


def test_spl_status_inconsistent_when_spl_without_token(journal_copy: Path) -> None:
    _set_posture(journal_copy, "spl")

    result = status.spl_status()

    _assert_shape(result)
    assert result == {
        "service": "spl",
        "state": "inconsistent",
        "guidance": status.SPL_INCONSISTENT_GUIDANCE,
    }


def test_spl_status_not_enabled_when_direct_with_token(journal_copy: Path) -> None:
    _set_posture(journal_copy, "direct")
    save_service_token("SECRET_TOKEN")

    result = status.spl_status()

    _assert_shape(result)
    assert result == {
        "service": "spl",
        "state": "not_enabled",
        "guidance": status.SPL_NOT_ENABLED_GUIDANCE,
    }
    assert "SECRET_TOKEN" not in json.dumps(result)


def test_spl_status_not_enabled_when_direct_without_token(journal_copy: Path) -> None:
    _set_posture(journal_copy, "direct")

    result = status.spl_status()

    _assert_shape(result)
    assert result == {
        "service": "spl",
        "state": "not_enabled",
        "guidance": status.SPL_NOT_ENABLED_GUIDANCE,
    }


@pytest.mark.parametrize("token_body", ["{", "{}"])
def test_spl_status_bad_token_file_counts_absent(
    journal_copy: Path,
    token_body: str,
) -> None:
    _set_posture(journal_copy, "spl")
    token_path = service_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token_body, encoding="utf-8")

    result = status.spl_status()

    _assert_shape(result)
    assert result == {
        "service": "spl",
        "state": "inconsistent",
        "guidance": status.SPL_INCONSISTENT_GUIDANCE,
    }


def test_status_helpers_do_not_create_link_dirs(tmp_path: Path, monkeypatch) -> None:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    status.scout_status()
    status.spl_status()

    assert not (journal / "link").exists()

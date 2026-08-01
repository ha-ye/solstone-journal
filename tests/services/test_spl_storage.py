# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from solstone.think.link.paths import (
    authorized_clients_path,
    load_service_token,
    save_service_token,
)
from solstone.think.services import spl
from solstone.think.services import spl as relay_client
from tests.helpers.journal_config import seed_journal_config


def _config_path(journal_copy: Path) -> Path:
    return journal_copy / "config" / "journal.json"


def _read_config(journal_copy: Path) -> dict[str, Any]:
    return json.loads(_config_path(journal_copy).read_text("utf-8"))


def _write_posture(journal_copy: Path, posture: str) -> None:
    config = _read_config(journal_copy)
    config.setdefault("link", {})["posture"] = posture
    seed_journal_config(config, journal_copy)


def _install_relay(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[tuple[str, dict[str, Any]]],
) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")

    def post_json(url: str, body: dict[str, Any]) -> dict[str, str]:
        captured.append((url, body))
        return {"service_token": "tok.spl"}

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)


def test_enable_spl_writes_posture_and_service_token(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    _install_relay(monkeypatch, captured)

    spl.enable_spl()

    config = _read_config(journal_copy)
    assert config["link"]["posture"] == "spl"
    assert captured[0][0] == "https://relay.test/enroll/home"
    assert set(captured[0][1]) == {"instance_id", "ca_pubkey", "home_label"}
    assert captured[0][1]["instance_id"]
    assert captured[0][1]["ca_pubkey"]
    assert captured[0][1]["home_label"]
    assert load_service_token() == "tok.spl"


def test_enable_spl_does_not_write_token_to_journal_config(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    _install_relay(monkeypatch, captured)

    spl.enable_spl()

    config_text = json.dumps(_read_config(journal_copy))
    assert "tok.spl" not in config_text


def test_enable_spl_reenrolls_service_identity_only(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    _install_relay(monkeypatch, captured)

    spl.enable_spl()
    spl.enable_spl()

    assert len(captured) == 2
    assert set(captured[0][1]) == {"instance_id", "ca_pubkey", "home_label"}
    assert set(captured[1][1]) == {"instance_id", "ca_pubkey", "home_label"}
    assert load_service_token() == "tok.spl"
    assert spl.is_spl_enabled()


def test_disable_spl_when_enabled_parks_relay_state(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    _install_relay(monkeypatch, captured)
    spl.enable_spl()
    authorized_clients_path().write_text('{"clients": []}\n', encoding="utf-8")
    authorized_text = authorized_clients_path().read_text("utf-8")

    outcome = spl.disable_spl()

    assert outcome == spl.SplDisableOutcome(was_enabled=True)
    assert _read_config(journal_copy)["link"]["posture"] == "direct"
    assert load_service_token() == "tok.spl"
    assert authorized_clients_path().read_text("utf-8") == authorized_text


def test_disable_spl_when_already_direct_returns_was_enabled_false(
    journal_copy: Path,
) -> None:
    _write_posture(journal_copy, "direct")

    outcome = spl.disable_spl()

    assert outcome == spl.SplDisableOutcome(was_enabled=False)


def test_is_spl_enabled_matrix(journal_copy: Path) -> None:
    _write_posture(journal_copy, "direct")
    assert not spl.is_spl_enabled()

    _write_posture(journal_copy, "spl")
    assert not spl.is_spl_enabled()

    _write_posture(journal_copy, "direct")
    save_service_token("tok.spl")
    assert not spl.is_spl_enabled()

    _write_posture(journal_copy, "spl")
    assert spl.is_spl_enabled()


def test_enable_spl_relay_down_leaves_posture_direct(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_posture(journal_copy, "direct")
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")

    def post_json(_url: str, _body: dict[str, Any]) -> dict[str, str]:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    with pytest.raises(spl.RelayUnreachableError):
        spl.enable_spl()

    assert _read_config(journal_copy)["link"]["posture"] == "direct"


def test_enable_spl_relay_http_rejection_raises_relay_rejected(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_posture(journal_copy, "direct")
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")

    def post_json(_url: str, _body: dict[str, Any]) -> dict[str, str]:
        raise urllib.error.HTTPError(
            "https://relay.test/enroll/home",
            409,
            "conflict",
            hdrs=None,
            fp=io.BytesIO(
                json.dumps(
                    {"error": "ca_pubkey already registered to another instance"}
                ).encode()
            ),
        )

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    with pytest.raises(spl.RelayRejectedError) as excinfo:
        spl.enable_spl()

    assert excinfo.value.status == 409
    assert excinfo.value.reason == "ca_pubkey already registered to another instance"
    assert _read_config(journal_copy)["link"]["posture"] == "direct"


def test_enable_spl_relay_http_rejection_non_json_body_has_no_reason(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_posture(journal_copy, "direct")
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://relay.test")

    def post_json(_url: str, _body: dict[str, Any]) -> dict[str, str]:
        raise urllib.error.HTTPError(
            "https://relay.test/enroll/home",
            502,
            "bad gateway",
            hdrs=None,
            fp=io.BytesIO(b"<html>502 Bad Gateway</html>"),
        )

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    with pytest.raises(spl.RelayRejectedError) as excinfo:
        spl.enable_spl()

    assert excinfo.value.status == 502
    assert excinfo.value.reason is None
    assert _read_config(journal_copy)["link"]["posture"] == "direct"


def test_require_journal_config_raises_on_uninitialized_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    with pytest.raises(spl.JournalNotInitializedError):
        spl._require_journal_config()

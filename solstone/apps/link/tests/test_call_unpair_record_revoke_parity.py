# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from importlib import import_module

from typer.testing import CliRunner

from solstone.apps.link import call as link_call
from solstone.apps.observer.utils import (
    load_observer_by_fingerprint,
    mint_pl_observer_record,
    save_observer,
)
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path

journal_sources = import_module("solstone.apps.import.journal_sources")
load_journal_source_by_fingerprint = journal_sources.load_journal_source_by_fingerprint
mint_pl_journal_source_record = journal_sources.mint_pl_journal_source_record
save_journal_source = journal_sources.save_journal_source

PAIRED_AT = "2026-05-20T00:00:00Z"
PHONE_FINGERPRINT = "sha256:" + ("a" * 64)
OBSERVER_FINGERPRINT = "sha256:" + ("b" * 64)
PEER_FINGERPRINT = "sha256:" + ("c" * 64)
UNKNOWN_ROLE_FINGERPRINT = "sha256:" + ("d" * 64)


def _short(fingerprint: str) -> str:
    return fingerprint.removeprefix("sha256:")[:16]


def _authorized() -> AuthorizedClients:
    return AuthorizedClients(authorized_clients_path())


def _add_authorized(
    fingerprint: str, device_label: str, *, role: str = "phone"
) -> None:
    _authorized().add(
        fingerprint,
        device_label,
        "inst-1",
        role=role,
        paired_at=PAIRED_AT,
    )


def _invoke(arg: str):
    return CliRunner().invoke(link_call.app, ["unpair", arg])


def _action_entries(env) -> list[dict]:
    actions_dir = env.journal / "config" / "actions"
    entries = []
    if not actions_dir.exists():
        return entries
    for path in sorted(actions_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            entries.append(json.loads(line))
    return entries


def test_unpair_phone_by_fingerprint_removes_authorized(link_env, monkeypatch) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    _add_authorized(PHONE_FINGERPRINT, "phone")

    result = _invoke(PHONE_FINGERPRINT)

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(PHONE_FINGERPRINT) is False
    assert load_observer_by_fingerprint(PHONE_FINGERPRINT) is None
    assert load_journal_source_by_fingerprint(PHONE_FINGERPRINT) is None


def test_unpair_unknown_role_treats_as_phone_and_warns(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    _add_authorized(UNKNOWN_ROLE_FINGERPRINT, "tablet", role="tablet")
    caplog.set_level(logging.WARNING, logger="solstone.apps.link.call")

    result = _invoke(UNKNOWN_ROLE_FINGERPRINT)

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(UNKNOWN_ROLE_FINGERPRINT) is False
    assert "unexpected role" in caplog.text
    assert "tablet" in caplog.text


def test_unpair_observer_revokes_record_and_does_not_double_remove(
    link_env,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    mint_pl_observer_record(
        fingerprint=OBSERVER_FINGERPRINT,
        device_label="observer",
        paired_at=PAIRED_AT,
    )
    _add_authorized(OBSERVER_FINGERPRINT, "observer", role="observer")
    original_remove = AuthorizedClients.remove
    remove_calls = []

    def spy_remove(self, fingerprint: str) -> bool:
        if fingerprint == OBSERVER_FINGERPRINT:
            remove_calls.append(fingerprint)
        return original_remove(self, fingerprint)

    monkeypatch.setattr(AuthorizedClients, "remove", spy_remove)

    result = _invoke("observer")

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert remove_calls == [OBSERVER_FINGERPRINT]
    assert _authorized().is_authorized(OBSERVER_FINGERPRINT) is False
    observer = load_observer_by_fingerprint(OBSERVER_FINGERPRINT)
    assert observer is not None
    assert observer["revoked"] is True
    assert observer["revoked_at"] is not None


def test_unpair_observer_already_revoked_removes_authorized_and_warns(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    mint_pl_observer_record(
        fingerprint=OBSERVER_FINGERPRINT,
        device_label="observer-revoked",
        paired_at=PAIRED_AT,
    )
    observer = load_observer_by_fingerprint(OBSERVER_FINGERPRINT)
    assert observer is not None
    observer["revoked"] = True
    observer["revoked_at"] = 123
    assert save_observer(observer) is True
    _add_authorized(OBSERVER_FINGERPRINT, "observer-revoked", role="observer")
    caplog.set_level(logging.WARNING, logger="solstone.apps.link.call")

    result = _invoke(OBSERVER_FINGERPRINT)

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(OBSERVER_FINGERPRINT) is False
    observer = load_observer_by_fingerprint(OBSERVER_FINGERPRINT)
    assert observer is not None
    assert observer["revoked"] is True
    assert observer["revoked_at"] == 123
    assert "already revoked" in caplog.text


def test_unpair_observer_missing_record_removes_authorized_and_warns(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    _add_authorized(OBSERVER_FINGERPRINT, "observer-missing", role="observer")
    caplog.set_level(logging.WARNING, logger="solstone.apps.link.call")

    result = _invoke(OBSERVER_FINGERPRINT)

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(OBSERVER_FINGERPRINT) is False
    assert load_observer_by_fingerprint(OBSERVER_FINGERPRINT) is None
    assert "observer record missing" in caplog.text


def test_unpair_observer_save_failure_removes_authorized_and_logs_error(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    mint_pl_observer_record(
        fingerprint=OBSERVER_FINGERPRINT,
        device_label="observer-save-fails",
        paired_at=PAIRED_AT,
    )
    _add_authorized(OBSERVER_FINGERPRINT, "observer-save-fails", role="observer")
    monkeypatch.setattr(
        "solstone.apps.observer.utils.save_observer",
        lambda *_a, **_kw: False,
    )
    caplog.set_level(logging.ERROR, logger="solstone.apps.link.call")

    result = _invoke("observer-save-fails")

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(OBSERVER_FINGERPRINT) is False
    observer = load_observer_by_fingerprint(OBSERVER_FINGERPRINT)
    assert observer is not None
    assert observer.get("revoked") is not True
    assert observer.get("revoked_at") is None
    assert _short(OBSERVER_FINGERPRINT) in caplog.text
    assert "failed to save observer record" in caplog.text


def test_unpair_peer_revokes_source_removes_authorized_and_logs_action(
    link_env,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    env = link_env()
    mint_pl_journal_source_record(
        fingerprint=PEER_FINGERPRINT,
        device_label="peer",
        paired_at=PAIRED_AT,
    )
    _add_authorized(PEER_FINGERPRINT, "peer", role="peer")

    result = _invoke("peer")

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(PEER_FINGERPRINT) is False
    source = load_journal_source_by_fingerprint(PEER_FINGERPRINT)
    assert source is not None
    assert source["revoked"] is True
    assert source["revoked_at"] is not None
    entries = _action_entries(env)
    assert len(entries) == 1
    assert entries[0]["source"] == "app"
    assert entries[0]["actor"] == "import"
    assert entries[0]["action"] == "journal_source_revoke"
    assert entries[0]["params"] == {
        "name": "peer",
        "key_prefix": _short(PEER_FINGERPRINT),
    }


def test_unpair_peer_already_revoked_removes_authorized_and_warns(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    env = link_env()
    mint_pl_journal_source_record(
        fingerprint=PEER_FINGERPRINT,
        device_label="peer-revoked",
        paired_at=PAIRED_AT,
    )
    source = load_journal_source_by_fingerprint(PEER_FINGERPRINT)
    assert source is not None
    source["revoked"] = True
    source["revoked_at"] = 123
    assert save_journal_source(source) is True
    _add_authorized(PEER_FINGERPRINT, "peer-revoked", role="peer")
    caplog.set_level(logging.WARNING, logger="solstone.apps.link.call")

    result = _invoke(PEER_FINGERPRINT)

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(PEER_FINGERPRINT) is False
    source = load_journal_source_by_fingerprint(PEER_FINGERPRINT)
    assert source is not None
    assert source["revoked"] is True
    assert source["revoked_at"] == 123
    assert _action_entries(env) == []
    assert "already revoked" in caplog.text


def test_unpair_peer_missing_source_removes_authorized_and_warns(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    link_env()
    _add_authorized(PEER_FINGERPRINT, "peer-missing", role="peer")
    caplog.set_level(logging.WARNING, logger="solstone.apps.link.call")

    result = _invoke(PEER_FINGERPRINT)

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(PEER_FINGERPRINT) is False
    assert load_journal_source_by_fingerprint(PEER_FINGERPRINT) is None
    assert "peer journal source missing" in caplog.text


def test_unpair_peer_save_failure_removes_authorized_and_logs_error(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_call, "require_solstone", lambda: None)
    env = link_env()
    mint_pl_journal_source_record(
        fingerprint=PEER_FINGERPRINT,
        device_label="peer-save-fails",
        paired_at=PAIRED_AT,
    )
    _add_authorized(PEER_FINGERPRINT, "peer-save-fails", role="peer")
    monkeypatch.setattr(link_call, "save_journal_source", lambda *_a, **_kw: False)
    caplog.set_level(logging.ERROR, logger="solstone.apps.link.call")

    result = _invoke("peer-save-fails")

    assert result.exit_code == 0
    assert "Unpaired." in result.stdout
    assert _authorized().is_authorized(PEER_FINGERPRINT) is False
    source = load_journal_source_by_fingerprint(PEER_FINGERPRINT)
    assert source is not None
    assert source["revoked"] is False
    assert source["revoked_at"] is None
    assert _action_entries(env) == []
    assert _short(PEER_FINGERPRINT) in caplog.text
    assert "failed to save peer journal source" in caplog.text

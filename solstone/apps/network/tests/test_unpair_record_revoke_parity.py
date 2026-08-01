# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from importlib import import_module

import solstone.apps.network.routes as link_routes
import solstone.apps.observer.utils as observer_utils
from solstone.apps.observer.utils import load_observer, save_observer
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path

journal_sources = import_module("solstone.apps.import.journal_sources")
load_journal_source_by_fingerprint = journal_sources.load_journal_source_by_fingerprint
mint_pl_journal_source_record = journal_sources.mint_pl_journal_source_record
save_journal_source = journal_sources.save_journal_source

PAIRED_AT = "2026-05-20T00:00:00Z"
PHONE_FINGERPRINT = "sha256:" + ("a" * 64)
PEER_FINGERPRINT = "sha256:" + ("c" * 64)
UNKNOWN_ROLE_FINGERPRINT = "sha256:" + ("d" * 64)
OTHER_FINGERPRINT = "sha256:" + ("e" * 64)


def _short(fingerprint: str) -> str:
    return fingerprint.removeprefix("sha256:")[:16]


def _authorized() -> AuthorizedClients:
    return AuthorizedClients(authorized_clients_path())


def _add_authorized(fingerprint: str, device_label: str, *, role: str = "") -> None:
    _authorized().add(
        fingerprint,
        device_label,
        "inst-1",
        role=role,
        paired_at=PAIRED_AT,
    )


def _save_bound_observer(handle: str, name: str, fingerprint: str) -> None:
    assert save_observer(
        {
            "key": handle,
            "name": name,
            "created_at": 1,
            "enabled": True,
            "revoked": False,
            "device_binding": {"device": fingerprint, "kind": "cert"},
            "stats": {"segments_received": 0, "bytes_received": 0},
        }
    )


def _save_unbound_observer(handle: str, name: str) -> None:
    assert save_observer(
        {
            "key": handle,
            "name": name,
            "created_at": 1,
            "enabled": True,
            "revoked": False,
            "stats": {"segments_received": 0, "bytes_received": 0},
        }
    )


def _post_unpair(env, payload: dict):
    return env.client.post("/app/network/unpair", json=payload)


def _unpair_payload(
    fingerprint: str, revoked_observers: list[dict] | None = None
) -> dict:
    return {
        "unpaired": fingerprint,
        "revoked_observers": revoked_observers or [],
    }


def _action_entries(env) -> list[dict]:
    actions_dir = env.journal / "config" / "actions"
    entries = []
    if not actions_dir.exists():
        return entries
    for path in sorted(actions_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            entries.append(json.loads(line))
    return entries


def test_unpair_phone_by_fingerprint_removes_authorized(link_env) -> None:
    env = link_env()
    _add_authorized(PHONE_FINGERPRINT, "phone")

    response = _post_unpair(env, {"fingerprint": PHONE_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PHONE_FINGERPRINT)
    assert _authorized().is_authorized(PHONE_FINGERPRINT) is False
    assert load_journal_source_by_fingerprint(PHONE_FINGERPRINT) is None


def test_unpair_phone_revokes_bound_observer_records(link_env) -> None:
    env = link_env()
    _add_authorized(PHONE_FINGERPRINT, "phone")
    _save_bound_observer("phone-a-observer", "phone-a", PHONE_FINGERPRINT)
    _save_bound_observer("phone-b-observer", "phone-b", PHONE_FINGERPRINT)
    _save_bound_observer("other-observer", "other", OTHER_FINGERPRINT)

    response = _post_unpair(env, {"fingerprint": PHONE_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(
        PHONE_FINGERPRINT,
        [
            {"name": "phone-a", "prefix": "phone-a-"},
            {"name": "phone-b", "prefix": "phone-b-"},
        ],
    )
    assert _authorized().is_authorized(PHONE_FINGERPRINT) is False
    assert load_observer("phone-a-observer")["revoked"] is True
    assert load_observer("phone-b-observer")["revoked"] is True
    assert load_observer("other-observer")["revoked"] is False


def test_unpair_phone_leaves_unbound_observer_records_unrevoked(link_env) -> None:
    env = link_env()
    _add_authorized(PHONE_FINGERPRINT, "phone")
    _save_unbound_observer("phone-a-observer", "phone-a")
    _save_unbound_observer("phone-b-observer", "phone-b")

    response = _post_unpair(env, {"fingerprint": PHONE_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PHONE_FINGERPRINT)
    assert _authorized().is_authorized(PHONE_FINGERPRINT) is False
    assert load_observer("phone-a-observer")["revoked"] is False
    assert load_observer("phone-b-observer")["revoked"] is False


def test_unpair_partial_observer_revoke_failure_reports_saved_revocations(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()
    _add_authorized(PHONE_FINGERPRINT, "phone")
    _save_bound_observer("phone-a-observer", "phone-a", PHONE_FINGERPRINT)
    _save_bound_observer("phone-b-observer", "phone-b", PHONE_FINGERPRINT)
    real_save_observer = observer_utils.save_observer

    def fail_second_bound_observer(observer: dict) -> bool:
        if observer.get("name") == "phone-b":
            return False
        return real_save_observer(observer)

    monkeypatch.setattr(observer_utils, "save_observer", fail_second_bound_observer)

    response = _post_unpair(env, {"fingerprint": PHONE_FINGERPRINT})

    assert response.status_code == 500
    body = response.get_json()
    assert body["reason_code"] == "internal_error"
    assert body["detail"] == "Failed to revoke one or more bound observer streams."
    assert body["unpaired"] == PHONE_FINGERPRINT
    assert body["failed_operation"] == "observer_revoke"
    assert body["revoked_observers"] == [{"name": "phone-a", "prefix": "phone-a-"}]
    assert _authorized().is_authorized(PHONE_FINGERPRINT) is False
    assert load_observer("phone-a-observer")["revoked"] is True
    assert load_observer("phone-b-observer")["revoked"] is False


def test_unpair_cascade_runs_after_authorized_removal(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()
    _add_authorized(PHONE_FINGERPRINT, "phone")
    authorized_present_during_cascade: list[bool] = []

    def record_cascade_order(fingerprint: str) -> list[dict]:
        authorized_present_during_cascade.append(
            _authorized().is_authorized(fingerprint)
        )
        return []

    monkeypatch.setattr(
        link_routes,
        "revoke_observers_bound_to_device",
        record_cascade_order,
    )

    response = _post_unpair(env, {"fingerprint": PHONE_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PHONE_FINGERPRINT)
    assert authorized_present_during_cascade == [False]


def test_unpair_unknown_role_removes_authorized_without_warning(
    link_env,
    caplog,
) -> None:
    env = link_env()
    _add_authorized(UNKNOWN_ROLE_FINGERPRINT, "tablet", role="tablet")
    caplog.set_level(logging.WARNING, logger="solstone.apps.network.routes")

    response = _post_unpair(env, {"fingerprint": UNKNOWN_ROLE_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(UNKNOWN_ROLE_FINGERPRINT)
    assert _authorized().is_authorized(UNKNOWN_ROLE_FINGERPRINT) is False
    assert [
        record
        for record in caplog.records
        if record.name == "solstone.apps.network.routes"
        and record.levelno >= logging.WARNING
    ] == []


def test_unpair_peer_revokes_source_removes_authorized_and_logs_action(
    link_env,
) -> None:
    env = link_env()
    mint_pl_journal_source_record(
        fingerprint=PEER_FINGERPRINT,
        device_label="peer",
        paired_at=PAIRED_AT,
    )
    _add_authorized(PEER_FINGERPRINT, "peer", role="peer")

    response = _post_unpair(env, {"device_label": "peer"})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PEER_FINGERPRINT)
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
) -> None:
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
    caplog.set_level(logging.WARNING, logger="solstone.apps.network.routes")

    response = _post_unpair(env, {"fingerprint": PEER_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PEER_FINGERPRINT)
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
) -> None:
    env = link_env()
    _add_authorized(PEER_FINGERPRINT, "peer-missing", role="peer")
    caplog.set_level(logging.WARNING, logger="solstone.apps.network.routes")

    response = _post_unpair(env, {"fingerprint": PEER_FINGERPRINT})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PEER_FINGERPRINT)
    assert _authorized().is_authorized(PEER_FINGERPRINT) is False
    assert load_journal_source_by_fingerprint(PEER_FINGERPRINT) is None
    assert "peer journal source missing" in caplog.text


def test_unpair_peer_save_failure_removes_authorized_and_logs_error(
    link_env,
    caplog,
    monkeypatch,
) -> None:
    env = link_env()
    mint_pl_journal_source_record(
        fingerprint=PEER_FINGERPRINT,
        device_label="peer-save-fails",
        paired_at=PAIRED_AT,
    )
    _add_authorized(PEER_FINGERPRINT, "peer-save-fails", role="peer")
    monkeypatch.setattr(link_routes, "save_journal_source", lambda *_a, **_kw: False)
    caplog.set_level(logging.ERROR, logger="solstone.apps.network.routes")

    response = _post_unpair(env, {"device_label": "peer-save-fails"})

    assert response.status_code == 200
    assert response.get_json() == _unpair_payload(PEER_FINGERPRINT)
    assert _authorized().is_authorized(PEER_FINGERPRINT) is False
    source = load_journal_source_by_fingerprint(PEER_FINGERPRINT)
    assert source is not None
    assert source["revoked"] is False
    assert source["revoked_at"] is None
    assert _action_entries(env) == []
    assert _short(PEER_FINGERPRINT) in caplog.text
    assert "failed to save peer journal source" in caplog.text

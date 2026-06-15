# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest
from flask import Flask, g

from solstone.apps.observer.utils import (
    resolve_observer_identity,
    save_observer,
)
from solstone.convey.secure_listener import ConveyIdentity
from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
from solstone.think.utils import now_ms

DL_KEY = "dlkey123456789"
HEADER_HANDLE = "headerhandle123456789"
FINGERPRINT = "sha256:" + ("c" * 64)
OTHER_FINGERPRINT = "sha256:" + ("d" * 64)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from solstone.convey import state

    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(state, "journal_root", str(journal))
    app = Flask(__name__)
    return app


def _error_payload(error):
    response, status = error
    return response.get_json(), status


def _pl_identity(fingerprint: str) -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-direct",
        fingerprint=fingerprint,
        device_label="observer",
        paired_at="2026-04-20T00:00:00Z",
        session_id=None,
    )


def _save_observer(handle: str, name: str) -> None:
    assert save_observer(
        {
            "key": handle,
            "name": name,
            "created_at": now_ms(),
            "enabled": True,
            "stats": {
                "segments_received": 0,
                "bytes_received": 0,
            },
        }
    )


def test_resolve_dl_success_from_bearer(app_env):
    _save_observer(DL_KEY, "dl")

    with app_env.test_request_context(headers={"Authorization": f"Bearer {DL_KEY}"}):
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "dl"
    assert prefix == DL_KEY[:8]


def test_resolve_dl_uses_bearer_key(app_env):
    header_key = "headerkey123456789"
    _save_observer(header_key, "header")

    with app_env.test_request_context(
        headers={"Authorization": f"Bearer {header_key}"}
    ):
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "header"
    assert observer["key"] == header_key
    assert prefix == header_key[:8]


def test_resolve_dl_missing_auth(app_env):
    with app_env.test_request_context():
        observer, prefix, error = resolve_observer_identity()

    payload, status = _error_payload(error)
    assert observer is None
    assert prefix is None
    assert status == 401
    assert payload["reason_code"] == "auth_required"


def test_resolve_dl_invalid_key(app_env):
    _save_observer(DL_KEY, "dl")

    with app_env.test_request_context(headers={"Authorization": "Bearer wrong"}):
        observer, prefix, error = resolve_observer_identity()

    payload, status = _error_payload(error)
    assert observer is None
    assert prefix is None
    assert status == 401
    assert payload["reason_code"] == "auth_key_invalid"


def test_resolve_dl_revoked(app_env):
    save_observer({"key": DL_KEY, "name": "dl", "revoked": True, "stats": {}})

    with app_env.test_request_context(headers={"Authorization": f"Bearer {DL_KEY}"}):
        _observer, _prefix, error = resolve_observer_identity()

    payload, status = _error_payload(error)
    assert status == 403
    assert payload["reason_code"] == "pl_revoked"


def test_resolve_dl_disabled(app_env):
    save_observer({"key": DL_KEY, "name": "dl", "enabled": False, "stats": {}})

    with app_env.test_request_context(headers={"Authorization": f"Bearer {DL_KEY}"}):
        _observer, _prefix, error = resolve_observer_identity()

    payload, status = _error_payload(error)
    assert status == 403
    assert payload["reason_code"] == "feature_unavailable"


def test_resolve_handle_success_from_header(app_env):
    _save_observer(HEADER_HANDLE, "header")

    with app_env.test_request_context(headers={OBSERVER_HANDLE_HEADER: HEADER_HANDLE}):
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "header"
    assert observer["key"] == HEADER_HANDLE
    assert prefix == HEADER_HANDLE[:8]


def test_resolve_handle_success_from_bearer(app_env):
    bearer_handle = "bearerhandle123456789"
    _save_observer(bearer_handle, "bearer")

    with app_env.test_request_context(
        headers={"Authorization": f"Bearer {bearer_handle}"}
    ):
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "bearer"
    assert observer["key"] == bearer_handle
    assert prefix == bearer_handle[:8]


def test_resolve_header_takes_precedence_over_bearer(app_env):
    _save_observer("headerfirst123456789", "header-first")
    _save_observer("bearersecond123456789", "bearer-second")

    with app_env.test_request_context(
        headers={
            OBSERVER_HANDLE_HEADER: "headerfirst123456789",
            "Authorization": "Bearer bearersecond123456789",
        }
    ):
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "header-first"
    assert prefix == "headerfi"


def test_resolve_pl_phone_without_handle_is_auth_required(app_env):
    with app_env.test_request_context():
        g.identity = _pl_identity(OTHER_FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

    payload, status = _error_payload(error)
    assert observer is None
    assert prefix is None
    assert status == 401
    assert payload["reason_code"] == "auth_required"


def test_resolve_pl_identity_with_header_uses_named_observer(app_env):
    _save_observer(HEADER_HANDLE, "named-observer")

    with app_env.test_request_context(headers={OBSERVER_HANDLE_HEADER: HEADER_HANDLE}):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "named-observer"
    assert observer["key"] == HEADER_HANDLE
    assert prefix == HEADER_HANDLE[:8]


def test_resolve_handle_is_independent_of_pl_fingerprint(app_env):
    _save_observer(HEADER_HANDLE, "stable-observer")

    with app_env.test_request_context(headers={OBSERVER_HANDLE_HEADER: HEADER_HANDLE}):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

        g.identity = _pl_identity(OTHER_FINGERPRINT)
        observer_again, prefix_again, error_again = resolve_observer_identity()

    assert error is None
    assert error_again is None
    assert observer["name"] == "stable-observer"
    assert observer_again["name"] == "stable-observer"
    assert prefix == HEADER_HANDLE[:8]
    assert prefix_again == HEADER_HANDLE[:8]

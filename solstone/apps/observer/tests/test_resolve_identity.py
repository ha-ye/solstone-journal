# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest
from flask import Flask, g

import solstone.convey.root as root_module
from solstone.apps.observer.routes import OBSERVER_CALLOSUM_SSE_ROUTE
from solstone.apps.observer.utils import (
    get_observers_dir,
    load_observer,
    resolve_observer_identity,
    save_observer,
)
from solstone.convey.secure_listener import ConveyIdentity
from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path
from solstone.think.utils import now_ms

DL_KEY = "dlkey123456789"
HEADER_HANDLE = "headerhandle123456789"
FINGERPRINT = "sha256:" + ("c" * 64)
OTHER_FINGERPRINT = "sha256:" + ("d" * 64)


ROUTE_CASES = (
    ("callosum_sse", "GET", OBSERVER_CALLOSUM_SSE_ROUTE),
    ("delete_source", "DELETE", "/app/observer/source/screen"),
    ("ingest_upload", "POST", "/app/observer/ingest"),
    ("ingest_manifest", "GET", "/app/observer/ingest/manifest"),
    ("ingest_manifest_day", "GET", "/app/observer/ingest/manifest/20250103"),
    ("ingest_event", "POST", "/app/observer/ingest/event"),
    ("ingest_health", "POST", "/app/observer/health"),
    ("ingest_segments", "GET", "/app/observer/ingest/segments/20250103"),
)

# These are post-admission outcomes for the request shapes below; they prove
# identity resolution succeeded and the route handler body ran.
ADMITTED_ROUTE_OUTCOMES = {
    "callosum_sse": (200, None),
    "delete_source": (400, "invalid_segment_or_stream"),
    "ingest_upload": (400, "missing_required_field"),
    "ingest_manifest": (200, None),
    "ingest_manifest_day": (200, None),
    "ingest_event": (200, None),
    "ingest_health": (200, None),
    "ingest_segments": (200, None),
}


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


def _save_observer(
    handle: str,
    name: str,
    *,
    device_binding: dict[str, str] | None = None,
) -> None:
    record = {
        "key": handle,
        "name": name,
        "created_at": now_ms(),
        "enabled": True,
        "stats": {
            "segments_received": 0,
            "bytes_received": 0,
        },
    }
    if device_binding is not None:
        record["device_binding"] = device_binding
    assert save_observer(record)


def _save_observer_with_null_device_binding(handle: str, name: str):
    record = {
        "key": handle,
        "name": name,
        "created_at": now_ms(),
        "enabled": True,
        "stats": {
            "segments_received": 0,
            "bytes_received": 0,
        },
        "device_binding": None,
    }
    assert save_observer(record)
    path = get_observers_dir(ensure_exists=False) / f"{handle[:8]}.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "device_binding" in persisted
    assert persisted["device_binding"] is None
    return path


def _authorize_cert(fingerprint: str = FINGERPRINT) -> None:
    AuthorizedClients(authorized_clients_path()).add(
        fingerprint,
        "observer",
        "instance-1",
    )


def _save_cert_observer(
    handle: str,
    name: str,
    *,
    fingerprint: str = FINGERPRINT,
) -> None:
    _authorize_cert(fingerprint)
    _save_observer(
        handle,
        name,
        device_binding={"device": fingerprint, "kind": "cert"},
    )


def _route_response(client, route_case, handle: str, *, identity=None):
    _route_name, method, path = route_case
    kwargs = {"headers": {OBSERVER_HANDLE_HEADER: handle}}
    if identity is not None:
        kwargs["environ_overrides"] = {"pl.identity": identity}
    if method == "GET":
        if path == OBSERVER_CALLOSUM_SSE_ROUTE:
            kwargs["buffered"] = False
        return client.get(path, **kwargs)
    if method == "DELETE":
        return client.delete(path, **kwargs)
    if path == "/app/observer/ingest/event":
        kwargs["json"] = {"tract": "observe", "event": "status"}
    elif path == "/app/observer/health":
        kwargs["json"] = {"status": "ok"}
    else:
        kwargs["data"] = {}
    return client.post(path, **kwargs)


def _assert_route_reason(response, *, status: int, reason_code: str) -> None:
    try:
        assert response.status_code == status
        body = response.get_json()
        assert body["reason_code"] == reason_code
    finally:
        response.close()


def test_resolve_dl_success_from_bearer(app_env):
    _save_cert_observer(DL_KEY, "dl")

    with app_env.test_request_context(headers={"Authorization": f"Bearer {DL_KEY}"}):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "dl"
    assert prefix == DL_KEY[:8]


def test_resolve_dl_uses_bearer_key(app_env):
    header_key = "headerkey123456789"
    _save_cert_observer(header_key, "header")

    with app_env.test_request_context(
        headers={"Authorization": f"Bearer {header_key}"}
    ):
        g.identity = _pl_identity(FINGERPRINT)
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
    _save_cert_observer(HEADER_HANDLE, "header")

    with app_env.test_request_context(headers={OBSERVER_HANDLE_HEADER: HEADER_HANDLE}):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "header"
    assert observer["key"] == HEADER_HANDLE
    assert prefix == HEADER_HANDLE[:8]


def test_resolve_handle_success_from_bearer(app_env):
    bearer_handle = "bearerhandle123456789"
    _save_cert_observer(bearer_handle, "bearer")

    with app_env.test_request_context(
        headers={"Authorization": f"Bearer {bearer_handle}"}
    ):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "bearer"
    assert observer["key"] == bearer_handle
    assert prefix == bearer_handle[:8]


def test_resolve_header_takes_precedence_over_bearer(app_env):
    _save_cert_observer("headerfirst123456789", "header-first")
    _save_observer("bearersecond123456789", "bearer-second")

    with app_env.test_request_context(
        headers={
            OBSERVER_HANDLE_HEADER: "headerfirst123456789",
            "Authorization": "Bearer bearersecond123456789",
        }
    ):
        g.identity = _pl_identity(FINGERPRINT)
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
    _save_cert_observer(HEADER_HANDLE, "named-observer")

    with app_env.test_request_context(headers={OBSERVER_HANDLE_HEADER: HEADER_HANDLE}):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

    assert error is None
    assert observer["name"] == "named-observer"
    assert observer["key"] == HEADER_HANDLE
    assert prefix == HEADER_HANDLE[:8]


def test_resolve_bound_cert_requires_matching_pl_fingerprint(app_env):
    _save_cert_observer(HEADER_HANDLE, "stable-observer")

    with app_env.test_request_context(headers={OBSERVER_HANDLE_HEADER: HEADER_HANDLE}):
        g.identity = _pl_identity(FINGERPRINT)
        observer, prefix, error = resolve_observer_identity()

        g.identity = _pl_identity(OTHER_FINGERPRINT)
        observer_again, prefix_again, error_again = resolve_observer_identity()

    assert error is None
    payload, status = _error_payload(error_again)
    assert status == 403
    assert payload["reason_code"] == "pl_revoked"
    assert observer["name"] == "stable-observer"
    assert observer_again is None
    assert prefix == HEADER_HANDLE[:8]
    assert prefix_again is None


@pytest.mark.parametrize(
    "route_case", ROUTE_CASES, ids=[case[0] for case in ROUTE_CASES]
)
def test_all_device_routes_admit_unbound_record(observer_env, route_case):
    env = observer_env()
    _save_observer(HEADER_HANDLE, "unbound-observer")
    route_name, _method, _path = route_case
    expected_status, expected_reason_code = ADMITTED_ROUTE_OUTCOMES[route_name]

    response = _route_response(env.client, route_case, HEADER_HANDLE)

    try:
        assert response.status_code == expected_status
        if expected_reason_code is not None:
            body = response.get_json()
            assert body["reason_code"] == expected_reason_code
    finally:
        response.close()


@pytest.mark.parametrize(
    "route_case", ROUTE_CASES, ids=[case[0] for case in ROUTE_CASES]
)
def test_all_device_routes_refuse_missing_matching_device_identity(
    observer_env,
    route_case,
    monkeypatch,
):
    env = observer_env()
    _authorize_cert(FINGERPRINT)
    monkeypatch.setattr(
        root_module,
        "get_authorized_clients",
        lambda: AuthorizedClients(authorized_clients_path()),
    )
    _save_observer(
        HEADER_HANDLE,
        "cert-observer",
        device_binding={"device": FINGERPRINT, "kind": "cert"},
    )

    response = _route_response(
        env.client,
        route_case,
        HEADER_HANDLE,
        identity=_pl_identity(OTHER_FINGERPRINT),
    )

    _assert_route_reason(response, status=403, reason_code="pl_revoked")


@pytest.mark.parametrize(
    "route_case", ROUTE_CASES, ids=[case[0] for case in ROUTE_CASES]
)
def test_all_device_routes_refuse_legacy_browser_kind_binding(
    observer_env,
    route_case,
):
    env = observer_env()
    _save_observer(
        HEADER_HANDLE,
        "browser-observer",
        device_binding={"device": FINGERPRINT, "kind": "browser"},
    )

    response = _route_response(env.client, route_case, HEADER_HANDLE)

    _assert_route_reason(response, status=403, reason_code="pl_revoked")


@pytest.mark.parametrize(
    "route_case", ROUTE_CASES, ids=[case[0] for case in ROUTE_CASES]
)
def test_all_device_routes_refuse_present_null_device_binding(
    observer_env,
    route_case,
):
    env = observer_env()
    path = _save_observer_with_null_device_binding(
        HEADER_HANDLE,
        "null-binding-observer",
    )
    before = path.read_bytes()

    response = _route_response(env.client, route_case, HEADER_HANDLE)

    _assert_route_reason(response, status=403, reason_code="pl_revoked")
    assert path.read_bytes() == before
    observer = load_observer(HEADER_HANDLE)
    assert observer is not None
    assert observer["enabled"] is True
    assert observer.get("revoked") is not True
    assert "device_binding" in observer
    assert observer["device_binding"] is None

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from flask import Flask, g, request

from solstone.convey import create_app
from solstone.convey.push import push_bp
from solstone.convey.secure_listener.identity import ConveyIdentity
from solstone.convey.sol_initiated.copy import APNS_CATEGORY_SOL_CHAT_REQUEST
from solstone.think.push.devices import load_devices
from solstone.think.push.runtime import stop_all_push_runtime


def _identity(fingerprint: str = "fp-1") -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-direct",
        fingerprint=fingerprint,
        device_label="iPhone",
        paired_at="2026-05-20T00:00:00Z",
        session_id="s1",
    )


def _register_app() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.before_request
    def stamp_identity() -> None:
        stamped = request.environ.get("pl.identity")
        if stamped is not None:
            g.identity = stamped

    app.register_blueprint(push_bp)
    return app


def _register_body(token: str = "A" * 64) -> dict[str, str]:
    return {
        "device_token": token,
        "bundle_id": "org.solpbc.solstone-swift",
        "environment": "development",
        "platform": "ios",
    }


def _post_register(
    client, *, identity: ConveyIdentity | None = None, token: str = "A" * 64
):
    environ_overrides = {"pl.identity": identity or _identity()}
    return client.post(
        "/api/push/register",
        json=_register_body(token),
        environ_overrides=environ_overrides,
    )


def _device_row() -> dict[str, object]:
    return {
        "fingerprint": "fp-1",
        "token": "a" * 64,
        "bundle_id": "org.solpbc.solstone-swift",
        "environment": "development",
        "platform": "ios",
        "registered_at": 1,
    }


def test_register_push_device_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    client = _register_app().test_client()

    response = _post_register(client, identity=_identity("fp-1"), token="A" * 64)

    assert response.status_code == 200
    assert response.get_json() == {"registered": True, "device_count": 1}
    assert load_devices() == [
        {
            "fingerprint": "fp-1",
            "token": "a" * 64,
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "development",
            "platform": "ios",
            "registered_at": load_devices()[0]["registered_at"],
        }
    ]


def test_register_push_device_replaces_same_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    client = _register_app().test_client()

    first = _post_register(client, identity=_identity("fp-1"), token="A" * 64)
    second = _post_register(client, identity=_identity("fp-1"), token="B" * 64)

    assert first.get_json()["device_count"] == 1
    assert second.get_json() == {"registered": True, "device_count": 1}
    stored = load_devices()
    assert len(stored) == 1
    assert stored[0]["fingerprint"] == "fp-1"
    assert stored[0]["token"] == "b" * 64


def test_delete_push_device_by_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    client = _register_app().test_client()
    identity = _identity("fp-1")
    _post_register(client, identity=identity)

    response = client.delete(
        "/api/push/register",
        environ_overrides={"pl.identity": identity},
    )

    assert response.status_code == 200
    assert response.get_json() == {"removed": True, "device_count": 0}
    assert load_devices() == []


def test_register_refuses_none_fingerprint_on_loopback(journal_copy):
    app = create_app(str(journal_copy))
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        response = client.post("/api/push/register", json=_register_body())
    finally:
        stop_all_push_runtime()

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "push_request_invalid"


def test_register_refuses_missing_paired_identity(journal_copy):
    app = create_app(str(journal_copy))
    app.config["TESTING"] = True
    client = app.test_client()
    try:
        response = client.post(
            "/api/push/register",
            json=_register_body(),
            environ_overrides={"REMOTE_ADDR": "203.0.113.7"},
        )
    finally:
        stop_all_push_runtime()

    assert response.status_code == 400
    data = response.get_json()
    assert data["reason_code"] == "push_request_invalid"
    assert data["detail"] == "push registration requires a paired device"


def test_status_shape(monkeypatch):
    client = _register_app().test_client()
    monkeypatch.setattr(
        "solstone.convey.push.load_devices",
        lambda: [
            {
                "fingerprint": "fp-1",
                "token": "a" * 64,
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "development",
                "platform": "ios",
                "registered_at": 1713528000,
            }
        ],
    )
    monkeypatch.setattr("solstone.convey.push.push_relay_token", lambda: "tok")

    response = client.get("/api/push/status")

    assert response.status_code == 200
    data = response.get_json()
    assert set(data) == {"device_count", "relay_available", "devices"}
    assert data["device_count"] == 1
    assert data["relay_available"] is True
    assert data["devices"] == [
        {
            "token_suffix": "...aaaa",
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "development",
            "platform": "ios",
            "registered_at": "2024-04-19T12:00:00Z",
        }
    ]


def test_status_relay_unavailable(monkeypatch):
    client = _register_app().test_client()
    monkeypatch.setattr("solstone.convey.push.load_devices", lambda: [])
    monkeypatch.setattr("solstone.convey.push.push_relay_token", lambda: "")

    response = client.get("/api/push/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "device_count": 0,
        "relay_available": False,
        "devices": [],
    }


def test_push_test_relays(monkeypatch):
    client = _register_app().test_client()
    calls: list[dict[str, str]] = []
    monkeypatch.setattr("solstone.convey.push.push_relay_token", lambda: "tok")
    monkeypatch.setattr("solstone.convey.push.load_devices", lambda: [_device_row()])
    monkeypatch.setattr(
        "solstone.convey.push.dispatch_via_portal",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )

    response = client.post("/api/push/test", json={"body": "hi"})

    assert response.status_code == 200
    data = response.get_json()
    assert data["dispatched"] is True
    assert data["request_id"].startswith("push-test-")
    assert calls == [
        {
            "request_id": data["request_id"],
            "summary": "hi",
            "category": APNS_CATEGORY_SOL_CHAT_REQUEST,
        }
    ]


def test_push_test_returns_503_when_relay_unavailable(monkeypatch):
    client = _register_app().test_client()
    monkeypatch.setattr("solstone.convey.push.push_relay_token", lambda: "")

    response = client.post("/api/push/test")

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "feature_unavailable"


def test_push_test_returns_503_when_no_devices(monkeypatch):
    client = _register_app().test_client()
    monkeypatch.setattr("solstone.convey.push.push_relay_token", lambda: "tok")
    monkeypatch.setattr("solstone.convey.push.load_devices", lambda: [])

    response = client.post("/api/push/test")

    assert response.status_code == 503
    data = response.get_json()
    assert data["reason_code"] == "feature_unavailable"
    assert data["detail"] == "no devices to reach"


def test_push_test_returns_503_when_relay_dispatch_fails(monkeypatch):
    client = _register_app().test_client()
    monkeypatch.setattr("solstone.convey.push.push_relay_token", lambda: "tok")
    monkeypatch.setattr("solstone.convey.push.load_devices", lambda: [_device_row()])
    monkeypatch.setattr("solstone.convey.push.dispatch_via_portal", lambda **_: None)

    response = client.post("/api/push/test")

    assert response.status_code == 503
    data = response.get_json()
    assert data["reason_code"] == "feature_unavailable"
    assert data["detail"] == "push relay dispatch failed"

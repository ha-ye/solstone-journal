# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
import urllib.parse

import pytest

from solstone.apps.network import routes as link_routes
from solstone.think.journal_config import write_journal_config
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import (
    authorized_clients_path,
    nonces_path,
    save_service_token,
    service_token_path,
)
from solstone.think.services import operations


@pytest.fixture(autouse=True)
def clear_private_link_registry():
    operations.clear_registry()
    yield
    operations.clear_registry()


class _RecordingThreading:
    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []

    def Thread(self, *args, **kwargs) -> threading.Thread:
        thread = threading.Thread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def join_all(self) -> None:
        for thread in self.threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


class _InlineThread:
    def __init__(self, *, target, args=(), kwargs=None, daemon=None):
        del daemon
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


class _InlineThreading:
    Thread = _InlineThread


def _set_posture(env, posture: str) -> None:
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config.setdefault("link", {})["posture"] = posture
    write_journal_config(config)


def _seed_enabled_private_link(env) -> None:
    _set_posture(env, "spl")
    save_service_token("secret-service-token")


def _status(env) -> dict:
    response = env.client.get("/app/network/api/private-link")
    assert response.status_code == 200
    return response.get_json()


def test_private_link_status_default_enabled_and_inconsistent(link_env):
    env = link_env()

    default = _status(env)
    assert default["service"] == "spl"
    assert default["state"] == "not_enabled"
    assert default["posture"] == "direct"
    assert default["enrolled"] is False
    assert default["actions"] == {"enable": True, "disable": False}
    assert default["operation"] is None

    _seed_enabled_private_link(env)
    enabled = _status(env)
    assert enabled["state"] == "enabled"
    assert enabled["posture"] == "spl"
    assert enabled["enrolled"] is True
    assert enabled["actions"] == {"enable": False, "disable": True}

    service_token_path().unlink(missing_ok=True)
    _set_posture(env, "spl")
    inconsistent = _status(env)
    assert inconsistent["state"] == "inconsistent"
    assert inconsistent["posture"] == "spl"
    assert inconsistent["enrolled"] is False
    assert inconsistent["actions"] == {"enable": True, "disable": True}


def test_private_link_enable_busy_returns_service_busy(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()
    started = threading.Event()
    release = threading.Event()

    def slow_flow(**_kwargs):
        started.set()
        release.wait(2)
        return operations.HandoffResult("enabled", None, False)

    recording_threading = _RecordingThreading()
    monkeypatch.setattr(operations, "threading", recording_threading)
    monkeypatch.setattr(link_routes.spl_handoff, "run_spl_handoff", slow_flow)

    try:
        first = env.client.post("/app/network/private-link/enable")
        assert started.wait(timeout=2)
        second = env.client.post("/app/network/private-link/enable")

        assert first.status_code == 202
        assert second.status_code == 503
        assert second.get_json()["reason_code"] == "service_busy"
    finally:
        release.set()
        recording_threading.join_all()


def test_private_link_enable_already_enabled_guard(link_env):
    env = link_env()
    _seed_enabled_private_link(env)

    response = env.client.post("/app/network/private-link/enable")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_operation_for_state"


def test_private_link_enable_prepare_failure_returns_service_operation_failed(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "build_spl_handoff_url",
        lambda: (_ for _ in ()).throw(OSError("locked")),
    )
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: pytest.fail("handoff should not run"),
    )

    response = env.client.post("/app/network/private-link/enable")

    assert response.status_code == 500
    data = response.get_json()
    assert data["reason_code"] == "service_operation_failed"
    assert data["detail"] == "couldn't prepare the consent link"


def test_private_link_enable_success_operation_reaches_enabled(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()

    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult("enabled", None, False),
    )
    monkeypatch.setattr(operations, "threading", _InlineThreading())

    response = env.client.post("/app/network/private-link/enable")
    payload = _status(env)

    assert response.status_code == 202
    assert payload["operation"]["phase"] == "enabled"


def test_private_link_enable_returns_consent_url(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()
    consent_url = "https://services.test/enable/spl?nonce=NONCE&instance=00000000-0000"
    monkeypatch.setattr(
        link_routes.spl_handoff,
        "build_spl_handoff_url",
        lambda: (consent_url, "NONCE", "https://services.test"),
    )

    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult("enabled", None, False),
    )
    monkeypatch.setattr(operations, "threading", _InlineThreading())

    response = env.client.post("/app/network/private-link/enable")
    started = response.get_json()
    payload = _status(env)
    parsed = urllib.parse.urlparse(started["operation"]["portal_url"])

    assert response.status_code == 202
    assert started["operation"]["portal_url"] == consent_url
    assert parsed.path == "/enable/spl"
    assert urllib.parse.parse_qs(parsed.query)["nonce"] == ["NONCE"]
    # enabled is terminal ⇒ CTA suppressed.
    assert payload["operation"]["portal_url"] is None


def test_private_link_disable_success(link_env):
    env = link_env()
    _seed_enabled_private_link(env)

    response = env.client.post("/app/network/private-link/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"]["was_enabled"] is True
    assert data["status"]["state"] == "not_enabled"
    assert data["status"]["posture"] == "direct"


def test_private_link_disable_already_direct(link_env):
    env = link_env()

    response = env.client.post("/app/network/private-link/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"]["was_enabled"] is False
    assert data["status"]["state"] == "not_enabled"


def test_private_link_disable_failure_does_not_report_clean_direct(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()
    _seed_enabled_private_link(env)

    def fail_disable():
        raise RuntimeError("config locked")

    monkeypatch.setattr(link_routes.spl, "disable_spl", fail_disable)

    response = env.client.post("/app/network/private-link/disable")
    followup = _status(env)

    assert response.status_code == 500
    assert response.get_json()["reason_code"] == "service_operation_failed"
    assert followup["state"] == "enabled"
    assert followup["posture"] == "spl"


def test_private_link_direct_spl_direct_without_repairing_devices(link_env):
    env = link_env()
    clients = AuthorizedClients(authorized_clients_path())
    clients.add(
        "sha256:" + "a" * 64,
        "phone",
        "instance-1",
        paired_at="2026-06-01T00:00:00Z",
    )
    authorized_before = authorized_clients_path().read_bytes()
    nonces_before = NonceStore(nonces_path()).snapshot()

    _seed_enabled_private_link(env)
    response = env.client.post("/app/network/private-link/disable")

    assert response.status_code == 200
    assert response.get_json()["status"]["state"] == "not_enabled"
    assert authorized_clients_path().read_bytes() == authorized_before
    assert NonceStore(nonces_path()).snapshot() == nonces_before


def test_private_link_status_secret_free(link_env):
    env = link_env()
    _set_posture(env, "spl")
    save_service_token("secret-service-token")

    response = env.client.get("/app/network/api/private-link")
    serialized = json.dumps(response.get_json())

    assert response.status_code == 200
    assert "secret-service-token" not in serialized

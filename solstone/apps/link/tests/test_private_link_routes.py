# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
import time

import pytest

from solstone.apps.link import routes as link_routes
from solstone.think.journal_config import write_journal_config
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import (
    authorized_clients_path,
    nonces_path,
    save_service_token,
    save_totp_secret,
    service_token_path,
)
from solstone.think.services import operations


@pytest.fixture(autouse=True)
def clear_private_link_registry():
    operations.clear_registry()
    yield
    operations.clear_registry()


def _set_posture(env, posture: str) -> None:
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config.setdefault("link", {})["posture"] = posture
    write_journal_config(config)


def _seed_enabled_private_link(env) -> None:
    _set_posture(env, "spl")
    save_service_token("secret-service-token")


def _status(env) -> dict:
    response = env.client.get("/app/link/api/private-link")
    assert response.status_code == 200
    return response.get_json()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def _wait_for_phase(env, phase: str) -> dict:
    payload: dict = {}

    def reached() -> bool:
        nonlocal payload
        payload = _status(env)
        operation = payload.get("operation")
        return isinstance(operation, dict) and operation.get("phase") == phase

    _wait_until(reached)
    return payload


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
        return operations.HandoffResult("enabled", None, False, True, None)

    monkeypatch.setattr(link_routes.spl_handoff, "run_spl_handoff", slow_flow)

    first = env.client.post("/app/link/private-link/enable")
    _wait_until(started.is_set)
    second = env.client.post("/app/link/private-link/enable")
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "service_busy"


def test_private_link_enable_already_enabled_guard(link_env):
    env = link_env()
    _seed_enabled_private_link(env)

    response = env.client.post("/app/link/private-link/enable")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_operation_for_state"


def test_private_link_enable_success_operation_reaches_enabled(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()

    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult("enabled", None, False, True, None),
    )

    response = env.client.post("/app/link/private-link/enable")
    payload = _wait_for_phase(env, "enabled")

    assert response.status_code == 202
    assert payload["operation"]["phase"] == "enabled"


def test_private_link_enable_browser_open_failure_is_surfaced(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
):
    env = link_env()

    monkeypatch.setattr(
        link_routes.spl_handoff,
        "run_spl_handoff",
        lambda **_kwargs: operations.HandoffResult(
            "enabled",
            None,
            False,
            False,
            "http://portal/x",
        ),
    )

    response = env.client.post("/app/link/private-link/enable")
    payload = _wait_for_phase(env, "enabled")

    assert response.status_code == 202
    assert payload["operation"]["browser_open_succeeded"] is False
    assert payload["operation"]["portal_url"] == "http://portal/x"


def test_private_link_disable_success(link_env):
    env = link_env()
    _seed_enabled_private_link(env)

    response = env.client.post("/app/link/private-link/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"]["was_enabled"] is True
    assert data["status"]["state"] == "not_enabled"
    assert data["status"]["posture"] == "direct"


def test_private_link_disable_already_direct(link_env):
    env = link_env()

    response = env.client.post("/app/link/private-link/disable")

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

    response = env.client.post("/app/link/private-link/disable")
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
    response = env.client.post("/app/link/private-link/disable")

    assert response.status_code == 200
    assert response.get_json()["status"]["state"] == "not_enabled"
    assert authorized_clients_path().read_bytes() == authorized_before
    assert NonceStore(nonces_path()).snapshot() == nonces_before


def test_private_link_status_secret_free(link_env):
    env = link_env()
    _set_posture(env, "spl")
    save_service_token("secret-service-token")
    save_totp_secret("secret-totp-value")

    response = env.client.get("/app/link/api/private-link")
    serialized = json.dumps(response.get_json())

    assert response.status_code == 200
    assert "secret-service-token" not in serialized
    assert "secret-totp-value" not in serialized
    assert "totp" not in serialized.lower()

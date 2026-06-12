# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Flow tests for the backup app."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from solstone.apps.backup import routes as backup_routes
from solstone.think.backup.destination import DestinationStatus


def _config_path(env) -> Path:
    return env.journal / "config" / "journal.json"


def _write_config(env, payload: dict) -> None:
    _config_path(env).write_text(json.dumps(payload), encoding="utf-8")


def _read_config(env) -> dict:
    return json.loads(_config_path(env).read_text(encoding="utf-8"))


def _configured_backup(*, confirmed: bool) -> dict:
    return {
        "backup": {
            "enabled": False,
            "destination": {
                "repository": "s3:safe-bucket/path",
                "backend": "s3",
                "credentials": {
                    "access_key_id": "access-secret",
                    "secret_access_key": "secret-secret",
                },
            },
            "daily_key": "daily-secret",
            "recovery_key": "A" * 64,
            "confirmed_recovery_key": confirmed,
        }
    }


def test_enable_blocked_until_recovery_key_confirmed(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    _write_config(env, _configured_backup(confirmed=False))
    init_repository = Mock()
    request_backup_now = Mock(return_value=True)
    monkeypatch.setattr(backup_routes, "init_repository", init_repository)
    monkeypatch.setattr(backup_routes, "request_backup_now", request_backup_now)

    response = env.client.post("/app/backup/enable")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "backup_not_confirmed"
    init_repository.assert_not_called()
    request_backup_now.assert_not_called()
    assert _read_config(env)["backup"]["enabled"] is False


def test_enable_after_confirmation_sets_up_repository_and_queues_backup(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, _configured_backup(confirmed=True))
    init_repository = Mock()
    request_backup_now = Mock(return_value=True)
    monkeypatch.setattr(
        backup_routes, "ensure_restic", Mock(return_value=Path("/restic"))
    )
    monkeypatch.setattr(backup_routes, "init_repository", init_repository)
    monkeypatch.setattr(backup_routes, "request_backup_now", request_backup_now)

    response = env.client.post("/app/backup/enable")
    wait_until_helper(lambda: init_repository.called)

    assert response.status_code == 202
    assert _read_config(env)["backup"]["enabled"] is True
    init_repository.assert_called_once()
    request_backup_now.assert_called_once_with()


def test_enable_busy_does_not_set_enabled(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, _configured_backup(confirmed=True))
    started = threading.Event()
    release = threading.Event()

    def slow_rotate():
        started.set()
        release.wait(2)
        return SimpleNamespace(status="ok", reason_code=None)

    monkeypatch.setattr(backup_routes, "rotate_recovery_key", slow_rotate)

    rotate_response = env.client.post("/app/backup/recovery-key/rotate")
    wait_until_helper(started.is_set)
    enable_response = env.client.post("/app/backup/enable")
    release.set()

    assert rotate_response.status_code == 202
    assert enable_response.status_code == 503
    assert enable_response.get_json()["reason_code"] == "backup_busy"
    assert _read_config(env)["backup"]["enabled"] is False


def test_confirm_mismatch_does_not_write_state(backup_env) -> None:
    env = backup_env()
    _write_config(env, _configured_backup(confirmed=False))

    response = env.client.post("/app/backup/confirm", json={"recovery_key": "B" * 64})

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "recovery_key_mismatch"
    assert _read_config(env)["backup"]["confirmed_recovery_key"] is False


def test_reveal_returns_display_without_losing_key_progress(backup_env) -> None:
    env = backup_env()
    _write_config(env, _configured_backup(confirmed=False))
    before = _read_config(env)

    response = env.client.post("/app/backup/recovery-key/reveal")

    assert response.status_code == 200
    data = response.get_json()
    assert data["recovery_key_display"] == " ".join(["AAAA"] * 16)
    assert _read_config(env) == before


def test_status_management_errors_and_initial_seed_scrub_secrets(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    _write_config(env, _configured_backup(confirmed=True))
    monkeypatch.setattr(backup_routes, "request_backup_now", Mock(return_value=False))
    monkeypatch.setattr(
        backup_routes, "ensure_restic", Mock(return_value=Path("/restic"))
    )
    monkeypatch.setattr(
        backup_routes,
        "validate_destination",
        Mock(
            return_value=DestinationStatus(
                reachable=True,
                repo_exists=True,
                reason_code="repo_exists",
                message="backup repository is reachable",
            )
        ),
    )

    status = env.client.get("/app/backup/status").get_json()
    backup_now = env.client.post("/app/backup/backup-now").get_json()
    destination = env.client.post(
        "/app/backup/destination",
        json={
            "repository": "s3:other-bucket/path",
            "backend": "s3",
            "credentials": {
                "access_key_id": "submitted-access",
                "secret_access_key": "submitted-secret",
            },
        },
    ).get_json()
    html = env.client.get("/app/backup/").get_data(as_text=True)
    match = re.search(r"const BACKUP_INITIAL = (\{.*\});", html)
    assert match, "BACKUP_INITIAL assignment not found"
    initial = json.loads(match.group(1))

    browser_payloads = [status, backup_now, destination, initial]
    serialized = json.dumps(browser_payloads)
    for secret in (
        "daily-secret",
        "A" * 64,
        "access-secret",
        "secret-secret",
        "submitted-access",
        "submitted-secret",
    ):
        assert secret not in serialized

    reveal = env.client.post("/app/backup/recovery-key/reveal").get_json()
    assert "daily-secret" not in json.dumps(reveal)

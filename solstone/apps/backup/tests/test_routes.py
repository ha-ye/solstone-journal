# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Route tests for the backup app."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from solstone.apps.backup import routes as backup_routes
from solstone.apps.backup.copy import backup_copy_values
from solstone.convey.config import DEFAULT_APP_ORDER
from solstone.think.backup.destination import DestinationStatus


def test_backup_app_discovered_and_auto_appended_for_saved_order(backup_env) -> None:
    env = backup_env()
    convey_path = env.journal / "config" / "convey.json"
    convey_path.write_text(
        json.dumps(
            {
                "apps": {
                    "order": [
                        "home",
                        "activities",
                        "entities",
                        "search",
                        "reflections",
                        "news",
                    ],
                    "starred": ["home"],
                }
            }
        ),
        encoding="utf-8",
    )

    response = env.client.get("/app/backup/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-app-name="backup"' in html
    assert "backup" not in DEFAULT_APP_ORDER


def test_destination_route_sets_destination_and_returns_sanitized_probe(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    ensure_restic = Mock(return_value=Path("/restic"))
    validate_destination = Mock(
        return_value=DestinationStatus(
            reachable=True,
            repo_exists=False,
            reason_code="repo_missing",
            message="backup destination is reachable and needs setup",
        )
    )
    monkeypatch.setattr(backup_routes, "ensure_restic", ensure_restic)
    monkeypatch.setattr(backup_routes, "validate_destination", validate_destination)
    monkeypatch.setattr(backup_routes, "generate_daily_key", lambda: "probe-secret")

    response = env.client.post(
        "/app/backup/destination",
        json={
            "repository": "s3:safe-bucket/path",
            "backend": "s3",
            "credentials": {
                "access_key_id": "access-secret",
                "secret_access_key": "secret-secret",
            },
        },
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["destination_status"]["reason_code"] == "repo_missing"
    serialized = json.dumps(data)
    for secret in ("access-secret", "secret-secret", "probe-secret"):
        assert secret not in serialized
    ensure_restic.assert_called_once_with()
    validate_destination.assert_called_once()


def test_backup_now_unavailable_returns_reason(backup_env, monkeypatch) -> None:
    env = backup_env()
    monkeypatch.setattr(backup_routes, "request_backup_now", Mock(return_value=False))

    response = env.client.post("/app/backup/backup-now")

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "backup_unavailable"


def test_retention_validation_errors_return_invalid_config_value(backup_env) -> None:
    env = backup_env()

    response = env.client.post("/app/backup/retention", json={"hourly": 1})

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_config_value"


def test_rotate_restore_and_teardown_routes_call_engine_hooks(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    rotate_recovery_key = Mock(
        return_value=SimpleNamespace(status="ok", reason_code=None)
    )
    teardown_backup = Mock(return_value=SimpleNamespace(status="ok", reason_code=None))
    restore_journal = Mock(return_value=SimpleNamespace(status="ok", reason_code=None))
    monkeypatch.setattr(backup_routes, "rotate_recovery_key", rotate_recovery_key)
    monkeypatch.setattr(backup_routes, "teardown_backup", teardown_backup)
    monkeypatch.setattr(backup_routes, "restore_journal", restore_journal)

    rotate_response = env.client.post("/app/backup/recovery-key/rotate")
    wait_until_helper(lambda: rotate_recovery_key.called)
    backup_routes._clear_registry()

    teardown_response = env.client.post("/app/backup/teardown")
    wait_until_helper(lambda: teardown_backup.called)
    backup_routes._clear_registry()

    restore_response = env.client.post(
        "/app/backup/restore",
        json={
            "repository": "b2:bucket:path",
            "backend": "b2",
            "credentials": {
                "account_id": "key-id",
                "account_key": "application-key",
            },
            "recovery_key": "A" * 64,
        },
    )
    wait_until_helper(lambda: restore_journal.called)

    assert rotate_response.status_code == 202
    assert teardown_response.status_code == 202
    assert restore_response.status_code == 202
    restore_destination = restore_journal.call_args.args[0]
    assert restore_destination.repository == "b2:bucket:path"
    assert restore_destination.backend == "b2"
    assert restore_journal.call_args.args[1] == "A" * 64


def test_single_slot_concurrent_operation_returns_backup_busy(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    started = threading.Event()
    release = threading.Event()

    def slow_rotate():
        started.set()
        release.wait(2)
        return SimpleNamespace(status="ok", reason_code=None)

    monkeypatch.setattr(backup_routes, "rotate_recovery_key", slow_rotate)
    monkeypatch.setattr(
        backup_routes,
        "teardown_backup",
        Mock(return_value=SimpleNamespace(status="ok", reason_code=None)),
    )

    first = env.client.post("/app/backup/recovery-key/rotate")
    wait_until_helper(started.is_set)
    second = env.client.post("/app/backup/teardown")
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "backup_busy"


def test_forbidden_terms_absent_from_backup_surfaces(backup_env, monkeypatch) -> None:
    env = backup_env()
    monkeypatch.setattr(backup_routes, "request_backup_now", Mock(return_value=False))
    html = env.client.get("/app/backup/").get_data(as_text=True)
    match = re.search(
        r'(<section class="backup-shell".*?</section>\s*<script>.*?window\.BACKUP_INITIAL = BACKUP_INITIAL;\s*</script>)',
        html,
        re.DOTALL,
    )
    assert match, "backup render surface not found"
    backup_html = match.group(1)
    js = Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")
    routes_source = Path("solstone/apps/backup/routes.py").read_text(encoding="utf-8")
    payloads = [
        env.client.post("/app/backup/backup-now").get_json(),
        env.client.post("/app/backup/enable").get_json(),
        env.client.post(
            "/app/backup/destination",
            json={"repository": "repo", "backend": "bad"},
        ).get_json(),
    ]
    haystack = "\n".join(
        [
            backup_html,
            js,
            routes_source,
            "\n".join(backup_copy_values()),
            json.dumps(payloads, sort_keys=True),
        ]
    ).lower()
    forbidden = [
        "activate",
        "subscribe",
        "sign up for",
        "upgrade",
        "log in",
        "sign in",
        "account",
        "capture",
        "watch",
        "record",
        "monitor",
        "track",
        "collect",
    ]

    hits = [
        term for term in forbidden if re.search(rf"\b{re.escape(term)}\b", haystack)
    ]

    assert "recorded" in haystack
    assert hits == []

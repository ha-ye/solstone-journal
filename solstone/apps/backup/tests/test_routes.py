# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Route tests for the backup app."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solstone.apps.backup import routes as backup_routes
from solstone.convey.config import DEFAULT_APP_ORDER
from solstone.think.backup.destination import DestinationStatus
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
)
from solstone.think.backup.restore import RestoreResult
from solstone.think.offload_ledger import OffloadFile, append_offload_event
from solstone.think.offload_measurement import (
    RawMediaUsage,
    SuggestedOffloadDefaults,
)
from solstone.think.offload_restore import OffloadStatusMeasurement
from solstone.think.utils import DEFAULT_STREAM

CONSENT_URL = "https://services.test/enable/backup?nonce=NONCE"
SUBSCRIBE_URL = "https://services.test/plan"


def _config_path(env) -> Path:
    return env.journal / "config" / "journal.json"


def _write_config(env, payload: dict) -> None:
    payload.setdefault("setup", {"completed_at": 1700000000000})
    _config_path(env).write_text(json.dumps(payload), encoding="utf-8")


def _configured_keys(*, confirmed: bool = True) -> dict:
    return {
        "backup": {
            "confirmed_recovery_key": confirmed,
            "daily_key": "daily-secret",
            "recovery_key": "A" * 64,
        }
    }


def _binding() -> HostedBinding:
    return HostedBinding(
        broker_endpoint="https://broker.test",
        account_id="acct",
        instance_id="inst",
        bucket="bkt",
        prefix="users/acct/inst",
        broker_token="BTOKEN",
    )


def _creds() -> HostedCredentials:
    return HostedCredentials(
        access_key_id="AKID",
        secret_access_key="SAK",
        session_token="SESS",
        endpoint="https://r2.example",
        expires_at="2026-07-13T12:00:00Z",
    )


def _handoff(
    state: str,
    *,
    binding: HostedBinding | None = None,
    subscribe_url: str | None = None,
    reason_code: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        binding=binding,
        subscribe_url=subscribe_url,
        reason_code=reason_code,
    )


def _mock_handoff_url(monkeypatch) -> Mock:
    build = Mock(return_value=(CONSENT_URL, "NONCE", "https://services.test"))
    monkeypatch.setattr(backup_routes, "build_spb_handoff_url", build)
    return build


def _operation(env) -> dict | None:
    return env.client.get("/app/backup/status").get_json()["operation"]


def _wait_for_phase(env, wait_until_helper, phase: str) -> dict:
    wait_until_helper(
        lambda: (
            (operation := _operation(env)) is not None and operation["phase"] == phase
        )
    )
    operation = _operation(env)
    assert operation is not None
    return operation


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

    response = env.client.get("/api/shell")

    assert response.status_code == 200
    apps = response.get_json()["apps"]
    names = [app["name"] for app in apps]
    backup = next(app for app in apps if app["name"] == "backup")
    assert "backup" in names
    assert backup["workspace_url"] == "/app/backup/workspace"
    assert "backup" not in DEFAULT_APP_ORDER


def test_backup_spa_shell_workspace_and_route_resolution(backup_env) -> None:
    env = backup_env()
    workspace_path = Path("solstone/apps/backup/workspace.html")
    routes_source = Path("solstone/apps/backup/routes.py").read_text(encoding="utf-8")
    workspace = workspace_path.read_text(encoding="utf-8")
    js = Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")

    index_response = env.client.get("/app/backup/")
    workspace_response = env.client.get("/app/backup/workspace")

    assert index_response.status_code == 200
    assert b'data-solstone-shell="spa"' in index_response.data
    assert workspace_response.status_code == 200
    assert workspace_response.data == workspace_path.read_bytes()
    assert "render_template" not in routes_source
    assert "window.BACKUP_COPY" not in workspace
    assert "window.BACKUP_INITIAL" not in workspace
    assert 'href="/app/backup/static/backup.css"' in workspace
    assert '<script defer src="/app/backup/static/backup.js"></script>' in workspace
    assert "await refreshStatus();" in js
    assert "window.BACKUP_INITIAL" not in js

    adapter = env.app.url_map.bind("localhost")
    for path, method in (
        ("/app/backup/static/backup.css", "GET"),
        ("/app/backup/static/backup.js", "GET"),
        ("/app/backup/status", "GET"),
        ("/app/backup/keys/generate", "POST"),
        ("/app/backup/recovery-key/reveal", "POST"),
        ("/app/backup/confirm", "POST"),
        ("/app/backup/enable", "POST"),
        ("/app/backup/enable-hosted", "POST"),
        ("/app/backup/destination", "POST"),
        ("/app/backup/backup-now", "POST"),
        ("/app/backup/offload/status", "GET"),
        ("/app/backup/offload/config", "POST"),
        ("/app/backup/offload/enable", "POST"),
        ("/app/backup/offload/disable", "POST"),
        ("/app/backup/offload/restore", "POST"),
        ("/app/backup/recovery-key/rotate", "POST"),
        ("/app/backup/retention", "POST"),
        ("/app/backup/teardown", "POST"),
        ("/app/backup/restore", "POST"),
        ("/app/backup/restore-hosted", "POST"),
    ):
        endpoint, _args = adapter.match(path, method=method)
        assert endpoint


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


def test_retention_accepts_numeric_strings_and_persists_ints(backup_env) -> None:
    env = backup_env()

    response = env.client.post(
        "/app/backup/retention",
        json={"hourly": "36", "daily": "9", "weekly": "5", "monthly": "18"},
    )

    assert response.status_code == 200
    assert response.get_json()["retention"] == {
        "hourly": 36,
        "daily": 9,
        "weekly": 5,
        "monthly": 18,
    }
    stored = json.loads(_config_path(env).read_text(encoding="utf-8"))
    assert stored["backup"]["retention"] == {
        "hourly": 36,
        "daily": 9,
        "weekly": 5,
        "monthly": 18,
    }


@pytest.mark.parametrize(
    "bad_value",
    [True, 1.0, None, [], {}, "1_000", "1.0", " 36", "+36", "", "-1", "٣٦"],
)
def test_retention_rejects_non_numeric_string_representations(
    backup_env,
    bad_value,
) -> None:
    env = backup_env()
    payload = {"hourly": 36, "daily": 9, "weekly": 5, "monthly": 18}
    payload["hourly"] = bad_value

    response = env.client.post("/app/backup/retention", json=payload)

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_config_value"


def _offload_status_payload(enabled: bool = False) -> dict:
    return {
        "offload": {
            "enabled": enabled,
            "budget_bytes": 100,
            "floor_bytes": 50,
        },
        "last_offload": {"status": "ok"},
        "last_verification": {"status": "ok"},
        "last_restore": {"status": "no_op", "reason": "nothing_to_restore"},
        "device": {"free_bytes": 900, "total_bytes": 1000},
        "suggested_defaults": {"budget_bytes": 500, "floor_bytes": 100},
        "raw_media": {"total_bytes": 300, "total_files": 3},
        "backup_only": {
            "total_bytes": 200,
            "total_files": 2,
            "total_segments": 1,
            "total_days": 1,
            "degraded": True,
            "skipped_records": 4,
            "unreadable_ledgers": ["health/offload/20260101.jsonl"],
        },
        "days": [
            {
                "day": "20260101",
                "raw_media_bytes": 300,
                "raw_media_files": 3,
                "backup_only_bytes": 200,
                "backup_only_files": 2,
                "backup_only_segments": 1,
                "degraded": True,
                "skipped_records": 4,
                "unreadable_ledgers": ["health/offload/20260101.jsonl"],
            }
        ],
    }


def test_offload_status_route_returns_measurements_and_degraded_signal(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    build_offload_status = Mock(return_value=_offload_status_payload(enabled=True))
    monkeypatch.setattr(backup_routes, "build_offload_status", build_offload_status)

    response = env.client.get("/app/backup/offload/status")

    assert response.status_code == 200
    data = response.get_json()
    assert data["offload"] == {
        "enabled": True,
        "budget_bytes": 100,
        "floor_bytes": 50,
    }
    assert data["last_offload"] == {"status": "ok"}
    assert data["last_verification"] == {"status": "ok"}
    assert data["last_restore"] == {
        "status": "no_op",
        "reason": "nothing_to_restore",
    }
    assert data["device"] == {"free_bytes": 900, "total_bytes": 1000}
    assert data["suggested_defaults"] == {"budget_bytes": 500, "floor_bytes": 100}
    assert data["backup_only"]["degraded"] is True
    assert data["backup_only"]["skipped_records"] == 4
    assert data["days"][0]["degraded"] is True
    assert data["operation"] is None


def test_offload_status_cache_keeps_ledger_degraded_signal_fresh(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    calls = 0

    def fake_measurement() -> OffloadStatusMeasurement:
        nonlocal calls
        calls += 1
        return OffloadStatusMeasurement(
            usage=RawMediaUsage(total_bytes=0, total_files=0, per_day=()),
            free_bytes=900,
            total_bytes=1000,
            suggested_defaults=SuggestedOffloadDefaults(
                budget_bytes=500,
                floor_bytes=100,
            ),
        )

    monkeypatch.setattr(backup_routes, "measure_offload_status", fake_measurement)
    append_offload_event(
        day="20260101",
        stream=DEFAULT_STREAM,
        segment="090000_300",
        snapshot_id="snap1",
        files=[OffloadFile(name="audio.wav", bytes=10, sha256="a" * 64)],
        time=100,
    )

    clean = env.client.get("/app/backup/offload/status").get_json()
    ledger = env.journal / "health" / "offload" / "20260101.jsonl"
    ledger.write_bytes(b"\xff")
    degraded = env.client.get("/app/backup/offload/status").get_json()

    assert calls == 1
    assert clean["backup_only"]["degraded"] is False
    assert clean["backup_only"]["total_bytes"] == 10
    assert degraded["backup_only"]["degraded"] is True
    assert degraded["backup_only"]["total_bytes"] == 0
    assert degraded["backup_only"]["unreadable_ledgers"]


def test_offload_config_preserves_enabled_and_rejects_bad_values(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    budget_bytes = 37_000_000_000
    floor_bytes = 23_000_000_000
    _write_config(
        env,
        {
            "backup": {
                "offload": {
                    "enabled": True,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                }
            }
        },
    )
    monkeypatch.setattr(
        backup_routes,
        "build_offload_status",
        Mock(return_value=_offload_status_payload(enabled=True)),
    )

    response = env.client.post(
        "/app/backup/offload/config",
        json={"budget_bytes": budget_bytes, "floor_bytes": floor_bytes},
    )
    invalid = env.client.post(
        "/app/backup/offload/config",
        json={"budget_bytes": 0, "floor_bytes": 50},
    )
    float_payload = env.client.post(
        "/app/backup/offload/config",
        json={"budget_bytes": 37.0 * 1_000_000_000, "floor_bytes": floor_bytes},
    )

    assert response.status_code == 200
    assert invalid.status_code == 400
    assert invalid.get_json()["reason_code"] == "invalid_config_value"
    assert float_payload.status_code == 400
    assert float_payload.get_json()["reason_code"] == "invalid_config_value"
    assert json.loads(_config_path(env).read_text(encoding="utf-8"))["backup"][
        "offload"
    ] == {
        "enabled": True,
        "budget_bytes": budget_bytes,
        "floor_bytes": floor_bytes,
    }


def test_offload_enable_gates_and_requests_first_verification(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    request_verification_now = Mock(return_value=True)
    monkeypatch.setattr(
        backup_routes,
        "build_offload_status",
        Mock(return_value=_offload_status_payload(enabled=True)),
    )
    monkeypatch.setattr(
        backup_routes,
        "request_verification_now",
        request_verification_now,
    )

    _write_config(
        env,
        {
            "backup": {
                "enabled": False,
                "confirmed_recovery_key": True,
                "daily_key": "daily-secret",
                "recovery_key": "A" * 64,
                "offload": {
                    "enabled": False,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                },
            }
        },
    )
    disabled = env.client.post("/app/backup/offload/enable")

    _write_config(
        env,
        {
            "backup": {
                "enabled": True,
                "confirmed_recovery_key": False,
                "daily_key": "daily-secret",
                "recovery_key": "A" * 64,
                "offload": {
                    "enabled": False,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                },
            }
        },
    )
    unconfirmed = env.client.post("/app/backup/offload/enable")

    _write_config(
        env,
        {
            "backup": {
                "enabled": True,
                "confirmed_recovery_key": True,
                "daily_key": "daily-secret",
                "recovery_key": "A" * 64,
                "offload": {
                    "enabled": False,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                },
            }
        },
    )
    enabled = env.client.post("/app/backup/offload/enable")

    assert disabled.status_code == 400
    assert disabled.get_json()["reason_code"] == "invalid_operation_for_state"
    assert unconfirmed.status_code == 400
    assert unconfirmed.get_json()["reason_code"] == "backup_not_confirmed"
    assert enabled.status_code == 200
    assert json.loads(_config_path(env).read_text(encoding="utf-8"))["backup"][
        "offload"
    ] == {
        "enabled": True,
        "budget_bytes": 10,
        "floor_bytes": 5,
    }
    request_verification_now.assert_called_once_with()


def test_offload_enable_does_not_request_verification_after_first_run(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    request_verification_now = Mock(return_value=True)
    monkeypatch.setattr(
        backup_routes,
        "build_offload_status",
        Mock(return_value=_offload_status_payload(enabled=True)),
    )
    monkeypatch.setattr(
        backup_routes,
        "request_verification_now",
        request_verification_now,
    )
    _write_config(
        env,
        {
            "backup": {
                "enabled": True,
                "confirmed_recovery_key": True,
                "daily_key": "daily-secret",
                "recovery_key": "A" * 64,
                "last_verification": {"status": "ok"},
                "offload": {
                    "enabled": False,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                },
            }
        },
    )

    response = env.client.post("/app/backup/offload/enable")

    assert response.status_code == 200
    request_verification_now.assert_not_called()


def test_offload_disable_preserves_config_and_does_not_touch_ledger_or_media(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    ledger = env.journal / "health" / "offload" / "20260101.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"event_kind":"offload"}\n', encoding="utf-8")
    media = env.journal / "chronicle" / "20260101" / "090000_300" / "audio.wav"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"raw")
    _write_config(
        env,
        {
            "backup": {
                "enabled": True,
                "mode": "operated",
                "daily_key": "daily-secret",
                "recovery_key": "A" * 64,
                "confirmed_recovery_key": True,
                "custom_marker": {"keep": True},
                "offload": {
                    "enabled": True,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                },
            }
        },
    )
    monkeypatch.setattr(
        backup_routes,
        "build_offload_status",
        Mock(return_value=_offload_status_payload(enabled=False)),
    )

    response = env.client.post("/app/backup/offload/disable")

    assert response.status_code == 200
    backup = json.loads(_config_path(env).read_text(encoding="utf-8"))["backup"]
    assert backup["mode"] == "operated"
    assert backup["daily_key"] == "daily-secret"
    assert backup["custom_marker"] == {"keep": True}
    assert backup["offload"] == {
        "enabled": False,
        "budget_bytes": 10,
        "floor_bytes": 5,
    }
    assert ledger.read_text(encoding="utf-8") == '{"event_kind":"offload"}\n'
    assert media.read_bytes() == b"raw"


def test_offload_restore_rejects_invalid_day_before_side_effects(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    restore_day = Mock()
    restore_all = Mock()
    monkeypatch.setattr(backup_routes, "restore_day", restore_day)
    monkeypatch.setattr(backup_routes, "restore_all", restore_all)

    response = env.client.post(
        "/app/backup/offload/restore",
        json={"day": "20260230"},
    )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_day"
    restore_day.assert_not_called()
    restore_all.assert_not_called()
    assert not (env.journal / "chronicle" / "20260230").exists()


@pytest.mark.parametrize("offload_enabled", [True, False])
def test_offload_restore_route_accepts_day_with_offload_enabled_or_disabled(
    backup_env,
    monkeypatch,
    wait_until_helper,
    offload_enabled: bool,
) -> None:
    env = backup_env()
    _write_config(
        env,
        {
            "backup": {
                "enabled": True,
                "offload": {
                    "enabled": offload_enabled,
                    "budget_bytes": 10,
                    "floor_bytes": 5,
                },
            }
        },
    )
    restore_day = Mock(return_value=SimpleNamespace(status="ok", reason=None))
    monkeypatch.setattr(backup_routes, "restore_day", restore_day)

    response = env.client.post(
        "/app/backup/offload/restore",
        json={"day": "20260228"},
    )
    final = _wait_for_phase(env, wait_until_helper, "done")

    assert response.status_code == 202
    assert final["reason_code"] is None
    restore_day.assert_called_once_with("20260228")


def test_offload_restore_route_accepts_all_and_rejects_mixed_scope(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    restore_all = Mock(
        return_value=SimpleNamespace(status="no_op", reason="nothing_to_restore")
    )
    restore_day = Mock()
    monkeypatch.setattr(backup_routes, "restore_all", restore_all)
    monkeypatch.setattr(backup_routes, "restore_day", restore_day)

    response = env.client.post("/app/backup/offload/restore", json={"all": True})
    final = _wait_for_phase(env, wait_until_helper, "done")
    backup_routes._clear_registry()
    mixed = env.client.post(
        "/app/backup/offload/restore",
        json={"all": True, "day": "20260228"},
    )

    assert response.status_code == 202
    assert final["reason_code"] == "nothing_to_restore"
    restore_all.assert_called_once_with()
    restore_day.assert_not_called()
    assert mixed.status_code == 400
    assert mixed.get_json()["reason_code"] == "invalid_request_value"


def test_offload_restore_busy_path_uses_single_long_op_slot(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    started = threading.Event()
    release = threading.Event()

    def slow_restore(_day: str) -> SimpleNamespace:
        started.set()
        release.wait(2)
        return SimpleNamespace(status="ok", reason=None)

    monkeypatch.setattr(backup_routes, "restore_day", slow_restore)

    first = env.client.post("/app/backup/offload/restore", json={"day": "20260228"})
    wait_until_helper(started.is_set)
    second = env.client.post("/app/backup/offload/restore", json={"day": "20260228"})
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "backup_busy"


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


def test_restore_route_reports_degraded_as_terminal_phase(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    restore_journal = Mock(
        return_value=RestoreResult(
            status="degraded",
            reason_code="integrity_failed",
            integrity_ok=False,
            resumable=True,
            bytes_restored=123,
        )
    )
    monkeypatch.setattr(backup_routes, "restore_journal", restore_journal)

    response = env.client.post(
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
    final = _wait_for_phase(env, wait_until_helper, "degraded")
    js_text = Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")

    assert response.status_code == 202
    assert final["reason_code"] == "integrity_failed"
    assert "degraded" in backup_routes.TERMINAL_PHASES
    assert (
        "new Set(['done', 'error', 'needs_subscription', 'degraded', 'refused'])"
        in js_text
    )


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


def test_enable_hosted_requires_confirmed_key_and_keys(
    backup_env,
    monkeypatch,
) -> None:
    env = backup_env()
    _write_config(env, _configured_keys(confirmed=False))
    build = _mock_handoff_url(monkeypatch)

    unconfirmed = env.client.post("/app/backup/enable-hosted")

    assert unconfirmed.status_code == 400
    assert unconfirmed.get_json()["reason_code"] == "backup_not_confirmed"
    build.assert_not_called()

    backup_routes._clear_registry()
    _write_config(env, {"backup": {"confirmed_recovery_key": True}})

    no_keys = env.client.post("/app/backup/enable-hosted")

    assert no_keys.status_code == 400
    assert no_keys.get_json()["reason_code"] == "invalid_operation_for_state"


def test_enable_hosted_returns_portal_url_and_polls_to_needs_subscription(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, _configured_keys())
    _mock_handoff_url(monkeypatch)
    save_hosted_binding = Mock()
    set_mode = Mock()
    monkeypatch.setattr(
        backup_routes,
        "run_spb_handoff",
        Mock(
            return_value=_handoff(
                "needs_subscription",
                subscribe_url=SUBSCRIBE_URL,
            )
        ),
    )
    monkeypatch.setattr(backup_routes, "save_hosted_binding", save_hosted_binding)
    monkeypatch.setattr(backup_routes, "set_mode", set_mode)

    response = env.client.post("/app/backup/enable-hosted")
    operation = response.get_json()["operation"]
    final = _wait_for_phase(env, wait_until_helper, "needs_subscription")

    assert response.status_code == 202
    assert operation["portal_url"] == CONSENT_URL
    assert final["subscribe_url"] == SUBSCRIBE_URL
    assert final["portal_url"] is None
    save_hosted_binding.assert_not_called()
    set_mode.assert_not_called()


def test_enable_hosted_approved_initializes_then_persists_and_queues(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, _configured_keys())
    binding = _binding()
    _mock_handoff_url(monkeypatch)
    init_repository = Mock()
    request_backup_now = Mock(return_value=True)
    save_hosted_binding = Mock()
    set_mode = Mock()
    set_enabled = Mock()
    monkeypatch.setattr(
        backup_routes,
        "run_spb_handoff",
        Mock(return_value=_handoff("approved", binding=binding)),
    )
    monkeypatch.setattr(
        backup_routes,
        "fetch_hosted_credentials",
        Mock(return_value=_creds()),
    )
    monkeypatch.setattr(
        backup_routes,
        "ensure_restic",
        Mock(return_value=Path("/restic")),
    )
    monkeypatch.setattr(backup_routes, "init_repository", init_repository)
    monkeypatch.setattr(backup_routes, "request_backup_now", request_backup_now)
    monkeypatch.setattr(backup_routes, "save_hosted_binding", save_hosted_binding)
    monkeypatch.setattr(backup_routes, "set_mode", set_mode)
    monkeypatch.setattr(backup_routes, "set_enabled", set_enabled)

    response = env.client.post("/app/backup/enable-hosted")
    final = _wait_for_phase(env, wait_until_helper, "done")

    assert response.status_code == 202
    assert response.get_json()["operation"]["portal_url"] == CONSENT_URL
    assert final["reason_code"] is None
    init_repository.assert_called_once()
    destination = init_repository.call_args.args[0]
    assert destination.repository == "s3:https://r2.example/bkt/users/acct/inst"
    assert init_repository.call_args.kwargs["daily_key"] == "daily-secret"
    assert init_repository.call_args.kwargs["recovery_key"] == "A" * 64
    assert init_repository.call_args.kwargs["restic_path"] == Path("/restic")
    assert init_repository.call_args.kwargs["timeout"] == backup_routes.ENABLE_TIMEOUT
    save_hosted_binding.assert_called_once_with(binding)
    set_mode.assert_called_once_with("operated")
    set_enabled.assert_called_once_with(True)
    request_backup_now.assert_called_once_with()


def test_enable_hosted_broker_error_does_not_persist(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, _configured_keys())
    _mock_handoff_url(monkeypatch)
    save_hosted_binding = Mock()
    set_mode = Mock()
    init_repository = Mock()
    monkeypatch.setattr(
        backup_routes,
        "run_spb_handoff",
        Mock(return_value=_handoff("approved", binding=_binding())),
    )
    monkeypatch.setattr(
        backup_routes,
        "fetch_hosted_credentials",
        Mock(side_effect=HostedCredsUnavailable("hosted_entitlement_inactive")),
    )
    monkeypatch.setattr(backup_routes, "save_hosted_binding", save_hosted_binding)
    monkeypatch.setattr(backup_routes, "set_mode", set_mode)
    monkeypatch.setattr(backup_routes, "init_repository", init_repository)

    response = env.client.post("/app/backup/enable-hosted")
    final = _wait_for_phase(env, wait_until_helper, "error")

    assert response.status_code == 202
    assert final["reason_code"] == "hosted_entitlement_inactive"
    save_hosted_binding.assert_not_called()
    set_mode.assert_not_called()
    init_repository.assert_not_called()


def test_restore_hosted_requires_recovery_key(backup_env) -> None:
    env = backup_env()

    response = env.client.post("/app/backup/restore-hosted", json={})

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "missing_required_field"


def test_restore_hosted_approved_works_without_local_keys(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, {"backup": {}})
    binding = _binding()
    _mock_handoff_url(monkeypatch)
    save_hosted_binding = Mock()
    restore_journal_operated = Mock(
        return_value=SimpleNamespace(status="ok", reason_code=None)
    )
    fetch_hosted_credentials = Mock(return_value=_creds())
    monkeypatch.setattr(
        backup_routes,
        "run_spb_handoff",
        Mock(return_value=_handoff("approved", binding=binding)),
    )
    monkeypatch.setattr(
        backup_routes,
        "fetch_hosted_credentials",
        fetch_hosted_credentials,
    )
    monkeypatch.setattr(backup_routes, "save_hosted_binding", save_hosted_binding)
    monkeypatch.setattr(
        backup_routes,
        "restore_journal_operated",
        restore_journal_operated,
    )

    response = env.client.post(
        "/app/backup/restore-hosted",
        json={"recovery_key": "A" * 64},
    )
    final = _wait_for_phase(env, wait_until_helper, "done")

    assert response.status_code == 202
    assert response.get_json()["operation"]["portal_url"] == CONSENT_URL
    assert final["reason_code"] is None
    save_hosted_binding.assert_called_once_with(binding)
    fetch_hosted_credentials.assert_called_once_with(binding, scope="operated")
    restore_journal_operated.assert_called_once_with(binding, _creds(), "A" * 64)


def test_restore_hosted_needs_subscription_returns_terminal_phase(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, {"backup": {}})
    _mock_handoff_url(monkeypatch)
    save_hosted_binding = Mock()
    restore_journal_operated = Mock()
    monkeypatch.setattr(
        backup_routes,
        "run_spb_handoff",
        Mock(
            return_value=_handoff(
                "needs_subscription",
                subscribe_url=SUBSCRIBE_URL,
            )
        ),
    )
    monkeypatch.setattr(backup_routes, "save_hosted_binding", save_hosted_binding)
    monkeypatch.setattr(
        backup_routes,
        "restore_journal_operated",
        restore_journal_operated,
    )

    response = env.client.post(
        "/app/backup/restore-hosted",
        json={"recovery_key": "A" * 64},
    )
    final = _wait_for_phase(env, wait_until_helper, "needs_subscription")

    assert response.status_code == 202
    assert final["subscribe_url"] == SUBSCRIBE_URL
    assert final["portal_url"] is None
    save_hosted_binding.assert_not_called()
    restore_journal_operated.assert_not_called()


def test_hosted_operation_uses_single_busy_slot(
    backup_env,
    monkeypatch,
    wait_until_helper,
) -> None:
    env = backup_env()
    _write_config(env, _configured_keys())
    started = threading.Event()
    release = threading.Event()
    _mock_handoff_url(monkeypatch)

    def slow_handoff(*_args, **_kwargs):
        started.set()
        release.wait(2)
        return _handoff("needs_subscription", subscribe_url=SUBSCRIBE_URL)

    monkeypatch.setattr(backup_routes, "run_spb_handoff", slow_handoff)

    first = env.client.post("/app/backup/enable-hosted")
    wait_until_helper(started.is_set)
    second = env.client.post(
        "/app/backup/restore-hosted",
        json={"recovery_key": "A" * 64},
    )
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "backup_busy"

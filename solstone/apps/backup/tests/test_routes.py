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
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
)
from solstone.think.backup.restore import RestoreResult

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
    assert backup["spa"] is True
    assert backup["workspace_url"] == "/app/backup/workspace"
    assert "backup" not in DEFAULT_APP_ORDER


def test_backup_spa_shell_workspace_and_route_resolution(backup_env) -> None:
    env = backup_env()
    workspace_path = Path("solstone/apps/backup/workspace.html")
    routes_source = Path("solstone/apps/backup/routes.py").read_text(encoding="utf-8")
    workspace = workspace_path.read_text(encoding="utf-8")
    js = Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")
    app_json = json.loads(
        Path("solstone/apps/backup/app.json").read_text(encoding="utf-8")
    )

    index_response = env.client.get("/app/backup/")
    workspace_response = env.client.get("/app/backup/workspace")

    assert app_json["spa"] is True
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
    assert "new Set(['done', 'error', 'needs_subscription', 'degraded'])" in js_text


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
    restore_journal_operated.assert_called_once()
    destination = restore_journal_operated.call_args.args[0]
    assert destination.credentials["session_token"] == "SESS"
    assert restore_journal_operated.call_args.args[1] == "A" * 64


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


def test_forbidden_terms_absent_from_backup_surfaces(backup_env, monkeypatch) -> None:
    env = backup_env()
    monkeypatch.setattr(backup_routes, "request_backup_now", Mock(return_value=False))
    workspace = Path("solstone/apps/backup/workspace.html").read_text(encoding="utf-8")
    js = Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")
    status = env.client.get("/app/backup/status").get_json()
    haystack = "\n".join(
        [
            workspace,
            js,
            "\n".join(backup_copy_values()),
            json.dumps(status, sort_keys=True),
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

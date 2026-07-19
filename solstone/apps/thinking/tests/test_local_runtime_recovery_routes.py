# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.apps.thinking import local_recovery
from solstone.think.providers.runtime_health import (
    make_synthetic_runtime_health,
    read_retry_token,
    request_retry_token,
    runtime_health_path,
    write_runtime_health,
)


def _write_failed_runtime(journal_path: Path):
    record = make_synthetic_runtime_health("local")
    record["phase"] = "failed"
    record["reason_code"] = "launch-budget-exhausted"
    record["desired_fingerprint_sha256"] = "fp-local"
    record["attempt"] = 6
    record["detail"] = {
        "error": "sensitive backend exception",
        "path": "/private/model/path",
    }
    record["process"] = {
        "name": "llama-server",
        "pid": 4242,
        "ref": "private-ref",
        "port": 43210,
    }
    record["updated_at"] = "2026-07-19T10:00:00+00:00"
    return write_runtime_health(record, journal_path=journal_path)


def _finish_setup(journal_path: Path, config: dict) -> None:
    config["setup"] = {"completed_at": 1700000000000}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def test_runtime_view_exposes_only_owner_safe_recovery_projection(
    settings_env,
) -> None:
    journal_path, config = settings_env()
    _finish_setup(journal_path, config)
    stored = _write_failed_runtime(journal_path)

    view = local_recovery.runtime_view()

    assert view == {
        "status": "ok",
        "phase": "failed",
        "reason_code": "launch-budget-exhausted",
        "health_revision": stored["revision"],
        "desired_fingerprint_sha256": "fp-local",
        "retry_revision": 0,
        "retry_pending": False,
        "can_retry": True,
        "poll": False,
        "updated_at": "2026-07-19T10:00:00+00:00",
    }
    assert "process" not in view
    assert "detail" not in view
    assert "token_id" not in view


def test_runtime_view_distinguishes_corrupt_and_unavailable_state(
    settings_env,
    monkeypatch,
) -> None:
    journal_path, config = settings_env()
    _finish_setup(journal_path, config)
    path = runtime_health_path("local", journal_path=journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    corrupt = local_recovery.runtime_view()
    assert corrupt["status"] == "corrupt"
    assert corrupt["phase"] == "state-corrupt"
    assert corrupt["can_retry"] is False

    def unavailable(_provider):
        return {
            "status": "unavailable",
            "provider": "local",
            "record_kind": "health",
            "path": "/not-owner-visible",
            "record": None,
            "reason_code": "record-unavailable",
            "error": "permission denied",
        }

    monkeypatch.setattr(local_recovery, "inspect_runtime_health", unavailable)
    unavailable_view = local_recovery.runtime_view()
    assert unavailable_view["status"] == "unavailable"
    assert unavailable_view["phase"] == "state-unavailable"
    assert "path" not in unavailable_view


def test_runtime_view_does_not_block_current_target_on_old_retry_token(
    settings_env,
) -> None:
    journal_path, config = settings_env()
    _finish_setup(journal_path, config)
    request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-old",
        journal_path=journal_path,
    )
    _write_failed_runtime(journal_path)

    view = local_recovery.runtime_view()

    assert view["phase"] == "failed"
    assert view["retry_pending"] is False
    assert view["can_retry"] is True


def test_runtime_retry_route_is_single_use_compare_and_set(
    settings_env,
    thinking_app,
) -> None:
    from solstone.convey import state

    journal_path, config = settings_env()
    _finish_setup(journal_path, config)
    state.journal_root = str(journal_path)
    _write_failed_runtime(journal_path)
    client = thinking_app.test_client()

    current_response = client.get("/app/thinking/api/local/runtime")
    assert current_response.status_code == 200
    current = current_response.get_json()
    assert current["can_retry"] is True

    request_body = {
        "health_revision": current["health_revision"],
        "retry_revision": current["retry_revision"],
        "desired_fingerprint_sha256": current["desired_fingerprint_sha256"],
    }
    first = client.post(
        "/app/thinking/api/local/runtime/retry",
        json=request_body,
    )
    assert first.status_code == 200
    requested = first.get_json()
    assert requested["phase"] == "retry-requested"
    assert requested["retry_pending"] is True
    assert requested["can_retry"] is False

    token = read_retry_token("local", journal_path=journal_path)
    assert token["token_id"] is not None
    assert token["desired_fingerprint_sha256"] == "fp-local"

    replay = client.post(
        "/app/thinking/api/local/runtime/retry",
        json=request_body,
    )
    assert replay.status_code == 400
    assert replay.get_json()["reason_code"] == "invalid_operation_for_state"
    assert read_retry_token("local", journal_path=journal_path) == token


def test_runtime_retry_route_rejects_untrusted_or_nonterminal_requests(
    settings_env,
    thinking_app,
) -> None:
    from solstone.convey import state

    journal_path, config = settings_env()
    _finish_setup(journal_path, config)
    state.journal_root = str(journal_path)
    client = thinking_app.test_client()

    missing = client.post("/app/thinking/api/local/runtime/retry")
    assert missing.status_code == 400
    assert missing.get_json()["reason_code"] == "missing_request_body"

    extra = client.post(
        "/app/thinking/api/local/runtime/retry",
        json={
            "health_revision": 0,
            "retry_revision": 0,
            "desired_fingerprint_sha256": "fp-local",
            "launch": True,
        },
    )
    assert extra.status_code == 400
    assert extra.get_json()["reason_code"] == "invalid_request_value"

    stopped = client.post(
        "/app/thinking/api/local/runtime/retry",
        json={
            "health_revision": 0,
            "retry_revision": 0,
            "desired_fingerprint_sha256": "fp-local",
        },
    )
    assert stopped.status_code == 400
    assert stopped.get_json()["reason_code"] == "invalid_operation_for_state"
    assert read_retry_token("local", journal_path=journal_path)["token_id"] is None

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, g

import solstone.convey.state as convey_state
import solstone.think.utils as think_utils
from solstone.convey.reasons import (
    IMPORT_CLIENT_ID_CONFLICT,
    IMPORT_CONFLICT,
    IMPORT_METADATA_FAILED,
    IMPORT_NOT_FOUND,
    IMPORT_QUEUE_UNREACHABLE,
    INVALID_OPERATION_FOR_STATE,
    MISSING_REQUIRED_FIELD,
)
from solstone.think.importers.utils import (
    read_import_metadata,
    update_import_metadata_fields,
    write_import_metadata,
)

import_routes = __import__("solstone.apps.import.routes", fromlist=["routes"])
import_contract = __import__("solstone.apps.import.contract", fromlist=["contract"])
_ACTION_SCHEMA = import_contract._ACTION_SCHEMA
_SAVE_RESPONSE_FIELDS = import_contract._SAVE_RESPONSE_FIELDS


@pytest.fixture
def journal_env(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(convey_state, "journal_root", str(tmp_path), raising=False)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None
    (tmp_path / "imports").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def _stable_timestamp_detection(monkeypatch):
    monkeypatch.setattr(
        import_routes,
        "resolve_created_deterministic",
        lambda *args, **kwargs: None,
    )

    def _no_model(*args, **kwargs):
        raise AssertionError("model timestamp detection should not run")

    monkeypatch.setattr(import_routes, "detect_created", _no_model)


@pytest.fixture
def client(journal_env):
    app = Flask(__name__)

    @app.before_request
    def _identity() -> None:
        g.identity = SimpleNamespace(mode="local", fingerprint=None)

    app.register_blueprint(import_routes.import_bp)
    return app.test_client()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _import_dirs(journal_root: Path) -> list[Path]:
    return sorted(
        path for path in (journal_root / "imports").iterdir() if path.is_dir()
    )


def _save_upload(
    client,
    *,
    client_item_id: str,
    content: bytes = b"sample audio",
    filename: str = "sample.m4a",
    content_type: str = "audio/mp4",
    extra: dict | None = None,
):
    data = {
        "client_item_id": client_item_id,
        "deterministic_only": "true",
        "file": (io.BytesIO(content), filename, content_type),
    }
    if extra:
        data.update(extra)
    return client.post(
        "/app/import/api/save",
        data=data,
        content_type="multipart/form-data",
    )


def _save_path(client, *, client_item_id: str, path: Path):
    return client.post(
        "/app/import/api/save-path",
        json={"client_item_id": client_item_id, "path": str(path)},
    )


def _write_imported_results(
    journal_root: Path,
    timestamp: str,
    results: dict,
) -> None:
    import_dir = journal_root / "imports" / timestamp
    import_dir.mkdir(parents=True, exist_ok=True)
    (import_dir / "imported.json").write_text(
        json.dumps(results),
        encoding="utf-8",
    )


def _write_manifest(
    journal_root: Path,
    *,
    import_id: str,
    source_hash: str,
    entry_count: int = 2,
) -> None:
    import_dir = journal_root / "imports" / import_id
    import_dir.mkdir(parents=True)
    (import_dir / "manifest.json").write_text(
        json.dumps(
            {
                "import_id": import_id,
                "source_type": "audio",
                "source_hash": source_hash,
                "entry_count": entry_count,
                "imported_at": "2026-01-01T12:00:00",
                "imported_via": "cli",
            }
        ),
        encoding="utf-8",
    )


def _write_staged_import(
    journal_root: Path,
    timestamp: str,
    metadata: dict,
) -> Path:
    import_dir = journal_root / "imports" / timestamp
    import_dir.mkdir(parents=True)
    media_path = import_dir / metadata.get("original_filename", "sample.m4a")
    media_path.write_bytes(b"sample")
    metadata = {
        "file_path": str(media_path),
        "user_timestamp": timestamp,
        "source": "audio",
        "source_inference": "extension",
        "client": {},
        "facet": None,
        "setting": None,
        "imported_via": "web_dashboard",
        "observer_handle": None,
        "source_hint": None,
        "mime_type": "audio/mp4",
        **metadata,
    }
    write_import_metadata(journal_root, timestamp, metadata)
    return media_path


def test_import_save_audio_upload_stages_versioned_summary(client, journal_env):
    response = _save_upload(
        client,
        client_item_id="ios-audio-1",
        content=b"audio bytes",
        extra={"facet": "work", "client": json.dumps({"device": "ios"})},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == 1
    assert body["status"] == "staged"
    assert body["replay"] is False
    assert body["source"] == "audio"
    assert body["client_item_id"] == "ios-audio-1"
    assert body["recommended_action"] == "start"
    assert body["facet"] == "work"
    assert body["metadata"] == {
        "original_filename": "sample.m4a",
        "mime_type": "audio/mp4",
        "imported_via": "web_dashboard",
        "observer_handle": None,
        "source_hint": None,
        "client": {"device": "ios"},
    }
    assert body["diagnostics"]["source_inference"] == "extension"
    assert "timestamp_detection_method" not in body
    assert "dedup" not in body

    metadata = read_import_metadata(journal_env, body["timestamp"])
    assert metadata["client_item_id"] == "ios-audio-1"
    assert metadata["source_hash"] == _sha(b"audio bytes")
    assert metadata["source"] == "audio"
    assert metadata["client"] == {"device": "ios"}


def test_save_response_contract_keeps_action_enum_and_optional_in_progress():
    fields = {field.name: field for field in _SAVE_RESPONSE_FIELDS}

    assert set(_ACTION_SCHEMA["enum"]) == {"start", "do_not_start"}
    assert fields["in_progress"].type == "boolean"
    assert fields["in_progress"].required is False


def test_save_recommended_action_values_match_contract(client, journal_env):
    allowed_actions = set(_ACTION_SCHEMA["enum"])
    content = b"contract action"
    fresh = _save_upload(
        client,
        client_item_id="contract-fresh",
        content=content,
    ).get_json()
    _write_manifest(
        journal_env,
        import_id="20260101_120001",
        source_hash=_sha(b"contract-imported"),
    )
    imported_duplicate = _save_upload(
        client,
        client_item_id="contract-imported",
        content=b"contract-imported",
    ).get_json()
    _write_staged_import(
        journal_env,
        "20260101_120002",
        {
            "original_filename": "contract-pending.m4a",
            "client_item_id": "contract-pending-other",
            "source_hash": _sha(b"contract-pending"),
        },
    )
    pending_staged = _save_upload(
        client,
        client_item_id="contract-pending-fresh",
        content=b"contract-pending",
    ).get_json()
    update_import_metadata_fields(
        journal_root=journal_env,
        timestamp=fresh["timestamp"],
        updates={"task_id": "task-running"},
    )
    running_replay = _save_upload(
        client,
        client_item_id="contract-fresh",
        content=content,
    ).get_json()

    for body in (fresh, imported_duplicate, pending_staged, running_replay):
        assert body["recommended_action"] in allowed_actions


def test_import_save_missing_client_item_id_returns_missing_required(client):
    response = client.post(
        "/app/import/api/save",
        data={
            "deterministic_only": "true",
            "file": (io.BytesIO(b"audio"), "sample.m4a", "audio/mp4"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == MISSING_REQUIRED_FIELD.status
    assert response.get_json()["reason_code"] == MISSING_REQUIRED_FIELD.code


def test_import_save_replay_same_client_and_content_does_not_stage_again(
    client, journal_env
):
    first = _save_upload(
        client,
        client_item_id="ios-replay",
        content=b"same bytes",
    )
    before_dirs = _import_dirs(journal_env)

    second = _save_upload(
        client,
        client_item_id="ios-replay",
        content=b"same bytes",
    )

    assert second.status_code == 200
    body = second.get_json()
    assert body["status"] == "staged"
    assert body["replay"] is True
    assert body["recommended_action"] == "start"
    assert body["path"] == first.get_json()["path"]
    assert _import_dirs(journal_env) == before_dirs


def test_import_save_replay_started_item_does_not_recommend_start(client, journal_env):
    first = _save_upload(
        client,
        client_item_id="ios-replay-started",
        content=b"same started bytes",
    )
    timestamp = first.get_json()["timestamp"]
    update_import_metadata_fields(
        journal_root=journal_env,
        timestamp=timestamp,
        updates={"task_id": "task-started"},
    )
    before_dirs = _import_dirs(journal_env)

    second = _save_upload(
        client,
        client_item_id="ios-replay-started",
        content=b"same started bytes",
    )

    assert second.status_code == 200
    body = second.get_json()
    assert body["status"] == "staged"
    assert body["replay"] is True
    assert body["recommended_action"] == "do_not_start"
    assert body["in_progress"] is True
    assert body["path"] == first.get_json()["path"]
    assert _import_dirs(journal_env) == before_dirs


def test_import_save_same_client_different_content_conflicts(client):
    _save_upload(client, client_item_id="ios-conflict", content=b"one")

    response = _save_upload(client, client_item_id="ios-conflict", content=b"two")

    assert response.status_code == IMPORT_CLIENT_ID_CONFLICT.status
    assert response.get_json()["reason_code"] == IMPORT_CLIENT_ID_CONFLICT.code


def test_import_save_duplicate_imported_content_is_terminal(client, journal_env):
    content = b"already imported"
    _write_manifest(
        journal_env,
        import_id="20260101_120000",
        source_hash=_sha(content),
        entry_count=3,
    )
    before_dirs = _import_dirs(journal_env)

    response = _save_upload(
        client,
        client_item_id="ios-imported-dup",
        content=content,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "duplicate"
    assert body["replay"] is False
    assert body["recommended_action"] == "do_not_start"
    assert body["duplicate"] == {
        "import_id": "20260101_120000",
        "imported_at": "2026-01-01T12:00:00",
        "entry_count": 3,
        "state": "imported",
    }
    assert _import_dirs(journal_env) == before_dirs
    assert not any((path / "import.json").exists() for path in before_dirs)


def test_import_save_pending_source_hash_match_offers_existing_import(
    client, journal_env
):
    content = b"already staged"
    _write_staged_import(
        journal_env,
        "20260101_121500",
        {
            "original_filename": "existing.m4a",
            "client_item_id": "other-client",
            "source_hash": _sha(content),
        },
    )
    before_dirs = _import_dirs(journal_env)

    response = _save_upload(
        client,
        client_item_id="ios-staged-fresh-client",
        content=content,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "staged"
    assert body["client_item_id"] == "ios-staged-fresh-client"
    assert body["timestamp"] == "20260101_121500"
    assert body["recommended_action"] == "start"
    assert body["replay"] is False
    assert "duplicate" not in body
    assert _import_dirs(journal_env) == before_dirs


def test_import_save_path_pending_source_hash_match_offers_existing_import(
    client, journal_env
):
    content = b"already staged path"
    local_path = journal_env / "existing-source.m4a"
    local_path.write_bytes(content)
    _write_staged_import(
        journal_env,
        "20260101_121501",
        {
            "original_filename": "existing-source.m4a",
            "client_item_id": "other-client",
            "file_path": str(local_path),
            "source_hash": _sha(content),
        },
    )
    before_dirs = _import_dirs(journal_env)

    response = _save_path(
        client,
        client_item_id="ios-staged-path-fresh-client",
        path=local_path,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "staged"
    assert body["client_item_id"] == "ios-staged-path-fresh-client"
    assert body["timestamp"] == "20260101_121501"
    assert body["recommended_action"] == "start"
    assert body["replay"] is False
    assert "duplicate" not in body
    assert _import_dirs(journal_env) == before_dirs


@pytest.mark.parametrize(
    ("imported_results", "expected_action", "expected_in_progress"),
    [
        ({"error": "parse failed", "error_stage": "parse"}, "start", False),
        ({"total_files_created": 1}, "do_not_start", False),
        (None, "do_not_start", True),
    ],
)
def test_duplicate_or_replay_response_replay_uses_resolved_terminal_status(
    client,
    journal_env,
    imported_results,
    expected_action,
    expected_in_progress,
):
    content = f"replay {expected_action} {expected_in_progress}".encode()
    first = _save_upload(
        client,
        client_item_id=f"ios-replay-{expected_action}-{expected_in_progress}",
        content=content,
    )
    timestamp = first.get_json()["timestamp"]
    if imported_results is not None:
        _write_imported_results(journal_env, timestamp, imported_results)
    else:
        update_import_metadata_fields(
            journal_root=journal_env,
            timestamp=timestamp,
            updates={"task_id": "task-running"},
        )

    response = _save_upload(
        client,
        client_item_id=f"ios-replay-{expected_action}-{expected_in_progress}",
        content=content,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "staged"
    assert body["replay"] is True
    assert body["recommended_action"] == expected_action
    if expected_in_progress:
        assert body["in_progress"] is True
    else:
        assert "in_progress" not in body


def test_import_save_failed_source_hash_match_offers_start(client, journal_env):
    content = b"failed staged"
    _write_staged_import(
        journal_env,
        "20260101_121502",
        {
            "original_filename": "failed.m4a",
            "client_item_id": "failed-other-client",
            "source_hash": _sha(content),
        },
    )
    _write_imported_results(
        journal_env,
        "20260101_121502",
        {"error": "parse failed", "error_stage": "parse"},
    )

    response = _save_upload(
        client,
        client_item_id="failed-fresh-client",
        content=content,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "staged"
    assert body["recommended_action"] == "start"
    assert body["timestamp"] == "20260101_121502"
    assert body["replay"] is False
    assert "duplicate" not in body


def test_import_save_path_failed_source_hash_match_offers_start(client, journal_env):
    content = b"failed staged path"
    local_path = journal_env / "failed-source.m4a"
    local_path.write_bytes(content)
    _write_staged_import(
        journal_env,
        "20260101_121503",
        {
            "original_filename": "failed-source.m4a",
            "client_item_id": "failed-path-other-client",
            "file_path": str(local_path),
            "source_hash": _sha(content),
        },
    )
    _write_imported_results(
        journal_env,
        "20260101_121503",
        {"error": "parse failed", "error_stage": "parse"},
    )

    response = _save_path(
        client,
        client_item_id="failed-path-fresh-client",
        path=local_path,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "staged"
    assert body["recommended_action"] == "start"
    assert body["timestamp"] == "20260101_121503"
    assert body["replay"] is False
    assert "duplicate" not in body


def test_import_save_failed_staged_hash_with_manifest_stays_terminal(
    client, journal_env
):
    content = b"failed but manifest exists"
    source_hash = _sha(content)
    _write_staged_import(
        journal_env,
        "20260101_121504",
        {
            "original_filename": "failed-manifest.m4a",
            "client_item_id": "failed-manifest-other-client",
            "source_hash": source_hash,
        },
    )
    _write_imported_results(
        journal_env,
        "20260101_121504",
        {"error": "parse failed", "error_stage": "parse"},
    )
    _write_manifest(
        journal_env,
        import_id="20260101_121505",
        source_hash=source_hash,
    )

    response = _save_upload(
        client,
        client_item_id="failed-manifest-fresh-client",
        content=content,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "duplicate"
    assert body["recommended_action"] == "do_not_start"
    assert body["duplicate"]["state"] == "imported"


def test_import_meta_updates_facet_and_setting(client, journal_env):
    media_path = _write_staged_import(
        journal_env,
        "20260101_130000",
        {"original_filename": "meta.m4a", "client_item_id": "meta-client"},
    )

    response = client.post(
        "/app/import/api/meta",
        json={"path": str(media_path), "facet": "work", "setting": "office"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "path": str(media_path),
        "timestamp": "20260101_130000",
        "updated": {"facet": "work", "setting": "office"},
    }
    metadata = read_import_metadata(journal_env, "20260101_130000")
    assert metadata["facet"] == "work"
    assert metadata["setting"] == "office"


def test_import_meta_missing_path_returns_missing_required(client):
    response = client.post("/app/import/api/meta", json={})

    assert response.status_code == MISSING_REQUIRED_FIELD.status
    assert response.get_json()["reason_code"] == MISSING_REQUIRED_FIELD.code


def test_import_facet_route_removed_no_alias(client):
    rules = list(client.application.url_map.iter_rules())

    assert not any(rule.rule == "/app/import/api/facet" for rule in rules)
    assert any(
        rule.rule == "/app/import/api/meta" and "POST" in rule.methods for rule in rules
    )


def test_import_meta_missing_item_returns_import_not_found(client, journal_env):
    missing_path = journal_env / "imports" / "20260101_130001" / "sample.m4a"

    response = client.post(
        "/app/import/api/meta",
        json={"path": str(missing_path), "facet": "work"},
    )

    assert response.status_code == IMPORT_NOT_FOUND.status
    assert response.get_json()["reason_code"] == IMPORT_NOT_FOUND.code


def test_import_meta_failed_item_allows_update(client, journal_env):
    media_path = _write_staged_import(
        journal_env,
        "20260101_130001",
        {
            "original_filename": "failed-meta.m4a",
            "client_item_id": "failed-meta-client",
        },
    )
    _write_imported_results(
        journal_env,
        "20260101_130001",
        {"error": "parse failed", "error_stage": "parse"},
    )

    response = client.post(
        "/app/import/api/meta",
        json={"path": str(media_path), "facet": "personal"},
    )

    assert response.status_code == 200
    metadata = read_import_metadata(journal_env, "20260101_130001")
    assert metadata["facet"] == "personal"


def test_import_meta_success_item_returns_invalid_operation(client, journal_env):
    media_path = _write_staged_import(
        journal_env,
        "20260101_130003",
        {
            "original_filename": "success-meta.m4a",
            "client_item_id": "success-meta-client",
        },
    )
    _write_imported_results(
        journal_env,
        "20260101_130003",
        {"total_files_created": 1},
    )

    response = client.post(
        "/app/import/api/meta",
        json={"path": str(media_path), "facet": "work"},
    )

    assert response.status_code == INVALID_OPERATION_FOR_STATE.status
    assert response.get_json()["reason_code"] == INVALID_OPERATION_FOR_STATE.code


def test_import_meta_running_item_returns_invalid_operation(client, journal_env):
    media_path = _write_staged_import(
        journal_env,
        "20260101_130004",
        {
            "original_filename": "running-meta.m4a",
            "client_item_id": "running-meta-client",
            "task_id": "task-running",
            "upload_timestamp": import_routes.now_ms(),
        },
    )

    response = client.post(
        "/app/import/api/meta",
        json={"path": str(media_path), "facet": "work"},
    )

    assert response.status_code == INVALID_OPERATION_FOR_STATE.status
    assert response.get_json()["reason_code"] == INVALID_OPERATION_FOR_STATE.code


def test_import_meta_started_item_returns_invalid_operation(client, journal_env):
    media_path = _write_staged_import(
        journal_env,
        "20260101_130002",
        {
            "original_filename": "started.m4a",
            "client_item_id": "started-client",
            "task_id": "task-1",
        },
    )

    response = client.post(
        "/app/import/api/meta",
        json={"path": str(media_path), "facet": "work"},
    )

    assert response.status_code == INVALID_OPERATION_FOR_STATE.status
    assert response.get_json()["reason_code"] == INVALID_OPERATION_FOR_STATE.code


def test_import_start_moves_dir_uses_saved_metadata_and_omits_generic_source(
    client, journal_env, monkeypatch
):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        import_routes,
        "callosum_send",
        lambda tract, event, **payload: (
            emitted.append({"tract": tract, "event": event, **payload}) or True
        ),
    )
    old_ts = "20260101_120000"
    new_ts = "20260101_121500"
    media_path = _write_staged_import(
        journal_env,
        old_ts,
        {
            "original_filename": "sample.m4a",
            "client_item_id": "start-client",
            "facet": "work",
            "setting": "office",
            "source_hint": None,
        },
    )

    response = client.post(
        "/app/import/api/start",
        json={
            "path": str(media_path),
            "timestamp": new_ts,
            "source": "recording",
            "facet": "ignored",
            "force": True,
        },
    )

    assert response.status_code == 200
    new_dir = journal_env / "imports" / new_ts
    assert new_dir.exists()
    assert not (journal_env / "imports" / old_ts).exists()
    metadata = read_import_metadata(journal_env, new_ts)
    assert metadata["file_path"] == str(new_dir / media_path.name)
    assert emitted == [
        {
            "tract": "supervisor",
            "event": "request",
            "ref": response.get_json()["task_id"],
            "cmd": [
                "journal",
                "importer",
                str(new_dir / media_path.name),
                new_ts,
                "--facet",
                "work",
                "--setting",
                "office",
                "--force",
            ],
            "queue_if_active_cmd_differs": True,
        }
    ]
    assert metadata["task_id"] == response.get_json()["task_id"]


def test_import_start_forwards_only_saved_source_hint(client, journal_env, monkeypatch):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        import_routes,
        "callosum_send",
        lambda tract, event, **payload: (
            emitted.append({"tract": tract, "event": event, **payload}) or True
        ),
    )
    ts = "20260101_122000"
    media_path = _write_staged_import(
        journal_env,
        ts,
        {
            "original_filename": "vault",
            "client_item_id": "source-hint-client",
            "source_hint": "obsidian",
        },
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": ts, "source": "quick"},
    )

    assert response.status_code == 200
    assert emitted[0]["cmd"] == [
        "journal",
        "importer",
        str(media_path),
        ts,
        "--source",
        "obsidian",
    ]
    assert emitted[0]["queue_if_active_cmd_differs"] is True
    assert (
        read_import_metadata(journal_env, ts)["task_id"]
        == response.get_json()["task_id"]
    )


def test_import_start_refuses_terminal_duplicate_even_with_force(
    client, journal_env, monkeypatch
):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        import_routes,
        "callosum_send",
        lambda tract, event, **payload: (
            emitted.append({"tract": tract, "event": event, **payload}) or True
        ),
    )
    content = b"terminal"
    source_hash = _sha(content)
    ts = "20260101_123000"
    media_path = _write_staged_import(
        journal_env,
        ts,
        {
            "original_filename": "terminal.m4a",
            "client_item_id": "terminal-client",
            "source_hash": source_hash,
        },
    )
    _write_manifest(
        journal_env,
        import_id="20260101_124500",
        source_hash=source_hash,
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": ts, "force": True},
    )

    assert response.status_code == INVALID_OPERATION_FOR_STATE.status
    assert response.get_json()["reason_code"] == INVALID_OPERATION_FOR_STATE.code
    assert emitted == []


def test_import_start_send_failure_returns_non_2xx_without_task_id(
    client, journal_env, monkeypatch
):
    """Covers a silent-drop send failure mode, not the observed field defect."""
    monkeypatch.setattr(import_routes, "callosum_send", lambda *args, **kwargs: False)
    ts = "20260101_123100"
    media_path = _write_staged_import(
        journal_env,
        ts,
        {
            "original_filename": "send-failed.m4a",
            "client_item_id": "send-failed-client",
        },
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": ts},
    )

    assert response.status_code == IMPORT_QUEUE_UNREACHABLE.status
    body = response.get_json()
    assert body["reason_code"] == IMPORT_QUEUE_UNREACHABLE.code
    assert (
        body["detail"]
        == "your journal's background service isn't running. start it, then try again."
    )
    assert "task_id" not in read_import_metadata(journal_env, ts)


def test_import_start_send_none_is_failure(client, journal_env, monkeypatch):
    monkeypatch.setattr(import_routes, "callosum_send", lambda *args, **kwargs: None)
    ts = "20260101_123101"
    media_path = _write_staged_import(
        journal_env,
        ts,
        {
            "original_filename": "send-none.m4a",
            "client_item_id": "send-none-client",
        },
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": ts},
    )

    assert response.status_code == IMPORT_QUEUE_UNREACHABLE.status
    assert response.get_json()["reason_code"] == IMPORT_QUEUE_UNREACHABLE.code
    assert "task_id" not in read_import_metadata(journal_env, ts)


def test_import_start_persist_failure_after_send_names_running_task(
    client, journal_env, monkeypatch
):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        import_routes,
        "callosum_send",
        lambda tract, event, **payload: (
            emitted.append({"tract": tract, "event": event, **payload}) or True
        ),
    )

    def _fail_metadata_update(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        import_routes, "update_import_metadata_fields", _fail_metadata_update
    )
    ts = "20260101_123102"
    media_path = _write_staged_import(
        journal_env,
        ts,
        {
            "original_filename": "persist-failed.m4a",
            "client_item_id": "persist-failed-client",
        },
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": ts},
    )

    assert response.status_code == IMPORT_METADATA_FAILED.status
    body = response.get_json()
    assert body["reason_code"] == IMPORT_METADATA_FAILED.code
    assert body["task_id"] == emitted[0]["ref"]
    assert body["task_id"] in body["detail"]


def test_import_start_missing_source_returns_import_not_found(client, journal_env):
    old_ts = "20260101_120000"
    new_ts = "20260101_121500"
    missing_path = journal_env / "imports" / old_ts / "sample.m4a"

    response = client.post(
        "/app/import/api/start",
        json={"path": str(missing_path), "timestamp": new_ts},
    )

    assert response.status_code == IMPORT_NOT_FOUND.status
    body = response.get_json()
    assert body["reason_code"] == IMPORT_NOT_FOUND.code


def test_import_start_target_exists_returns_import_conflict(client, journal_env):
    old_ts = "20260101_120000"
    new_ts = "20260101_121500"
    old_dir = journal_env / "imports" / old_ts
    new_dir = journal_env / "imports" / new_ts
    old_dir.mkdir()
    new_dir.mkdir()
    media_path = old_dir / "sample.m4a"
    media_path.write_bytes(b"sample")
    write_import_metadata(
        journal_env,
        old_ts,
        {
            "file_path": str(media_path),
            "user_timestamp": old_ts,
            "client_item_id": "conflict-client",
        },
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": new_ts},
    )

    assert response.status_code == IMPORT_CONFLICT.status
    body = response.get_json()
    assert body["reason_code"] == IMPORT_CONFLICT.code

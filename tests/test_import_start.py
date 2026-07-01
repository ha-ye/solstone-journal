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
    IMPORT_NOT_FOUND,
    INVALID_OPERATION_FOR_STATE,
    MISSING_REQUIRED_FIELD,
)
from solstone.think.importers.utils import (
    read_import_metadata,
    update_import_metadata_fields,
    write_import_metadata,
)

import_routes = __import__("solstone.apps.import.routes", fromlist=["routes"])


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


def test_import_save_duplicate_staged_content_is_terminal(client, journal_env):
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
        client_item_id="ios-staged-dup",
        content=content,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "duplicate"
    assert body["client_item_id"] == "ios-staged-dup"
    assert body["recommended_action"] == "do_not_start"
    assert body["duplicate"] == {
        "import_id": "20260101_121500",
        "imported_at": None,
        "entry_count": None,
        "state": "staged",
    }
    assert _import_dirs(journal_env) == before_dirs


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
        "emit",
        lambda tract, event, **payload: emitted.append(
            {"tract": tract, "event": event, **payload}
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
        }
    ]


def test_import_start_forwards_only_saved_source_hint(client, journal_env, monkeypatch):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        import_routes,
        "emit",
        lambda tract, event, **payload: emitted.append(
            {"tract": tract, "event": event, **payload}
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


def test_import_start_refuses_terminal_duplicate_even_with_force(
    client, journal_env, monkeypatch
):
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        import_routes,
        "emit",
        lambda tract, event, **payload: emitted.append(
            {"tract": tract, "event": event, **payload}
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

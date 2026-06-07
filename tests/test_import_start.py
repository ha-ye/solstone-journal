# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from flask import Flask

import solstone.convey.state as convey_state
import solstone.think.utils as think_utils
from solstone.convey.reasons import IMPORT_CONFLICT, IMPORT_NOT_FOUND

import_routes = import_module("solstone.apps.import.routes")


@pytest.fixture
def journal_env(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(convey_state, "journal_root", str(tmp_path), raising=False)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None
    (tmp_path / "imports").mkdir()
    return tmp_path


@pytest.fixture
def client(journal_env):
    app = Flask(__name__)
    app.register_blueprint(import_routes.import_bp)
    return app.test_client()


def test_import_start_moves_staging_dir_and_updates_file_path(client, journal_env):
    old_ts = "20260101_120000"
    new_ts = "20260101_121500"
    import_dir = journal_env / "imports" / old_ts
    import_dir.mkdir()
    media_path = import_dir / "sample.m4a"
    media_path.write_bytes(b"sample")
    (import_dir / "import.json").write_text(
        json.dumps({"facet": "work"}), encoding="utf-8"
    )

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": new_ts},
    )

    assert response.status_code == 200
    assert response.get_json()["task_id"]
    new_dir = journal_env / "imports" / new_ts
    assert new_dir.exists()
    assert not import_dir.exists()
    metadata = json.loads((new_dir / "import.json").read_text(encoding="utf-8"))
    assert metadata["file_path"] == str(new_dir / media_path.name)


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

    response = client.post(
        "/app/import/api/start",
        json={"path": str(media_path), "timestamp": new_ts},
    )

    assert response.status_code == IMPORT_CONFLICT.status
    body = response.get_json()
    assert body["reason_code"] == IMPORT_CONFLICT.code

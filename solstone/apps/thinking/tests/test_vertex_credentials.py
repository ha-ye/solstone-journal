# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json

import pytest

from solstone.apps.thinking.vertex_credentials import (
    delete_vertex_credentials,
    save_vertex_credentials,
)


def _valid_creds(**overrides):
    creds = {
        "type": "service_account",
        "project_id": "test-project",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
    }
    creds.update(overrides)
    return creds


def test_save_writes_canonical_bytes_and_mode(tmp_path):
    journal_root = tmp_path / "journal"
    creds = _valid_creds(project_id="test-projéct")

    creds_file = save_vertex_credentials(creds, journal_root)

    assert creds_file == journal_root / ".config" / "vertex-credentials.json"
    assert creds_file.exists()
    assert creds_file.stat().st_mode & 0o777 == 0o600
    expected = (json.dumps(creds, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert creds_file.read_bytes() == expected


def test_save_atomic_failure_preserves_existing_bytes(tmp_path, monkeypatch):
    journal_root = tmp_path / "journal"
    creds_file = save_vertex_credentials(_valid_creds(project_id="v1"), journal_root)
    v1_bytes = creds_file.read_bytes()

    def fail_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", fail_replace)

    with pytest.raises(OSError):
        save_vertex_credentials(_valid_creds(project_id="v2"), journal_root)

    assert creds_file.read_bytes() == v1_bytes
    assert list((journal_root / ".config").glob(".tmp_*")) == []


def test_delete_refuses_noncanonical_path(tmp_path):
    journal_root = tmp_path / "journal"
    noncanonical = journal_root / "elsewhere.json"
    noncanonical.parent.mkdir(parents=True, exist_ok=True)
    noncanonical.write_text("secret", encoding="utf-8")

    assert delete_vertex_credentials(noncanonical, journal_root) is False
    assert noncanonical.exists()


def test_delete_removes_canonical(tmp_path):
    journal_root = tmp_path / "journal"
    creds_file = save_vertex_credentials(_valid_creds(), journal_root)

    assert delete_vertex_credentials(str(creds_file), journal_root) is True
    assert not creds_file.exists()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.think.journal_config import read_journal_config
from solstone.think.models import LOCAL_FLASH
from solstone.think.providers import local_install
from solstone.think.providers.install_state import read_install_status
from solstone.think.providers.local import LOCAL_MODEL_SPECS


def _init_journal(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"providers": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _local_status() -> dict:
    return read_install_status(scope="bundled", name="local")


def _local_slot() -> dict:
    return read_journal_config()["providers"]["bundled"]["local"]


def test_install_llama_server_writes_canonical_sequence(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    pin = {
        "release_tag": "v1",
        "filename": "llama.tar.gz",
        "sha256": "abc123",
        "binary_name": "llama-server",
    }
    final_path = local_install.binary_path_for_pin("test-platform", pin)
    final_path.parent.mkdir(parents=True)
    final_path.write_text("binary", encoding="utf-8")
    observed: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)

    def fake_download(_url, _dest, **_kwargs):
        observed.append(
            ("download", _local_status()["install_state"], dict(_local_slot()))
        )

    def fake_verify(_path, _expected):
        observed.append(
            ("verify", _local_status()["install_state"], dict(_local_slot()))
        )

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", fake_verify)
    monkeypatch.setattr(
        local_install, "_safe_extract_tarball", lambda _tarball, _dest: None
    )
    monkeypatch.setattr(
        local_install, "_find_extracted_binary", lambda _dest, _name: final_path
    )
    monkeypatch.setattr(local_install, "_chmod_executable", lambda _path: None)
    monkeypatch.setattr(local_install, "_clear_macos_quarantine", lambda _path: None)

    result = local_install.install_llama_server()

    assert [entry[0] for entry in observed] == ["download", "verify"]
    assert observed[0][1] == "downloading"
    assert observed[0][2]["binary_artifact"] == "llama.tar.gz"
    assert observed[1][1] == "verifying"
    assert result["install_state"] == "installed"
    slot = _local_slot()
    assert slot["install_state"] == "installed"
    assert slot["binary_artifact"] == "llama.tar.gz"
    assert slot["binary_sha256"] == "abc123"
    assert slot["binary_path"] == str(final_path)
    assert "state" not in slot


def test_install_model_writes_canonical_sequence(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    spec = LOCAL_MODEL_SPECS[LOCAL_FLASH]
    observed: list[tuple[str, str, dict]] = []

    def fake_download(_url, _dest, **_kwargs):
        observed.append(
            ("download", _local_status()["install_state"], dict(_local_slot()))
        )

    def fake_verify(_path, _expected):
        observed.append(
            ("verify", _local_status()["install_state"], dict(_local_slot()))
        )

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", fake_verify)

    result = local_install.install_model(LOCAL_FLASH)

    assert [entry[0] for entry in observed] == ["download", "verify"]
    assert observed[0][1] == "downloading"
    assert observed[0][2]["model_id"] == LOCAL_FLASH
    assert observed[1][1] == "verifying"
    assert result["install_state"] == "installed"
    slot = _local_slot()
    assert slot["install_state"] == "installed"
    assert slot["model_id"] == LOCAL_FLASH
    assert slot["model_path"] == str(local_install.model_path(spec.model_id))
    assert slot["model_sha256"] == spec.sha256
    assert "state" not in slot


def test_install_llama_server_failure_writes_canonical_failed(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    pin = {
        "release_tag": "v1",
        "filename": "llama.tar.gz",
        "sha256": "abc123",
        "binary_name": "llama-server",
    }
    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)

    def fake_download(_url, _dest, **_kwargs):
        raise RuntimeError("network broke")

    monkeypatch.setattr(local_install, "_download_file", fake_download)

    with pytest.raises(RuntimeError, match="network broke"):
        local_install.install_llama_server()

    status = _local_status()
    assert status["install_state"] == "failed"
    assert status["install_error"] == "network broke"
    slot = _local_slot()
    assert slot["install_state"] == "failed"
    assert slot["install_error"] == "network broke"
    assert "state" not in slot

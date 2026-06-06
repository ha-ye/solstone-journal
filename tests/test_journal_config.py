# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from solstone.think.journal_config import write_journal_config


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def test_write_journal_config_crash_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = _config_path(tmp_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"OLD\n")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", boom)

    with pytest.raises(OSError):
        write_journal_config({"identity": {"name": "New"}})

    assert config_path.read_bytes() == b"OLD\n"
    assert list(config_path.parent.glob(".tmp_*")) == []


def test_write_journal_config_fsyncs_temp_and_parent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.fsync", spy)

    write_journal_config({"identity": {"name": "Durable"}})

    assert len(calls) >= 2


def test_write_journal_config_file_mode_is_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    write_journal_config({"identity": {"name": "Private"}})

    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_write_journal_config_applies_mode_before_replace(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    captured = {}
    real_replace = os.replace

    def spy(src, dst):
        captured["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", spy)

    write_journal_config({"identity": {"name": "Private"}})

    assert captured["mode"] == 0o600
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_write_journal_config_serializes_utf8_without_ascii_escapes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config = {"identity": {"name": "Renée"}}

    write_journal_config(config)

    actual = _config_path(tmp_path).read_bytes()
    expected = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert actual == expected
    assert "Renée".encode("utf-8") in actual
    assert b"Ren\\u00e9e" not in actual

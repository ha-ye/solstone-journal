# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from solstone.think.link import join_cli


def _files() -> dict[str, bytes]:
    return {name: f"{name}\n".encode("utf-8") for name in sorted(join_cli.BUNDLE_FILES)}


@pytest.mark.parametrize("fail_index", range(len(join_cli.BUNDLE_FILES)))
def test_atomic_publish_file_failure_leaves_no_final_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_index: int,
) -> None:
    files = _files()
    final = tmp_path / "bundle"
    real_write = join_cli._write_bundle_file
    writes = 0

    def fail_at_position(path: Path, content: bytes) -> None:
        nonlocal writes
        if writes == fail_index:
            raise OSError(f"fail write {path.name}")
        writes += 1
        real_write(path, content)

    monkeypatch.setattr(join_cli, "_write_bundle_file", fail_at_position)

    with pytest.raises(OSError, match="fail write"):
        join_cli._publish_bundle_atomic(final, files)

    assert not os.path.lexists(final)
    assert list(tmp_path.iterdir()) == []


def test_atomic_publish_rename_failure_leaves_no_final_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = tmp_path / "bundle"

    def fail_rename(_src: object, _dst: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(join_cli.os, "rename", fail_rename)

    with pytest.raises(OSError, match="rename failed"):
        join_cli._publish_bundle_atomic(final, _files())

    assert not os.path.lexists(final)
    assert list(tmp_path.iterdir()) == []


def test_atomic_publish_non_oserror_failure_leaves_no_final_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final = tmp_path / "bundle"

    def fail_write(_path: Path, _content: bytes) -> None:
        raise RuntimeError("unexpected write failure")

    monkeypatch.setattr(join_cli, "_write_bundle_file", fail_write)

    with pytest.raises(RuntimeError, match="unexpected write failure"):
        join_cli._publish_bundle_atomic(final, _files())

    assert not os.path.lexists(final)
    assert list(tmp_path.iterdir()) == []


def test_atomic_publish_success_sets_directory_and_file_modes(tmp_path: Path) -> None:
    final = tmp_path / "bundle"

    join_cli._publish_bundle_atomic(final, _files())

    assert stat.S_IMODE(final.stat().st_mode) == 0o700
    assert sorted(path.name for path in final.iterdir()) == sorted(
        join_cli.BUNDLE_FILES
    )
    for path in final.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

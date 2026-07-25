# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._repo_inventory import (
    EXCLUDED_RUNTIME_DIR_NAMES,
    assert_inventory_unchanged,
    format_inventory_diff,
    repository_inventory,
)

DEPTHS = ("root", "nested")
EXCLUDED_NAMES = tuple(sorted(EXCLUDED_RUNTIME_DIR_NAMES))
SIMILAR_NAMES = ("not__pycache__data", "__pycache__.bak", ".venvish", ".gitignore")


def _subject_path(root: Path, name: str, depth: str) -> Path:
    if depth == "root":
        return root / name
    parent = root / "ordinary" / "nested"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / name


def _assert_diff_mentions(diff: str, rel_path: str, *fields: str) -> None:
    assert rel_path in diff
    for field in fields:
        assert f"{field}:" in diff


@pytest.mark.parametrize("cache_name", EXCLUDED_NAMES)
@pytest.mark.parametrize("depth", DEPTHS)
@pytest.mark.parametrize("operation", ("create", "mutate", "remove"))
def test_excluded_cache_file_lifecycle_is_ignored(
    tmp_path: Path,
    cache_name: str,
    depth: str,
    operation: str,
) -> None:
    cache_dir = _subject_path(tmp_path, cache_name, depth)
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "entry.pyc"
    if operation in {"mutate", "remove"}:
        cache_file.write_bytes(b"before\n")

    before = repository_inventory(tmp_path)
    if operation == "create":
        cache_file.write_bytes(b"after\n")
    elif operation == "mutate":
        cache_file.write_bytes(b"after changed\n")
    else:
        cache_file.unlink()
    after = repository_inventory(tmp_path)

    assert_inventory_unchanged(before, after)


@pytest.mark.parametrize("cache_name", EXCLUDED_NAMES)
@pytest.mark.parametrize("depth", DEPTHS)
def test_excluded_cache_directory_first_creation_is_ignored(
    tmp_path: Path,
    cache_name: str,
    depth: str,
) -> None:
    cache_dir = _subject_path(tmp_path, cache_name, depth)
    assert not cache_dir.exists()

    before = repository_inventory(tmp_path)
    cache_dir.mkdir(parents=True)
    (cache_dir / "entry.pyc").write_bytes(b"created with cache dir\n")
    after = repository_inventory(tmp_path)

    assert_inventory_unchanged(before, after)


@pytest.mark.parametrize("name", EXCLUDED_NAMES)
def test_excluded_basename_regular_file_stays_visible(
    tmp_path: Path,
    name: str,
) -> None:
    path = tmp_path / name
    path.write_text("before\n", encoding="utf-8")

    before = repository_inventory(tmp_path)
    assert before[name].kind == "file"
    path.write_text("after changed\n", encoding="utf-8")
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, name, "size")


@pytest.mark.parametrize("name", EXCLUDED_NAMES)
def test_excluded_basename_symlink_stays_visible(
    tmp_path: Path,
    name: str,
) -> None:
    path = tmp_path / name
    os.symlink("target-a", path)

    before = repository_inventory(tmp_path)
    path.unlink()
    os.symlink("target-b", path)
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, name, "target")


@pytest.mark.parametrize("name", EXCLUDED_NAMES)
def test_excluded_basename_fifo_stays_visible(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    os.mkfifo(path)

    before = repository_inventory(tmp_path)
    current_mode = path.lstat().st_mode & 0o777
    path.chmod(0o600 if current_mode != 0o600 else 0o644)
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, name, "mode")


@pytest.mark.parametrize("name", EXCLUDED_NAMES)
def test_excluded_basename_symlink_is_not_followed(
    tmp_path: Path,
    name: str,
) -> None:
    root = tmp_path / "root"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    (external / "external.txt").write_text("outside\n", encoding="utf-8")
    os.symlink(external, root / name)

    inventory = repository_inventory(root)

    assert inventory[name].kind == "symlink"
    assert all(not path.startswith(f"{name}/") for path in inventory)


@pytest.mark.parametrize("name", SIMILAR_NAMES)
def test_similar_but_different_names_stay_visible(
    tmp_path: Path,
    name: str,
) -> None:
    visible_dir = tmp_path / name
    visible_dir.mkdir()
    child = visible_dir / "child.txt"
    child.write_text("before\n", encoding="utf-8")

    before = repository_inventory(tmp_path)
    assert name in before
    assert f"{name}/child.txt" in before
    child.write_text("after changed\n", encoding="utf-8")
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, f"{name}/child.txt", "size")


@pytest.mark.parametrize("operation", ("create", "mutate", "remove"))
def test_ordinary_file_writes_are_reported(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / "ordinary.txt"
    if operation in {"mutate", "remove"}:
        path.write_text("before\n", encoding="utf-8")

    before = repository_inventory(tmp_path)
    if operation == "create":
        path.write_text("created\n", encoding="utf-8")
    elif operation == "mutate":
        path.write_text("after changed\n", encoding="utf-8")
    else:
        path.unlink()
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    assert "ordinary.txt" in diff
    assert {"create": "added:", "mutate": "changed:", "remove": "removed:"}[
        operation
    ] in diff


def test_same_length_rewrite_with_mtime_restored_is_reported_by_ctime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-length.txt"
    path.write_text("aaaa\n", encoding="utf-8")
    original_stat = path.stat()
    before = repository_inventory(tmp_path)

    path.write_text("bbbb\n", encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, "same-length.txt", "ctime_ns")


def test_regular_file_chmod_only_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "mode.txt"
    path.write_text("content\n", encoding="utf-8")

    before = repository_inventory(tmp_path)
    path.chmod(0o600)
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, "mode.txt", "mode")


def test_real_directory_chmod_only_is_reported_without_directory_timestamps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "directory"
    path.mkdir()

    before = repository_inventory(tmp_path)
    path.chmod(0o700)
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, "directory", "mode")
    assert "mtime_ns" not in diff
    assert "ctime_ns" not in diff


@pytest.mark.parametrize("operation", ("create", "remove"))
def test_empty_non_cache_directory_create_and_remove_are_reported(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / "empty-dir"
    if operation == "remove":
        path.mkdir()

    before = repository_inventory(tmp_path)
    if operation == "create":
        path.mkdir()
    else:
        path.rmdir()
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    assert "empty-dir" in diff
    assert {"create": "added:", "remove": "removed:"}[operation] in diff


def test_special_entry_appearance_is_reported(tmp_path: Path) -> None:
    before = repository_inventory(tmp_path)
    os.mkfifo(tmp_path / "ordinary-fifo")
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    assert "ordinary-fifo" in diff
    assert "kind='fifo'" in diff


def test_symlink_target_replacement_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "ordinary-link"
    os.symlink("target-a", path)

    before = repository_inventory(tmp_path)
    path.unlink()
    os.symlink("target-b", path)
    after = repository_inventory(tmp_path)

    diff = format_inventory_diff(before, after)
    _assert_diff_mentions(diff, "ordinary-link", "target")


def test_root_itself_is_not_inventoried(tmp_path: Path) -> None:
    assert "" not in repository_inventory(tmp_path)
    assert "." not in repository_inventory(tmp_path)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Repository inventory assertions for no-write tests.

The oracle proves that a code path did not create, remove, or mutate ordinary
repository entries while it ran.  It deliberately does not prove anything about
runtime cache directories that sibling test workers may create as a side effect
of imports or test tooling, and it does not treat directory size or timestamps
as meaningful evidence.

Traversal is non-following throughout.  There is no OSError tolerance branch:
excluded real directories are pruned immediately after the DirEntry type check,
with no later syscall, and every entry that is not pruned is inventoried through
ordinary lstat/readlink operations that fail loudly if the entry races.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypeAlias

# Exactly these real directory basenames are ignored. Matching regular files,
# symlinks, and special entries remain inventoried.
# - .git: VCS metadata directories are not repository content under test and may
#   be managed outside the Python harness.
# - .venv: local dependency environments contain import-time/runtime caches.
# - .pytest_cache: pytest rewrites its own cache during normal test execution.
# - __pycache__: sibling workers create Python bytecode caches while importing.
# - .hypothesis: Hypothesis defaults its home to Path.cwd() / ".hypothesis";
#   tests/test_openapi_schemathesis.py has 6 unmarked default-selected tests
#   that rewrite .hypothesis/unicode_data/15.1.0/codec-utf-8.json.gz on every
#   run, which is otherwise a regular-file change at repo root.
EXCLUDED_RUNTIME_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".venv", ".pytest_cache", "__pycache__", ".hypothesis"}
)


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    kind: str
    dev: int
    ino: int
    mode: int
    size: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    target: str | None = None
    rdev: int | None = None


RepoInventory: TypeAlias = dict[str, InventoryRecord]


def repository_inventory(root: Path) -> RepoInventory:
    inventory: RepoInventory = {}

    def walk(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                entry_path = Path(entry.path)
                is_symlink = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                if (
                    entry.name in EXCLUDED_RUNTIME_DIR_NAMES
                    and is_dir
                    # Intentionally redundant: spell out the prune rule as
                    # real, non-symlink directories only.
                    and not is_symlink
                ):
                    continue

                record = _record_entry(entry, entry_path)
                inventory[entry_path.relative_to(root).as_posix()] = record
                if record.kind == "directory":
                    walk(entry_path)

    walk(root)
    return inventory


def format_inventory_diff(before: RepoInventory, after: RepoInventory) -> str:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        path for path in set(before) & set(after) if before[path] != after[path]
    )

    if not added and not removed and not changed:
        return ""

    lines = ["repository inventory changed"]
    if added:
        lines.append("added:")
        lines.extend(f"  + {path}: {_format_record(after[path])}" for path in added)
    if removed:
        lines.append("removed:")
        lines.extend(f"  - {path}: {_format_record(before[path])}" for path in removed)
    if changed:
        lines.append("changed:")
        for path in changed:
            lines.append(f"  * {path}")
            lines.extend(_format_field_changes(before[path], after[path]))
    return "\n".join(lines)


def assert_inventory_unchanged(before: RepoInventory, after: RepoInventory) -> None:
    diff = format_inventory_diff(before, after)
    if diff:
        raise AssertionError(diff)


def _record_entry(entry: os.DirEntry[str], path: Path) -> InventoryRecord:
    entry_stat = entry.stat(follow_symlinks=False)
    mode = stat.S_IMODE(entry_stat.st_mode)
    kind = _kind(entry_stat.st_mode)

    if kind == "directory":
        return InventoryRecord(
            kind=kind,
            dev=entry_stat.st_dev,
            ino=entry_stat.st_ino,
            mode=mode,
        )
    if kind == "symlink":
        return InventoryRecord(
            kind=kind,
            dev=entry_stat.st_dev,
            ino=entry_stat.st_ino,
            mode=mode,
            target=os.readlink(path),
        )
    if kind == "file":
        return InventoryRecord(
            kind=kind,
            dev=entry_stat.st_dev,
            ino=entry_stat.st_ino,
            mode=mode,
            size=entry_stat.st_size,
            mtime_ns=entry_stat.st_mtime_ns,
            ctime_ns=entry_stat.st_ctime_ns,
        )
    return InventoryRecord(
        kind=kind,
        dev=entry_stat.st_dev,
        ino=entry_stat.st_ino,
        mode=mode,
        rdev=entry_stat.st_rdev if kind in {"block_device", "char_device"} else None,
    )


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "special"


def _format_record(record: InventoryRecord) -> str:
    parts = [
        f"{field.name}={_format_value(getattr(record, field.name), field.name)}"
        for field in fields(record)
        if getattr(record, field.name) is not None
    ]
    return " ".join(parts)


def _format_field_changes(before: InventoryRecord, after: InventoryRecord) -> list[str]:
    changes = []
    for field in fields(before):
        before_value = getattr(before, field.name)
        after_value = getattr(after, field.name)
        if before_value != after_value:
            changes.append(
                "    "
                f"{field.name}: {_format_value(before_value, field.name)} -> "
                f"{_format_value(after_value, field.name)}"
            )
    return changes


def _format_value(value: object, field_name: str) -> str:
    if field_name == "mode" and isinstance(value, int):
        return oct(value)
    if isinstance(value, str):
        return repr(value)
    return str(value)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from fnmatch import fnmatchcase

import pytest

from solstone.think.backup.engine import BACKUP_EXCLUDES


def _restic_excludes(pattern: str, rel_path: str) -> bool:
    path_parts = tuple(part for part in rel_path.strip("/").split("/") if part)
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in path_parts)

    anchored = pattern.startswith("/")
    pattern_parts = tuple(part for part in pattern.strip("/").split("/") if part)
    starts = (0,) if anchored else range(len(path_parts))
    for start in starts:
        for end in range(start, len(path_parts) + 1):
            if _match_components(pattern_parts, path_parts[start:end]):
                return True
    return False


def _match_components(
    pattern_parts: tuple[str, ...], path_parts: tuple[str, ...]
) -> bool:
    if not pattern_parts:
        return not path_parts
    head, *tail = pattern_parts
    rest = tuple(tail)
    if head == "**":
        return _match_components(rest, path_parts) or (
            bool(path_parts) and _match_components(pattern_parts, path_parts[1:])
        )
    return (
        bool(path_parts)
        and fnmatchcase(path_parts[0], head)
        and _match_components(rest, path_parts[1:])
    )


@pytest.mark.parametrize(
    ("pattern", "rel_path", "expected"),
    [
        ("health", "health/offload/20260101.jsonl", True),
        ("*.jsonl", "health/offload/20260101.jsonl", True),
        ("health/*.jsonl", "health/offload/20260101.jsonl", False),
        ("offload/*.jsonl", "health/offload/20260101.jsonl", True),
        ("/health/offload", "health/offload/20260101.jsonl", True),
        ("/offload/*.jsonl", "health/offload/20260101.jsonl", False),
        ("health/**/20260101.jsonl", "health/offload/20260101.jsonl", True),
    ],
)
def test_restic_exclude_component_model(
    pattern: str, rel_path: str, expected: bool
) -> None:
    assert _restic_excludes(pattern, rel_path) is expected


def test_offload_ledger_survives_backup_excludes() -> None:
    # engine.py:58-62 documents why this exists: a bare "health" pattern
    # previously matched health/ content by basename at every depth.
    ledger_rel_path = "health/offload/20260101.jsonl"

    assert [
        pattern
        for pattern in BACKUP_EXCLUDES
        if _restic_excludes(pattern, ledger_rel_path)
    ] == []
    assert _restic_excludes("health", ledger_rel_path)

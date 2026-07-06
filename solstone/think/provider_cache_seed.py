# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Best-effort provider cache seeding from sibling git worktrees.

Hardlinks every regular file from a sibling checkout's populated
``<checkout>/journal/cache/providers/`` tree into this checkout's
``<journal>/cache/providers/`` tree before ``journal install-models`` downloads
anything. Seeding never aborts install: any unmet precondition or error logs a
reason and falls through to normal download.
"""

from __future__ import annotations

import errno
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from solstone.think.utils import get_journal, get_project_root, is_packaged_install


@dataclass(frozen=True)
class SeedResult:
    seeded: bool
    file_count: int
    byte_count: int
    source: Path | None
    reason: str  # "seeded" | "packaged" | "local-populated" | "no-source" | "source-empty" | "cross-device" | "error"


def seed_provider_cache() -> SeedResult:
    try:
        return _seed_provider_cache()
    except Exception as exc:
        print(f"provider cache: seed skipped (unexpected error: {exc})")
        return SeedResult(False, 0, 0, None, "error")


def _seed_provider_cache() -> SeedResult:
    local_cache = Path(get_journal()) / "cache" / "providers"
    if _has_regular_file(local_cache):
        print("provider cache: local cache already populated, skipping seed")
        return SeedResult(False, 0, 0, None, "local-populated")

    source_cache, reason = _resolve_source_cache()
    if source_cache is None:
        return SeedResult(False, 0, 0, None, reason)

    try:
        count, total = _hardlink_tree(source_cache, local_cache)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            print(
                f"provider cache: seed source {source_cache} is on a different "
                "filesystem, will download"
            )
            return SeedResult(False, 0, 0, source_cache, "cross-device")
        raise

    print(
        f"provider cache: seeded {count} files ({_human_bytes(total)}) "
        f"from {source_cache} via hardlink"
    )
    return SeedResult(True, count, total, source_cache, "seeded")


def _resolve_source_cache() -> tuple[Path | None, str]:
    override = os.environ.get("SOLSTONE_PROVIDER_CACHE_SEED_SOURCE")
    if override:
        cache = Path(override) / "journal" / "cache" / "providers"
        if _has_regular_file(cache):
            return cache, ""
        print(
            f"provider cache: seed source {override} has no populated provider "
            "cache, will download"
        )
        return None, "source-empty"

    if is_packaged_install():
        print("provider cache: packaged install (no sibling worktrees), skipping seed")
        return None, "packaged"

    for worktree in _sibling_worktrees(get_project_root()):
        cache = worktree / "journal" / "cache" / "providers"
        if _has_regular_file(cache):
            return cache, ""

    print("provider cache: no sibling checkout with a populated cache, will download")
    return None, "no-source"


def _sibling_worktrees(current_root: str) -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", current_root, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    current = Path(current_root).resolve()
    roots: list[Path] = []
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :]).resolve()
            if path != current:
                roots.append(path)
    return roots


def _has_regular_file(root: Path) -> bool:
    if not root.exists():
        return False
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if (Path(dirpath) / filename).is_file():
                return True
    return False


def _hardlink_tree(src_root: Path, dst_root: Path) -> tuple[int, int]:
    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(src_root):
        for filename in filenames:
            src = Path(dirpath) / filename
            if not src.is_file():
                continue
            rel = src.relative_to(src_root)
            dst = dst_root / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.link(src, dst)
            count += 1
            total += src.stat().st_size
    return count, total


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"

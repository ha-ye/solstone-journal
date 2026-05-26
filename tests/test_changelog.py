# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path


def _section(text: str, heading: str, next_pattern: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading) :]
    next_heading = re.search(next_pattern, rest)
    return rest[: next_heading.start()] if next_heading else rest


def test_unreleased_changed_bullet_carries_cogitate_and_mb() -> None:
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = _section(text, "## [Unreleased]", r"\n## \[")
    changed = _section(unreleased, "### Changed", r"\n### ")
    bullets = [
        line
        for line in changed.splitlines()
        if line.startswith("- ") and "cogitate" in line and re.search(r"\d+\s*MB", line)
    ]

    assert bullets

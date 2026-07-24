#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Check native-sol design records are linked from durable docs navigation."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = REPO_ROOT / "docs/design/native-sol-client"
NAV_DOCS = (REPO_ROOT / "docs/PORTING.md",)
LINK_RE = re.compile(r"docs/design/native-sol-client/[A-Za-z0-9_.-]+\.md")


def check_links() -> list[str]:
    design_files = sorted(DESIGN_DIR.glob("*.md"))
    errors: list[str] = []
    if not design_files:
        errors.append(f"{DESIGN_DIR.relative_to(REPO_ROOT)} has no design records")
    expected = {path.relative_to(REPO_ROOT).as_posix() for path in design_files}
    linked: set[str] = set()
    for nav_doc in NAV_DOCS:
        if not nav_doc.is_file():
            errors.append(f"{nav_doc.relative_to(REPO_ROOT)} is missing")
            continue
        linked.update(LINK_RE.findall(nav_doc.read_text(encoding="utf-8")))
    if not linked:
        errors.append("native-sol durable docs link set is empty")
    missing = sorted(expected - linked)
    if missing:
        errors.append(f"native-sol design records not linked: {missing!r}")
    return errors


def main() -> int:
    errors = check_links()
    if errors:
        print("native sol docs link check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("native sol docs links ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

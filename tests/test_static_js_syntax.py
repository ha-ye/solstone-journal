# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Syntax-check every first-party static JS file with ``node --check``.

A doubled-escape apostrophe once shipped a parse error in shell_boot.js that
silently killed all SPA-shell hydration — unit tests can't see browser parse
failures, so this guard runs node's parser over every served script. Vendor
bundles are third-party and excluded.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOTS = [
    REPO_ROOT / "solstone" / "convey" / "static",
    *sorted((REPO_ROOT / "solstone" / "apps").glob("*/static")),
]


def _first_party_js() -> list[Path]:
    files: list[Path] = []
    for root in STATIC_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.js")):
            if "vendor" in path.relative_to(root).parts:
                continue
            files.append(path)
    return files


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_static_js_parses() -> None:
    files = _first_party_js()
    assert files, "expected first-party static JS files to exist"
    failures = []
    for path in files:
        result = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append(f"{path.relative_to(REPO_ROOT)}\n{result.stderr.strip()}")
    assert not failures, "static JS failed to parse:\n" + "\n\n".join(failures)

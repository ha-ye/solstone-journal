# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_access_imports_clean.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_access_imports_clean.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
    )


def test_injected_access_heavy_import_goes_red_and_names_offender() -> None:
    result = _run("--inject-heavy-module", "solstone.think.check")

    assert result.returncode == 1
    assert "sol check --help [solstone.think.check]" in result.stderr
    assert "solstone.think.check" in result.stderr
    assert "numpy" in result.stderr

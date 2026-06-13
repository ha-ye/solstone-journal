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


def test_repo_tree_is_green() -> None:
    result = _run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "access-imports-clean: pass" in result.stdout


def test_injected_access_heavy_import_goes_red_and_names_offender() -> None:
    result = _run("--inject-heavy-module", "solstone.think.notify_cli")

    assert result.returncode == 1
    assert "sol notify --help" in result.stderr
    assert "solstone.think.notify_cli" in result.stderr
    assert "numpy" in result.stderr


def test_injected_mounted_app_failure_goes_red_and_names_app() -> None:
    result = _run("--inject-mounted-app", "import")

    assert result.returncode == 1
    assert "sol call --help" in result.stderr
    assert "import" in result.stderr
    assert "injected mounted app failure: import" in result.stderr

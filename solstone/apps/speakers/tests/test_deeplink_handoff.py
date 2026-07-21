# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
HARNESS = REPO_ROOT / "tests" / "js" / "speakers_deeplink_harness.js"


def test_speakers_deeplink_handoff_mounts_and_opens_from_cached_snapshot() -> None:
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not available")

    result = subprocess.run(
        [node, str(HARNESS), str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr

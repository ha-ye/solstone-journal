# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the vendored Silero VAD module."""

import subprocess
import sys

PROBE = """
import sys
import solstone.observe.vad  # noqa: F401
import solstone.observe.transcribe  # noqa: F401
assert "faster_whisper" not in sys.modules, sorted(m for m in sys.modules if "faster" in m)
sys.stdout.write("ok\\n")
"""


def test_vendored_module_does_not_pull_faster_whisper():
    """This test would fail on pre-L2 main because VAD loaded faster_whisper.vad at module load time."""
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "ok" in result.stdout

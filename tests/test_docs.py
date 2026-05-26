# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from pathlib import Path


def test_install_md_has_no_ollama_references() -> None:
    text = Path("INSTALL.md").read_text(encoding="utf-8")

    assert "ollama" not in text.lower()
    assert (
        "cogitate (sol's tool-calling agent loop, used by chat/digest/"
        "morning_briefing/etc.) works out of the box as soon as you set a "
        "provider key — no extra install step."
    ) in text
    assert "a local model via the local provider" in text


def test_ollama_grep_returns_zero_lines() -> None:
    result = subprocess.run(
        [
            "git",
            "grep",
            "-i",
            "ollama",
            "--",
            ":!tests/",
            ":!docs/design/",
            ":!solstone/apps/settings/maint/_migrate_ollama_to_local.py",
            ":!solstone/apps/settings/call.py",
            ":!CHANGELOG.md",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""

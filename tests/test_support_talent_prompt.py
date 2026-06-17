# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_support_talent_prompt_is_draft_only() -> None:
    prompt_path = REPO_ROOT / "solstone/apps/support/talent/support.md"
    skill_path = REPO_ROOT / "solstone/apps/support/talent/support/SKILL.md"

    text = prompt_path.read_text()
    skill_text = skill_path.read_text()
    lower_text = text.lower()

    # The bare flag substring does not match the required reply flag `--no-submit`.
    assert "--submit" not in text
    assert "--no-submit" in text
    assert "sol call support attach <id> <file> --no-submit" in text
    assert "sol call support attach 42 screenshot.png --no-submit" in skill_text
    assert "send-approval" not in lower_text
    assert "per-send owner approval" not in lower_text
    assert "gate denial" not in lower_text
    assert "--submit" not in skill_text

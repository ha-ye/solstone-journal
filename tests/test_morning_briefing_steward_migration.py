# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from pathlib import Path


def _briefing_prompt() -> str:
    return Path("solstone/talent/morning_briefing.md").read_text(encoding="utf-8")


def _briefing_metadata() -> dict:
    text = _briefing_prompt()
    metadata, end = json.JSONDecoder().raw_decode(text)
    assert isinstance(metadata, dict)
    assert text[end:].startswith("\n\n")
    return metadata


def test_morning_briefing_is_generate_with_pre_hook():
    metadata = _briefing_metadata()

    assert metadata["type"] == "generate"
    assert metadata["output"] == "md"
    assert metadata["schedule"] == "daily"
    assert metadata["hook"]["pre"] == "morning_briefing"
    assert "read_scope" not in metadata


def test_morning_briefing_prompt_uses_injected_packet_only():
    prompt = _briefing_prompt()

    assert "$health_surface" in prompt
    assert "gaps: $source_gaps" in prompt
    assert "$coverage_preamble" in prompt
    assert "Steward Health Surface" in prompt

    assert "sol call" not in prompt
    assert "read_file" not in prompt
    assert "emit_final" not in prompt
    assert "FinishTool" not in prompt

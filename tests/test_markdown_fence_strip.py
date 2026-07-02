# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import logging
from pathlib import Path

import frontmatter

from solstone.think.talents import _strip_outer_markdown_fence
from tests.test_output_hooks import copy_day, run_generator_with_config

LIVE_MARKDOWN = "---\ntype: morning_briefing\ndate: 2026-07-01\n---\n\n<body>\n"
WRAPPED_LIVE_MARKDOWN = f"```\n{LIVE_MARKDOWN}```"
FENCE_STRIP_LOG = "Stripped whole-output markdown fence"


def test_bare_fence_wrap_strips_to_valid_frontmatter():
    result, stripped = _strip_outer_markdown_fence(WRAPPED_LIVE_MARKDOWN)

    assert frontmatter.loads(WRAPPED_LIVE_MARKDOWN).metadata == {}
    assert stripped is True
    metadata = frontmatter.loads(result).metadata
    assert metadata["type"] == "morning_briefing"
    assert metadata["date"]


def test_markdown_labeled_fence_wrap_strips():
    result, stripped = _strip_outer_markdown_fence(f"```markdown\n{LIVE_MARKDOWN}```")

    assert stripped is True
    metadata = frontmatter.loads(result).metadata
    assert metadata["type"] == "morning_briefing"
    assert metadata["date"]


def test_interior_code_block_not_stripped():
    original = (
        "---\n"
        "type: morning_briefing\n"
        "date: 2026-07-01\n"
        "---\n"
        "\n"
        "```python\n"
        "print('hello')\n"
        "```\n"
    )

    result, stripped = _strip_outer_markdown_fence(original)

    assert stripped is False
    assert result == original


def test_unfenced_output_not_stripped():
    original = LIVE_MARKDOWN

    result, stripped = _strip_outer_markdown_fence(original)

    assert stripped is False
    assert result == original


def test_trailing_newline_after_closer_stripped():
    result, stripped = _strip_outer_markdown_fence(f"{WRAPPED_LIVE_MARKDOWN}\n")

    assert stripped is True
    metadata = frontmatter.loads(result).metadata
    assert metadata["type"] == "morning_briefing"
    assert metadata["date"]


def test_three_fence_lines_not_stripped():
    original = (
        "```\n"
        "---\n"
        "type: morning_briefing\n"
        "date: 2026-07-01\n"
        "---\n"
        "\n"
        "```python\n"
        "print('hello')\n"
        "```\n"
        "```"
    )

    result, stripped = _strip_outer_markdown_fence(original)

    assert stripped is False
    assert result == original


def _write_prompt(tmp_path: Path, name: str, output: str) -> None:
    prompt_file = tmp_path / f"{name}.md"
    prompt_file.write_text(
        "{\n"
        '  "type": "generate",\n'
        f'  "title": "{name}",\n'
        '  "schedule": "daily",\n'
        '  "priority": 10,\n'
        f'  "output": "{output}",\n'
        '  "load": {"transcripts": true, "percepts": true}\n'
        "}\n"
        "\n"
        "Test prompt"
    )


def _run_generate_talent(
    tmp_path: Path,
    monkeypatch,
    name: str,
    output: str,
    text: str,
) -> list[dict]:
    mod = importlib.import_module("solstone.think.talents")
    copy_day(tmp_path, monkeypatch)

    import solstone.think.talent as talent

    monkeypatch.setattr(talent, "TALENT_DIR", tmp_path)
    _write_prompt(tmp_path, name, output)

    from solstone.think import models

    monkeypatch.setattr(
        models,
        "generate_with_result",
        lambda *a, **k: {
            "text": text,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    config = {
        "name": name,
        "day": "20240101",
        "output": output,
        "provider": "google",
        "model": "gemini-2.0-flash",
    }

    return run_generator_with_config(mod, config, monkeypatch)


def test_generate_md_strips_fence_and_logs(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="solstone.think.talents"):
        events = _run_generate_talent(
            tmp_path,
            monkeypatch,
            "fence_md_test",
            "md",
            WRAPPED_LIVE_MARKDOWN,
        )

    finish_events = [event for event in events if event["event"] == "finish"]
    assert len(finish_events) == 1
    metadata = frontmatter.loads(finish_events[0]["result"]).metadata
    assert metadata["type"] == "morning_briefing"
    assert metadata["date"]

    strip_logs = [
        record
        for record in caplog.records
        if record.name == "solstone.think.talents"
        and FENCE_STRIP_LOG in record.getMessage()
    ]
    assert len(strip_logs) == 1


def test_generate_json_output_not_stripped(tmp_path, monkeypatch, caplog):
    wrapped_text = '```\n{"type": "morning_briefing"}\n```'

    with caplog.at_level(logging.INFO, logger="solstone.think.talents"):
        events = _run_generate_talent(
            tmp_path,
            monkeypatch,
            "fence_json_test",
            "json",
            wrapped_text,
        )

    finish_events = [event for event in events if event["event"] == "finish"]
    assert len(finish_events) == 1
    assert finish_events[0]["result"] == wrapped_text
    assert all(FENCE_STRIP_LOG not in record.getMessage() for record in caplog.records)

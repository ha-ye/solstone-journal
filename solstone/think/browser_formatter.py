# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Browser stream formatter for indexing and text rendering."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from solstone.think.markdown import _EXTRACTION_BOUND_MARKER, _MAX_EXTRACTION_CHARS

LOG = logging.getLogger(__name__)


def format_browser(
    entries: list[dict],
    context: dict | None = None,
) -> tuple[list[dict], dict]:
    """Format browser JSONL entries to markdown chunks."""
    meta: dict[str, Any] = {"indexer": {"agent": "browser"}}
    chunks: list[dict[str, Any]] = []

    if not entries:
        meta["error"] = "browser stream has no rows"
        return chunks, meta

    budget = _MAX_EXTRACTION_CHARS - len(_EXTRACTION_BOUND_MARKER)
    emitted_chars = 0
    saw_segment_start = False
    stopped = False
    skipped_non_objects = 0

    for row in entries:
        if stopped:
            break
        if not isinstance(row, dict):
            skipped_non_objects += 1
            continue

        kind = str(row.get("t") or "").strip()
        markdown = ""
        if kind == "segment_start":
            saw_segment_start = True
            markdown = _format_snapshot(row)
        elif kind == "delta":
            markdown = _format_delta(row)
        else:
            continue

        if not markdown:
            continue

        bounded, emitted_chars, stopped = _bound_chunk(markdown, emitted_chars, budget)
        chunks.append(
            {
                "timestamp": row["ts"],
                "markdown": bounded,
                "source": row,
            }
        )

    if skipped_non_objects:
        LOG.warning("Skipped %d non-object browser row(s)", skipped_non_objects)
    if not saw_segment_start:
        meta["error"] = "browser stream has no segment_start rows"

    return chunks, meta


def format_browser_text(jsonl_path: Path) -> str:
    """Read a browser JSONL file and return rendered markdown text."""
    entries = _load_jsonl(jsonl_path)
    chunks, _meta = format_browser(entries, {"file_path": jsonl_path})
    return "\n\n".join(chunk["markdown"] for chunk in chunks)


def _format_snapshot(row: dict[str, Any]) -> str:
    title = _clean_text(row.get("title"))
    site = _clean_text(row.get("site"))
    url = _clean_text(row.get("url"))
    heading = title or site or url or "Browser Page"

    lines = [f"## {heading}"]
    subline = " · ".join(
        part for part in (_clean_text(row.get("adapter")), site) if part
    )
    if subline:
        lines.extend(["", subline])

    block_lines = _format_blocks(row.get("blocks"))
    if block_lines:
        lines.extend(["", *block_lines])
    return "\n".join(lines).strip()


def _format_blocks(blocks: Any) -> list[str]:
    if not isinstance(blocks, list):
        return []

    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = _clean_text(block.get("text"))
        if not text:
            continue
        if str(block.get("type") or "").strip() == "heading":
            lines.append(f"### {text}")
        else:
            lines.append(text)
    return lines


def _format_delta(row: dict[str, Any]) -> str:
    op = str(row.get("op") or "").strip()
    if op == "remove":
        return ""
    if op not in {"add", "update"}:
        return ""

    block = row.get("block")
    if not isinstance(block, dict):
        return ""
    return _clean_text(block.get("text"))


def _bound_chunk(
    markdown: str,
    emitted_chars: int,
    budget: int,
) -> tuple[str, int, bool]:
    remaining = budget - emitted_chars
    if remaining <= 0:
        return _EXTRACTION_BOUND_MARKER, _MAX_EXTRACTION_CHARS, True
    if len(markdown) > remaining:
        bounded = markdown[:remaining] + _EXTRACTION_BOUND_MARKER
        return bounded, emitted_chars + len(bounded), True
    return markdown, emitted_chars + len(markdown), False


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _load_jsonl(path: Path) -> list[dict]:
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning(
                    "Skipping invalid browser JSONL row %s:%d", path, line_number
                )
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
            else:
                LOG.warning(
                    "Skipping non-object browser JSONL row %s:%d", path, line_number
                )
    return entries

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build generated fixtures consumed by the Rust core workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

from solstone.convey.contract.assemble import CALLOSUM_REGISTRY
from solstone.think import markdown as markdown_formatter
from solstone.think.cogitate_contract import (
    COGITATE_ACCESS_TIERS,
    COGITATE_READ_TOOL_NAMES,
    COGITATE_RUNTIME_PREAMBLE,
    FUTURE_ACCESS_TIERS,
    TALENT_FINALIZATION_MODES,
    capabilities_for_access_tier,
)
from solstone.think.indexer.edges import EDGES_SCHEMA_VERSION, _ensure_edges_schema

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "core" / "fixtures"
CALLOSUM_ARTIFACT_PATH = FIXTURE_DIR / "callosum_registry.json"
COGITATE_ARTIFACT_PATH = FIXTURE_DIR / "cogitate_contract.json"
EDGE_SCHEMA_ARTIFACT_PATH = FIXTURE_DIR / "edge_schema.json"
MARKDOWN_CHUNKS_ARTIFACT_PATH = FIXTURE_DIR / "markdown_chunks.json"
OVERSIZED_SIZE_NORMALIZATION = "oversized_size"
OVERSIZED_SIZE_TOKEN = "normalizedsize"


def build_callosum_registry_fixture() -> dict[str, Any]:
    return {
        "fixture": "solstone-callosum-registry",
        "fixture_version": 1,
        "generated_by": "make core-fixtures",
        "registry": {
            tract: list(CALLOSUM_REGISTRY[tract]) for tract in sorted(CALLOSUM_REGISTRY)
        },
    }


def build_cogitate_contract_fixture() -> dict[str, Any]:
    preamble_bytes = COGITATE_RUNTIME_PREAMBLE.encode("utf-8")
    return {
        "fixture": "solstone-cogitate-contract",
        "fixture_version": 1,
        "generated_by": "make core-fixtures",
        "access_tiers": list(COGITATE_ACCESS_TIERS),
        "capabilities": {
            tier: {
                "sol": capabilities_for_access_tier(tier).sol,
                "reads": capabilities_for_access_tier(tier).reads,
                "submit": capabilities_for_access_tier(tier).submit,
            }
            for tier in COGITATE_ACCESS_TIERS
        },
        "future_access_tiers": list(FUTURE_ACCESS_TIERS),
        "read_tools": list(COGITATE_READ_TOOL_NAMES),
        "finalization_modes": list(TALENT_FINALIZATION_MODES),
        "runtime_preamble": {
            "digest": hashlib.sha256(preamble_bytes).hexdigest(),
            "algorithm": "sha256",
            "encoding": "utf-8",
            "byte_length": len(preamble_bytes),
        },
    }


def build_edge_schema_fixture() -> dict[str, Any]:
    conn = sqlite3.connect(":memory:")

    def table_schema(table: str) -> dict[str, Any]:
        columns = [
            {"name": row[1], "type": row[2], "notnull": row[3], "pk": row[5]}
            for row in conn.execute(f"PRAGMA table_info({table})")
        ]
        indexes = []
        for row in conn.execute(f"PRAGMA index_list({table})"):
            index_name = row[1]
            indexes.append(
                {
                    "name": index_name,
                    "unique": row[2],
                    "origin": row[3],
                    "columns": [
                        column[2]
                        for column in conn.execute(f"PRAGMA index_info({index_name})")
                    ],
                }
            )
        indexes.sort(key=lambda index: index["name"])
        return {"columns": columns, "indexes": indexes}

    try:
        _ensure_edges_schema(conn)
        row = conn.execute("SELECT path, mtime FROM edge_files").fetchone()
        if row is None:
            raise RuntimeError("edge schema sentinel is missing")
        sentinel = {"path": row[0], "mtime": row[1]}
        return {
            "fixture": "solstone-edge-schema",
            "fixture_version": 1,
            "generated_by": "make core-fixtures",
            "schema_version": EDGES_SCHEMA_VERSION,
            "sentinel": sentinel,
            "tables": {
                "edge_files": table_schema("edge_files"),
                "edges": table_schema("edges"),
            },
        }
    finally:
        conn.close()


def _markdown_fixture_cases() -> list[dict[str, Any]]:
    long_line = "z" * (markdown_formatter._MAX_LINE_CHARS + 1)
    non_ascii_under_line_bound = "é" * (markdown_formatter._MAX_LINE_CHARS - 1)
    non_ascii_over_line_bound = "é" * (markdown_formatter._MAX_LINE_CHARS + 1)
    non_ascii_chunk_body = "\n".join(["é" * 1300] * 3)
    oversized_line = "alpha " * 300
    oversized_body = "\n".join([oversized_line] * 3)
    return [
        {"id": "empty", "input": ""},
        {"id": "whitespace_only", "input": " \n\t\n"},
        {"id": "heading_only", "input": "# Heading\n"},
        {"id": "thematic_break_only", "input": "---\n"},
        {
            "id": "header_only_table",
            "input": "| Name | Value |\n| --- | --- |\n",
        },
        {
            "id": "nested_heading_context",
            "input": "# Root\n\n## Child\n\nalpha paragraph\n\n### Leaf\n\nbeta paragraph\n",
        },
        {
            "id": "ordinary_paragraphs",
            "input": "# Notes\n\nalpha paragraph\n\nbeta paragraph\n",
        },
        {
            "id": "ordinary_list",
            "input": "# Tasks\n\n- alpha item\n- beta item\n",
        },
        {
            "id": "intro_list",
            "input": "# Tasks\n\nintro alpha\n\n- alpha item\n- beta item\n",
        },
        {
            "id": "intro_table",
            "input": (
                "# Metrics\n\nintro alpha\n\n"
                "| Name | Value |\n| --- | --- |\n| alpha | one |\n| beta | two |\n"
            ),
        },
        {
            "id": "definition_2_of_4",
            "input": (
                "# Definitions\n\n"
                "- **alpha:** value one\n"
                "- ordinary note.\n"
                "- **beta:** value two\n"
                "- ordinary other.\n"
            ),
        },
        {
            "id": "definition_2_of_5",
            "input": (
                "# Boundary\n\n"
                "- **alpha:** value one\n"
                "- ordinary note.\n"
                "- **beta:** value two\n"
                "- ordinary other.\n"
                "- ordinary final.\n"
            ),
        },
        {
            "id": "definition_1_of_2",
            "input": "# Boundary\n\n- **alpha:** value one\n- ordinary note.\n",
        },
        {
            "id": "multi_row_table",
            "input": (
                "# Matrix\n\n"
                "| Name | Value |\n| --- | --- |\n"
                "| alpha | one |\n| beta | two |\n| gamma | three |\n"
            ),
        },
        {
            "id": "fenced_code_info",
            "input": "# Code\n\n```python\nprint('alpha')\n```\n",
        },
        {
            "id": "blockquote_multi_paragraph",
            "input": "# Quote\n\n> alpha quote\n>\n> beta quote\n",
        },
        {
            "id": "overlong_line",
            "input": f"# Long\n\n{long_line}\n\nkept alpha\n",
        },
        {
            "id": "oversized_chunk",
            "input": f"# Big\n\n{oversized_body}\n",
        },
        {
            "id": "loose_nested_list",
            "input": "# Nested\n\n- parent alpha\n\n  - child beta\n",
        },
        {
            "id": "two_paragraph_list_item",
            "input": "# Loose\n\n- first alpha\n\n  second beta\n",
        },
        {
            "id": "list_item_fenced_code",
            "input": "# Item Code\n\n- alpha before\n\n  ```python\n  print('beta')\n  ```\n",
        },
        {
            "id": "inline_link",
            "input": "# Link\n\n[alpha](https://example.com/path/to-beta?q=gamma)\n",
        },
        {
            "id": "inline_image",
            "input": '# Image\n\n![alt text](images/pic-alpha.png "title beta")\n',
        },
        {
            "id": "autolink",
            "input": "# Auto\n\n<https://example.com/path?q=gamma>\n",
        },
        {
            "id": "reference_link",
            "input": '# Reference\n\n[alpha][ref]\n\n[ref]: https://example.com/path "title beta"\n',
        },
        {
            "id": "inline_html",
            "input": "# Html\n\nalpha <span>beta</span> gamma\n",
        },
        {
            "id": "non_ascii_line_under_char_bound_over_byte_bound",
            "input": f"# Accent\n\n{non_ascii_under_line_bound}\n",
            "token_comparison": False,
            "token_comparison_reason": (
                "non-ASCII is outside the ASCII tokenizer-equivalence guarantee; "
                "compare only chunk count and warnings"
            ),
        },
        {
            "id": "non_ascii_line_over_char_bound",
            "input": f"# Accent\n\n{non_ascii_over_line_bound}\n\nkept ascii\n",
            "token_comparison": False,
            "token_comparison_reason": (
                "non-ASCII is outside the ASCII tokenizer-equivalence guarantee; "
                "compare only chunk count and warnings"
            ),
        },
        {
            "id": "non_ascii_chunk_under_char_bound_over_byte_bound",
            "input": f"# Accent\n\n{non_ascii_chunk_body}\n",
            "token_comparison": False,
            "token_comparison_reason": (
                "non-ASCII is outside the ASCII tokenizer-equivalence guarantee; "
                "compare only chunk count and warnings"
            ),
        },
    ]


class _WarningCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _format_markdown_with_warnings(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    logger = logging.getLogger(markdown_formatter.__name__)
    handler = _WarningCapture()
    logger.addHandler(handler)
    try:
        chunks, _meta = markdown_formatter.format_markdown(text)
    finally:
        logger.removeHandler(handler)
    return chunks, handler.messages


def _fts5_tokens(chunks: list[str]) -> list[list[str]]:
    tokens: list[list[str]] = [[] for _chunk in chunks]
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(content)")
        conn.executemany(
            "INSERT INTO chunks(content) VALUES (?)",
            [(chunk,) for chunk in chunks],
        )
        conn.execute("CREATE VIRTUAL TABLE vocab USING fts5vocab(chunks, 'instance')")
        rows = conn.execute(
            "SELECT doc, offset, term FROM vocab ORDER BY doc, offset"
        ).fetchall()
    finally:
        conn.close()

    for doc, _offset, term in rows:
        tokens[int(doc) - 1].append(str(term))
    return tokens


def _normalize_oversized_size_tokens(tokens: list[str]) -> list[str]:
    normalized: list[str] = []
    i = 0
    while i < len(tokens):
        if i + 5 < len(tokens) and tokens[i : i + 5] == [
            "content",
            "too",
            "large",
            "to",
            "index",
        ]:
            normalized.extend(tokens[i : i + 5])
            j = i + 5
            while j < len(tokens) and tokens[j] != "chars":
                j += 1
            if j < len(tokens):
                normalized.append(OVERSIZED_SIZE_TOKEN)
                normalized.append("chars")
                i = j + 1
                continue
        normalized.append(tokens[i])
        i += 1
    return normalized


def _normalize_tokens(tokens: list[str], normalizations: list[str]) -> list[str]:
    if OVERSIZED_SIZE_NORMALIZATION in normalizations:
        tokens = _normalize_oversized_size_tokens(tokens)
    return tokens


def build_markdown_chunks_fixture() -> dict[str, Any]:
    cases = []
    for case in _markdown_fixture_cases():
        token_comparison = case.get("token_comparison", True)
        if token_comparison and not case["input"].isascii():
            raise RuntimeError(f"markdown fixture case is not ASCII-only: {case['id']}")
        chunks, warnings = _format_markdown_with_warnings(case["input"])
        entry = {
            "id": case["id"],
            "input": case["input"],
            "chunk_count": len(chunks),
            "warnings": warnings,
        }
        if token_comparison:
            rendered = [chunk["markdown"] for chunk in chunks]
            tokens_by_chunk = _fts5_tokens(rendered)
            chunk_entries = []
            for markdown, tokens in zip(rendered, tokens_by_chunk, strict=True):
                normalizations = (
                    [OVERSIZED_SIZE_NORMALIZATION]
                    if "[Content too large to index:" in markdown
                    else []
                )
                chunk_entry: dict[str, Any] = {
                    "markdown": markdown,
                    "tokens": _normalize_tokens(tokens, normalizations),
                }
                if normalizations:
                    chunk_entry["normalizations"] = normalizations
                chunk_entries.append(chunk_entry)
            entry["chunks"] = chunk_entries
        else:
            entry["token_comparison"] = False
            entry["token_comparison_reason"] = case["token_comparison_reason"]
        cases.append(entry)

    return {
        "fixture": "solstone-markdown-chunks",
        "fixture_version": 1,
        "generated_by": "make core-fixtures",
        "constraints": {
            "token_comparison_cases_ascii_only": True,
            "token_comparison_false_behavior": (
                "non-ASCII cases record only chunk_count and warnings because "
                "they are outside the ASCII tokenizer-equivalence guarantee"
            ),
            "max_line_chars": markdown_formatter._MAX_LINE_CHARS,
            "max_chunk_chars": markdown_formatter._MAX_CHUNK_CHARS,
            "normalizations": {
                OVERSIZED_SIZE_NORMALIZATION: (
                    "replace content-too-large size number tokens with "
                    f"{OVERSIZED_SIZE_TOKEN}"
                )
            },
            "tokenizer": (
                "sqlite fts5(content) with fts5vocab(chunks, 'instance') "
                "ordered by doc, offset"
            ),
        },
        "cases": cases,
    }


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def expected_outputs() -> dict[Path, str]:
    return {
        CALLOSUM_ARTIFACT_PATH: render_json(build_callosum_registry_fixture()),
        COGITATE_ARTIFACT_PATH: render_json(build_cogitate_contract_fixture()),
        EDGE_SCHEMA_ARTIFACT_PATH: render_json(build_edge_schema_fixture()),
        MARKDOWN_CHUNKS_ARTIFACT_PATH: render_json(build_markdown_chunks_fixture()),
    }


def write_outputs() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in expected_outputs().items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


def check_outputs() -> int:
    stale: list[str] = []
    for path, expected in expected_outputs().items():
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        if current != expected:
            stale.append(str(path.relative_to(ROOT)))

    if stale:
        paths = ", ".join(stale)
        print(
            f"Core generated fixtures are stale: {paths}. Run: make core-fixtures",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check generated fixtures without writing files.",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_outputs()
    write_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

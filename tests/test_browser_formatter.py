# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for browser stream formatting."""

from __future__ import annotations

import json

from solstone.think.browser_formatter import format_browser, format_browser_text
from solstone.think.markdown import _EXTRACTION_BOUND_MARKER, _MAX_EXTRACTION_CHARS


def _browser_entries() -> list[dict]:
    return [
        {
            "t": "segment_start",
            "ts": 1783046501000,
            "site": "mail.google.com",
            "url": "https://mail.google.com/mail/u/0/#inbox",
            "title": "Inbox - Gmail",
            "adapter": "gmail",
            "ctx": "inbox",
            "blocks": [
                {"type": "heading", "text": "Inbox"},
                {"type": "row", "text": "Ari Patel - Browser stream contract review"},
                {"type": "row", "text": "   "},
                {"type": "link", "text": "Open pull request"},
            ],
        },
        {
            "t": "delta",
            "ts": 1783046509120,
            "site": "mail.google.com",
            "adapter": "gmail",
            "ctx": "inbox",
            "op": "add",
            "block": {"type": "row", "text": "New: Casey Morgan - Lunch moved"},
        },
        {
            "t": "delta",
            "ts": 1783046523440,
            "site": "mail.google.com",
            "adapter": "gmail",
            "ctx": "inbox",
            "op": "update",
            "block": {
                "type": "row",
                "text": "Ari Patel - Browser stream contract review (2 replies)",
            },
        },
        {
            "t": "delta",
            "ts": 1783046530100,
            "site": "mail.google.com",
            "adapter": "gmail",
            "ctx": "inbox",
            "op": "remove",
            "block": {"type": "row", "text": "Promotions tab collapsed"},
        },
        {
            "t": "segment_start",
            "ts": 1783046594000,
            "site": "mail.google.com",
            "title": "Browser stream contract review - Gmail",
            "adapter": "gmail",
            "ctx": "thread",
            "blocks": [
                {"type": "heading", "text": "Browser stream contract review"},
                {
                    "type": "text",
                    "text": "Ari: The contract should accept delta-first recovery.",
                },
            ],
        },
        {
            "t": "segment_start",
            "ts": 1783046678000,
            "site": "mail.google.com",
            "title": "Search results for browser format",
            "adapter": "gmail",
            "blocks": [
                {"type": "heading", "text": "Search mail"},
                {"type": "row", "text": "Results for browser format"},
            ],
        },
        {
            "t": "delta",
            "ts": 1783046750123,
            "site": "mail.google.com",
            "adapter": "gmail",
            "op": "add",
            "block": {"type": "text", "text": "Status toast: All changes saved"},
        },
    ]


def test_format_browser_renders_snapshots_and_deltas():
    entries = _browser_entries()

    chunks, meta = format_browser(entries)

    assert meta["indexer"]["agent"] == "browser"
    assert len(chunks) == 6
    assert [chunk["timestamp"] for chunk in chunks] == [
        1783046501000,
        1783046509120,
        1783046523440,
        1783046594000,
        1783046678000,
        1783046750123,
    ]
    assert chunks[0]["source"] is entries[0]
    assert chunks[0]["markdown"].startswith(
        "## Inbox - Gmail\n\ngmail · mail.google.com"
    )
    assert "### Inbox" in chunks[0]["markdown"]
    assert "Open pull request" in chunks[0]["markdown"]
    assert "Promotions tab collapsed" not in "\n".join(
        chunk["markdown"] for chunk in chunks
    )
    assert chunks[-1]["markdown"] == "Status toast: All changes saved"


def test_format_browser_empty_entries_sets_error():
    chunks, meta = format_browser([])

    assert chunks == []
    assert meta["indexer"]["agent"] == "browser"
    assert meta["error"] == "browser stream has no rows"


def test_format_browser_delta_only_sets_error_but_emits_deltas():
    entries = [
        {
            "t": "delta",
            "ts": 10,
            "op": "add",
            "block": {"type": "text", "text": "first delta"},
        },
        {
            "t": "delta",
            "ts": 11,
            "op": "update",
            "block": {"type": "text", "text": "second delta"},
        },
    ]

    chunks, meta = format_browser(entries)

    assert [chunk["markdown"] for chunk in chunks] == ["first delta", "second delta"]
    assert meta["error"] == "browser stream has no segment_start rows"


def test_format_browser_truncates_oversized_snapshot():
    entries = [
        {
            "t": "segment_start",
            "ts": 1,
            "title": "Huge Gmail",
            "site": "mail.google.com",
            "blocks": [
                {"type": "row", "text": f"Message {index} " + ("x" * 120)}
                for index in range(500)
            ],
        }
    ]

    chunks, meta = format_browser(entries)

    assert meta["indexer"]["agent"] == "browser"
    assert len(chunks) == 1
    assert len(chunks[0]["markdown"]) <= _MAX_EXTRACTION_CHARS
    assert chunks[0]["markdown"].endswith(_EXTRACTION_BOUND_MARKER)


def test_format_browser_text_reads_jsonl(tmp_path):
    path = tmp_path / "browser_mail-google-com.jsonl"
    path.write_text(
        "\n".join(json.dumps(entry) for entry in _browser_entries()[:2]) + "\n",
        encoding="utf-8",
    )

    text = format_browser_text(path)

    assert "## Inbox - Gmail" in text
    assert "New: Casey Morgan - Lunch moved" in text

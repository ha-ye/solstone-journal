# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Persisted-index contracts against a disposable copy of the fixture journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.indexer.journal import (
    get_journal_index,
    index_file,
    scan_journal,
    search_counts,
    search_journal,
)

pytestmark = pytest.mark.integration


def _scan(journal: Path) -> None:
    scan_journal(str(journal), full=True)


def _entity_count(journal: Path) -> int:
    conn, _ = get_journal_index(str(journal))
    try:
        return conn.execute(
            "SELECT count(*) FROM chunks WHERE agent='entity'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_search_journal_stream_filter(journal_copy: Path) -> None:
    _scan(journal_copy)

    total, results = search_journal("", stream="default")
    assert total > 0
    assert all(result["metadata"]["stream"] == "default" for result in results)

    total, _results = search_journal("", stream="nonexistent")
    assert total == 0


def test_search_journal_results_include_stream(journal_copy: Path) -> None:
    _scan(journal_copy)

    total, results = search_journal("", stream="default")

    assert total > 0
    assert all(result["metadata"]["stream"] == "default" for result in results)


def test_browser_fixture_chunks_include_stream_and_agent(
    journal_copy: Path,
) -> None:
    rel = "20260703/suze.browser/000141_317/browser_mail-google-com.jsonl"

    index_file(str(journal_copy), rel)

    conn, _ = get_journal_index(str(journal_copy))
    try:
        rows = conn.execute(
            "SELECT agent, stream FROM chunks WHERE path=? ORDER BY idx",
            (rel,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 6
    assert {agent for agent, _stream in rows} == {"browser"}
    assert {stream for _agent, stream in rows} == {"suze.browser"}


def test_search_counts_stream_filter(journal_copy: Path) -> None:
    _scan(journal_copy)

    assert "streams" in search_counts("")
    assert search_counts("", stream="default")["total"] > 0
    assert search_counts("", stream="nonexistent")["total"] == 0


def test_entity_search_chunks_indexed(journal_copy: Path) -> None:
    _scan(journal_copy)

    # One chunk per entity-facet relationship in the current fixture journal.
    assert _entity_count(journal_copy) == 40


def test_entity_search_chunks_use_entity_search_path(journal_copy: Path) -> None:
    _scan(journal_copy)
    conn, _ = get_journal_index(str(journal_copy))
    try:
        rows = conn.execute(
            "SELECT DISTINCT path FROM chunks WHERE agent='entity'"
        ).fetchall()
    finally:
        conn.close()

    assert all(row[0].startswith("entity_search:") for row in rows)


def test_entity_search_by_name(journal_copy: Path) -> None:
    _scan(journal_copy)

    total, results = search_journal("Alice Johnson", agent="entity")

    assert total >= 1
    assert any(result["metadata"]["agent"] == "entity" for result in results)


def test_entity_search_by_type(journal_copy: Path) -> None:
    _scan(journal_copy)

    total, _results = search_journal("Person", agent="entity")

    assert total >= 1


def test_entity_search_includes_description(journal_copy: Path) -> None:
    _scan(journal_copy)

    total, results = search_journal("college", agent="entity")

    assert total >= 1
    assert any("college" in result["text"].lower() for result in results)


def test_entity_search_includes_facet(journal_copy: Path) -> None:
    _scan(journal_copy)

    total, results = search_journal(
        "Alice Johnson",
        agent="entity",
        facet="personal",
    )

    assert total >= 1
    assert all(result["metadata"]["facet"] == "personal" for result in results)


def test_entity_search_idempotent(journal_copy: Path) -> None:
    _scan(journal_copy)
    count1 = _entity_count(journal_copy)

    _scan(journal_copy)
    count2 = _entity_count(journal_copy)

    assert count1 == count2 == 40

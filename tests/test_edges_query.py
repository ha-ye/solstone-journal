# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for edge read/query APIs and entity-fold maintenance."""

from __future__ import annotations

import math
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from solstone.think.indexer import edges as edge_index
from solstone.think.indexer import journal as journal_index
from solstone.think.indexer.edges import (
    count_entity_edges,
    fold_entity_edges,
    insert_edges,
    load_edge_evidence,
    load_entity_network,
    load_network_overview,
)
from solstone.think.indexer.journal import (
    DB_NAME,
    INDEX_DIR,
    get_journal_index,
    scan_journal,
)
from tests._sqlite_assertions import edges_content_hash, table_content_hash

EDGE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "edges_journal"
CHUNK_COLUMNS = [
    "content",
    "path",
    "day",
    "facet",
    "agent",
    "stream",
    "idx",
    "time_bucket",
]
FILE_COLUMNS = ["path", "mtime"]
EDGE_FILE_COLUMNS = ["path", "mtime"]


@pytest.fixture
def edges_journal(tmp_path, monkeypatch):
    journal = tmp_path / "edges_journal"
    shutil.copytree(EDGE_FIXTURE, journal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    return journal


def _db_path(journal: Path) -> Path:
    return journal / INDEX_DIR / DB_NAME


def _direct_conn(journal: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(journal))
    conn.row_factory = sqlite3.Row
    return conn


def _insert(journal: Path, rows: list[dict[str, Any]]) -> None:
    conn, _ = get_journal_index(str(journal))
    insert_edges(conn, rows)
    conn.commit()
    conn.close()


def _row(
    src: str,
    dst: str,
    kind: str,
    path: str,
    *,
    day: str | None = "20260530",
    facet: str | None = "query-test",
    weight: int = 1,
    source: str = "query-test",
    anchor: str | None = None,
    label: str | None = None,
    ts: int | None = 0,
    src_name: str | None = None,
    dst_name: str | None = None,
) -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "kind": kind,
        "src_name": src_name,
        "dst_name": dst_name,
        "day": day,
        "facet": facet,
        "source": source,
        "path": path,
        "anchor": anchor,
        "label": label,
        "ts": ts,
        "weight": weight,
    }


def _table_hashes(journal: Path) -> dict[str, str]:
    conn = _direct_conn(journal)
    try:
        return {
            "edges": edges_content_hash(conn),
            "edge_files": table_content_hash(conn, "edge_files", EDGE_FILE_COLUMNS),
            "files": table_content_hash(conn, "files", FILE_COLUMNS),
            "chunks": table_content_hash(conn, "chunks", CHUNK_COLUMNS),
        }
    finally:
        conn.close()


def _edge_files_hash(journal: Path) -> str:
    conn = _direct_conn(journal)
    try:
        return table_content_hash(conn, "edge_files", EDGE_FILE_COLUMNS)
    finally:
        conn.close()


def _edge_rows(journal: Path) -> list[sqlite3.Row]:
    conn = _direct_conn(journal)
    try:
        return conn.execute(
            "SELECT src, dst, kind, directed, src_name, dst_name, path "
            "FROM edges ORDER BY path"
        ).fetchall()
    finally:
        conn.close()


def test_edge_kind_constant_tables_stay_in_sync():
    assert set(edge_index.KIND_WEIGHTS) == set(edge_index.KINDS)
    assert edge_index.DIRECTED_KINDS <= edge_index.KINDS


def test_entity_network_scores_kinds_decay_and_null_days(edges_journal):
    scan_journal(str(edges_journal), full=True)
    _insert(
        edges_journal,
        [
            _row(
                "edge_gold_self",
                "edge_gold_bob",
                "co-present",
                "golden/bob-co.jsonl",
                weight=2,
                src_name="Gold Self",
                dst_name="Gold Bob",
            ),
            _row(
                "edge_gold_self",
                "edge_gold_bob",
                "spoke-with",
                "golden/bob-spoke.jsonl",
                day="20260430",
                weight=1,
            ),
            _row(
                "edge_gold_self",
                "edge_gold_bob",
                "committed-to",
                "golden/bob-commit.jsonl",
                day=None,
                weight=1,
            ),
            _row(
                "edge_gold_self",
                "edge_gold_cora",
                "mentioned",
                "golden/cora-mentioned.jsonl",
                weight=2,
                src_name="Gold Self",
                dst_name="Gold Cora",
            ),
            _row(
                "edge_gold_self",
                "edge_gold_cora",
                "co-present",
                "golden/cora-co.jsonl",
                day="20260301",
                weight=5,
            ),
        ],
    )

    network = load_entity_network("edge_gold_self", reference_day="20260530")

    assert network["filters"] == {
        "kinds": None,
        "facet": None,
        "day_from": None,
        "day_to": None,
        "include_principal": False,
    }
    assert [n["entity_id"] for n in network["neighbors"]] == [
        "edge_gold_bob",
        "edge_gold_cora",
    ]
    bob, cora = network["neighbors"]
    assert bob["score"] == pytest.approx(9.866125242295157)
    assert bob["score"] == pytest.approx(
        sum(kind["weighted"] for kind in bob["kinds"].values())
    )
    assert bob["count"] == 3
    assert bob["first_seen"] == "20260430"
    assert bob["last_seen"] == "20260530"
    assert bob["directed"] == {"out": 1, "in": 0}
    assert bob["kinds"]["co-present"] == {"count": 1, "weighted": 2.0}
    assert bob["kinds"]["spoke-with"]["weighted"] == pytest.approx(
        4 * math.exp(-30 / 90)
    )
    assert bob["kinds"]["committed-to"] == {"count": 1, "weighted": 5.0}

    assert cora["score"] == pytest.approx(7.839397205857212)
    assert cora["directed"] == {"out": 1, "in": 0}
    assert cora["first_seen"] == "20260301"
    assert cora["last_seen"] == "20260530"
    assert cora["kinds"]["mentioned"] == {"count": 1, "weighted": 6.0}
    assert cora["kinds"]["co-present"]["weighted"] == pytest.approx(
        5 * math.exp(-90 / 90)
    )


def test_entity_network_ranks_by_weighted_decayed_score(edges_journal):
    scan_journal(str(edges_journal), full=True)
    _insert(
        edges_journal,
        [
            _row(
                "edge_rank_self",
                "edge_rank_byron",
                "co-present",
                f"rank/byron-{idx}.jsonl",
                day="20260301",
                weight=1,
            )
            for idx in range(4)
        ]
        + [
            _row(
                "edge_rank_self",
                "edge_rank_cora",
                "spoke-with",
                "rank/cora.jsonl",
                day="20260530",
                weight=1,
            )
        ],
    )

    network = load_entity_network("edge_rank_self", reference_day="20260530")

    assert [n["entity_id"] for n in network["neighbors"]] == [
        "edge_rank_cora",
        "edge_rank_byron",
    ]
    assert network["neighbors"][0]["score"] == pytest.approx(4.0)
    assert network["neighbors"][1]["score"] == pytest.approx(1.4715177646857693)


def test_edge_evidence_total_pagination_and_stable_newest_order(edges_journal):
    scan_journal(str(edges_journal), full=True)
    _insert(
        edges_journal,
        [
            _row(
                "edge_order_self",
                "edge_order_peer",
                "spoke-with",
                "order/old.jsonl",
                day="20260430",
                anchor="old",
                label="old",
                ts=999,
            ),
            _row(
                "edge_order_self",
                "edge_order_peer",
                "spoke-with",
                "order/null-day.jsonl",
                day=None,
                anchor="null-day",
                label="null-day",
                ts=999,
            ),
            _row(
                "edge_order_self",
                "edge_order_peer",
                "spoke-with",
                "order/null-ts.jsonl",
                day="20260530",
                anchor="null-ts",
                label="null-ts",
                ts=None,
            ),
            _row(
                "edge_order_self",
                "edge_order_peer",
                "spoke-with",
                "order/b.jsonl",
                day="20260530",
                anchor="b",
                label="b",
                ts=200,
            ),
            _row(
                "edge_order_self",
                "edge_order_peer",
                "spoke-with",
                "order/a.jsonl",
                day="20260530",
                anchor="a",
                label="a",
                ts=200,
            ),
        ],
    )

    page = load_edge_evidence(
        "edge_order_self",
        "edge_order_peer",
        limit=2,
        offset=1,
    )

    assert page["total"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert [row["label"] for row in page["evidence"]] == ["b", "null-ts"]


def test_filters_apply_to_scores_and_network_evidence(edges_journal):
    scan_journal(str(edges_journal), full=True)
    _insert(
        edges_journal,
        [
            _row(
                "edge_filter_self",
                "edge_filter_peer",
                "spoke-with",
                "filter/match.jsonl",
                day="20260515",
                facet="mixed-facet",
                label="match",
            ),
            _row(
                "edge_filter_self",
                "edge_filter_peer",
                "co-present",
                "filter/wrong-kind.jsonl",
                day="20260515",
                facet="mixed-facet",
                label="wrong-kind",
            ),
            _row(
                "edge_filter_self",
                "edge_filter_peer",
                "spoke-with",
                "filter/wrong-facet.jsonl",
                day="20260515",
                facet="other-facet",
                label="wrong-facet",
            ),
            _row(
                "edge_filter_self",
                "edge_filter_peer",
                "spoke-with",
                "filter/wrong-day.jsonl",
                day="20260401",
                facet="mixed-facet",
                label="wrong-day",
            ),
        ],
    )

    network = load_entity_network(
        "edge_filter_self",
        kinds=["spoke-with"],
        facet="MiXeD-FaCeT",
        day_from="20260501",
        day_to="20260531",
        reference_day="20260530",
    )
    evidence = load_edge_evidence(
        "edge_filter_self",
        "edge_filter_peer",
        kinds=["spoke-with"],
        facet="MiXeD-FaCeT",
        day_from="20260501",
        day_to="20260531",
    )

    assert network["total_neighbors"] == 1
    assert network["neighbors"][0]["score"] == pytest.approx(4 * math.exp(-15 / 90))
    assert [row["label"] for row in network["neighbors"][0]["evidence"]] == ["match"]
    assert evidence["total"] == 1
    assert [row["label"] for row in evidence["evidence"]] == ["match"]


def test_unknown_kind_filter_raises(edges_journal):
    scan_journal(str(edges_journal), full=True)

    with pytest.raises(ValueError, match="Unknown edge kind: 'bad-kind'"):
        load_entity_network("edge_filter_self", kinds=["bad-kind"])


def test_principal_exclusion_and_principal_self_query(edges_journal):
    scan_journal(str(edges_journal), full=True)

    bob_default = load_entity_network(
        "edge_bob",
        facet="edges-copresence",
        reference_day="20260430",
    )
    bob_with_principal = load_entity_network(
        "edge_bob",
        facet="edges-copresence",
        include_principal=True,
        reference_day="20260430",
    )
    alice_default = load_entity_network(
        "edge_alice",
        facet="edges-copresence",
        reference_day="20260430",
    )

    assert bob_default["neighbors"] == []
    assert [n["entity_id"] for n in bob_with_principal["neighbors"]] == ["edge_alice"]
    assert {n["entity_id"] for n in alice_default["neighbors"]} == {
        "edge_bob",
        "edge_cora",
    }


def test_insert_edges_marks_mentioned_directed_and_query_counts_in_out(edges_journal):
    scan_journal(str(edges_journal), full=True)
    _insert(
        edges_journal,
        [
            _row(
                "edge_z_mentions",
                "edge_a_mentioned",
                "mentioned",
                "mentioned/out.jsonl",
                label="out",
            ),
            _row(
                "edge_peer_mentions",
                "edge_z_mentions",
                "mentioned",
                "mentioned/in.jsonl",
                label="in",
            ),
        ],
    )

    conn = _direct_conn(edges_journal)
    try:
        stored = conn.execute(
            "SELECT src, dst, directed FROM edges WHERE path = ?",
            ("mentioned/out.jsonl",),
        ).fetchone()
    finally:
        conn.close()
    network = load_entity_network("edge_z_mentions", reference_day="20260530")

    assert dict(stored) == {
        "src": "edge_z_mentions",
        "dst": "edge_a_mentioned",
        "directed": 1,
    }
    by_peer = {n["entity_id"]: n for n in network["neighbors"]}
    assert by_peer["edge_a_mentioned"]["directed"] == {"out": 1, "in": 0}
    assert by_peer["edge_peer_mentions"]["directed"] == {"out": 0, "in": 1}


def test_fold_entity_edges_rewrites_drops_self_and_renormalizes_names(edges_journal):
    scan_journal(str(edges_journal), full=True)
    source = "edge_fold_source"
    target = "edge_000_target"
    peer = "edge_fold_peer"
    _insert(
        edges_journal,
        [
            _row(source, target, "co-present", "fold/self.jsonl"),
            _row(
                source,
                peer,
                "co-present",
                "fold/survivor.jsonl",
                src_name="Source Historical",
                dst_name="Peer Historical",
            ),
            _row(source, peer, "co-present", "fold/survivor-2.jsonl"),
            _row(source, peer, "committed-to", "fold/directed-out.jsonl"),
            _row(peer, source, "committed-to", "fold/directed-in.jsonl"),
            _row(
                "edge_unrelated_self",
                "edge_unrelated_self",
                "co-present",
                "fold/unrelated-self.jsonl",
            ),
        ],
    )
    before_edge_files = _edge_files_hash(edges_journal)

    result = fold_entity_edges(source, target)

    rows = _edge_rows(edges_journal)
    folded_rows = [row for row in rows if row["path"].startswith("fold/")]
    assert result == {"rows_folded": 5, "self_edges_dropped": 1}
    assert all(row["src"] != source and row["dst"] != source for row in folded_rows)
    assert any(
        row["path"] == "fold/unrelated-self.jsonl"
        and row["src"] == "edge_unrelated_self"
        and row["dst"] == "edge_unrelated_self"
        for row in folded_rows
    )
    survivor = next(row for row in folded_rows if row["path"] == "fold/survivor.jsonl")
    assert survivor["src"] == target
    assert survivor["dst"] == peer
    assert survivor["src_name"] == "Source Historical"
    assert survivor["dst_name"] == "Peer Historical"
    assert sum(row["path"].startswith("fold/survivor") for row in folded_rows) == 2
    directed_out = next(
        row for row in folded_rows if row["path"] == "fold/directed-out.jsonl"
    )
    assert directed_out["directed"] == 1
    assert directed_out["src"] == target
    assert directed_out["dst"] == peer
    directed_in = next(
        row for row in folded_rows if row["path"] == "fold/directed-in.jsonl"
    )
    assert directed_in["directed"] == 1
    assert directed_in["src"] == peer
    assert directed_in["dst"] == target

    assert _edge_files_hash(edges_journal) == before_edge_files


def test_network_overview_shape_totals_kinds_entities_and_filter(edges_journal):
    scan_journal(str(edges_journal), full=True)
    _insert(
        edges_journal,
        [
            _row(
                "edge_overview_a",
                "edge_overview_b",
                "spoke-with",
                "overview/spoke.jsonl",
                facet="overview",
                src_name="Overview A",
                dst_name="Overview B",
            ),
            _row(
                "edge_overview_a",
                "edge_overview_c",
                "co-present",
                "overview/co.jsonl",
                day="20260301",
                facet="overview",
                weight=2,
            ),
            _row(
                "edge_overview_b",
                "edge_overview_c",
                "committed-to",
                "overview/excluded.jsonl",
                day="20260430",
                facet="other-overview",
            ),
        ],
    )

    overview = load_network_overview(
        facet="Overview",
        reference_day="20260530",
        limit=2,
    )

    decayed_co = 2 * math.exp(-90 / 90)
    assert overview["filters"]["facet"] == "overview"
    assert overview["totals"] == {"edges": 2, "entities": 3}
    assert overview["kinds"]["spoke-with"] == {"count": 1, "weighted": 4.0}
    assert overview["kinds"]["co-present"]["count"] == 1
    assert overview["kinds"]["co-present"]["weighted"] == pytest.approx(decayed_co)
    assert [entity["entity_id"] for entity in overview["entities"]] == [
        "edge_overview_a",
        "edge_overview_b",
    ]
    assert overview["entities"][0]["score"] == pytest.approx(4 + decayed_co)
    assert overview["entities"][1]["score"] == pytest.approx(4.0)


def test_read_apis_are_query_only_and_content_stable(edges_journal, monkeypatch):
    scan_journal(str(edges_journal), full=True)
    before = _table_hashes(edges_journal)

    def fail_schema(*args, **kwargs):
        pytest.fail("_ensure_edges_schema should be unreachable from edge reads")

    def fail_get_journal_index(*args, **kwargs):
        pytest.fail("get_journal_index should be unreachable from edge reads")

    monkeypatch.setattr(edge_index, "_ensure_edges_schema", fail_schema)
    monkeypatch.setattr(journal_index, "_ensure_edges_schema", fail_schema)
    monkeypatch.setattr(journal_index, "get_journal_index", fail_get_journal_index)

    load_entity_network("edge_alice", reference_day="20260430")
    load_edge_evidence("edge_alice", "edge_bob")
    load_network_overview(reference_day="20260430")
    count_entity_edges("edge_alice")

    assert _table_hashes(edges_journal) == before


def test_read_apis_missing_index_raise_actionable_file_not_found(edges_journal):
    db_path = _db_path(edges_journal)
    assert not db_path.exists()

    with pytest.raises(FileNotFoundError, match="journal indexer --rescan"):
        load_entity_network("edge_alice")
    with pytest.raises(FileNotFoundError, match="journal indexer --rescan"):
        count_entity_edges("edge_alice")

    assert not db_path.exists()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for speaker-derived edge extraction."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.speakers import edges as speaker_edges
from solstone.apps.speakers.edges import extract_speaker_edges
from solstone.apps.speakers.time import segment_start_ts_ms
from solstone.think.edge_sources import EdgeContext
from solstone.think.indexer.journal import get_journal_index, index_file, scan_journal
from tests._sqlite_assertions import edges_content_hash

DAY = "20260430"
STREAM = "default"
SEGMENT = "120000_300"
COMPOSITE_ID = f"{DAY}/{STREAM}/{SEGMENT}"
LABEL_REL = f"{COMPOSITE_ID}/talents/speaker_labels.json"


@pytest.fixture
def edge_journal(tmp_path, monkeypatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    # Some tests mutate entities after extraction against the same tmp journal.
    speaker_edges._CANDIDATE_CACHE.clear()
    return journal


def _ctx(rel: str = LABEL_REL) -> EdgeContext:
    return EdgeContext(path=rel, day=DAY, facet="", resolve=lambda _name: None)


def _segment_dir(journal: Path) -> Path:
    path = journal / "chronicle" / DAY / STREAM / SEGMENT
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_entity(
    journal: Path,
    entity_id: str,
    name: str,
    *,
    aka: list[str] | None = None,
    blocked: bool = False,
) -> None:
    entity: dict[str, Any] = {"id": entity_id, "name": name, "type": "Person"}
    if aka is not None:
        entity["aka"] = aka
    if blocked:
        entity["blocked"] = True

    path = journal / "entities" / entity_id / "entity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entity) + "\n", encoding="utf-8")


def _write_labels(
    journal: Path,
    labels: list[dict[str, Any]],
    *,
    skipped: bool = False,
) -> Path:
    payload: dict[str, Any] = {"labels": labels}
    if skipped:
        payload["skipped"] = True

    path = _segment_dir(journal) / "talents" / "speaker_labels.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_transcript(
    journal: Path,
    sentences: list[str],
    *,
    stem: str = "audio",
) -> None:
    lines = [json.dumps({"raw": f"{stem}.flac", "model": "test"})]
    lines.extend(json.dumps({"text": sentence}) for sentence in sentences)
    (_segment_dir(journal) / f"{stem}.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _touch_npz(journal: Path, stem: str) -> None:
    (_segment_dir(journal) / f"{stem}.npz").touch()


def _extract(journal: Path, labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _write_labels(journal, labels)
    return extract_speaker_edges([], _ctx())


def _edge_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT src, dst, kind, day, path, anchor, source, label, weight
        FROM edges
        ORDER BY kind, src, dst, label
        """
    ).fetchall()


def test_ac1_two_speakers_emit_one_stored_spoke_with_row(edge_journal):
    _write_labels(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_b"},
            {"sentence_id": 2, "speaker": "speaker_a"},
        ],
    )
    _write_transcript(edge_journal, ["hello", "there"])

    assert index_file(str(edge_journal), LABEL_REL) is True
    conn, _ = get_journal_index(str(edge_journal))
    conn.row_factory = sqlite3.Row
    rows = _edge_rows(conn)
    conn.close()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["kind"] == "spoke-with"
    assert row["src"] == "speaker_a"
    assert row["dst"] == "speaker_b"
    assert row["anchor"] == COMPOSITE_ID
    assert row["day"] == DAY


def test_ac2_single_speaker_and_stub_emit_no_spoke_with_or_logs(
    edge_journal,
    caplog,
):
    _write_transcript(edge_journal, ["quiet", "anonymous"])
    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": None},
        ],
    )
    assert [row for row in rows if row["kind"] == "spoke-with"] == []

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger=speaker_edges.__name__)
    _write_labels(edge_journal, [], skipped=True)

    assert extract_speaker_edges([], _ctx()) == []
    assert [
        record for record in caplog.records if record.name == speaker_edges.__name__
    ] == []


def test_ac3_aka_match_emits_returned_mentioned_direction_and_variant_label(
    edge_journal,
):
    _write_entity(edge_journal, "target_c", "Cora Candidate", aka=["Project Zephyr"])
    _write_transcript(
        edge_journal,
        [
            "We discussed project zephyr at the boundary.",
            "PROJECT ZEPHYR came up again.",
        ],
    )
    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_a"},
        ],
    )

    mentions = [row for row in rows if row["kind"] == "mentioned"]
    assert mentions == [
        {
            "src": "speaker_a",
            "dst": "target_c",
            "kind": "mentioned",
            "src_name": None,
            "dst_name": "Cora Candidate",
            "day": DAY,
            "facet": "",
            "source": "mention",
            "path": LABEL_REL,
            "anchor": COMPOSITE_ID,
            "label": "Project Zephyr",
            "ts": segment_start_ts_ms(DAY, SEGMENT),
            "weight": 2,
        }
    ]


def test_candidate_index_refreshes_when_entity_files_change(edge_journal):
    _write_entity(edge_journal, "alpha_target", "Alpha Target")
    _write_transcript(edge_journal, ["Alpha Target is named."])
    assert [
        (row["dst"], row["label"])
        for row in _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])
        if row["kind"] == "mentioned"
    ] == [("alpha_target", "Alpha Target")]

    _write_entity(edge_journal, "beta_target", "Beta Target")
    _write_transcript(edge_journal, ["Beta Target is named."])
    assert [
        (row["dst"], row["label"])
        for row in _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])
        if row["kind"] == "mentioned"
    ] == [("beta_target", "Beta Target")]


def test_ac4_boundaries_short_forms_and_escaped_parenthetical_aka(edge_journal):
    _write_entity(edge_journal, "ann_target", "Ann")
    _write_entity(edge_journal, "short_target", "Al", aka=["Bo"])
    _write_entity(edge_journal, "ryan_target", "Ryan Entity", aka=["Ryan Reed (R2)"])
    _write_transcript(
        edge_journal,
        [
            "The annual Anna update mentioned Al, Bo, and R2 only.",
            "Ryan Reed joined later.",
        ],
    )
    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_a"},
        ],
    )

    mentions = [row for row in rows if row["kind"] == "mentioned"]
    assert [(row["dst"], row["label"], row["weight"]) for row in mentions] == [
        ("ryan_target", "Ryan Reed", 1)
    ]


def test_punctuation_terminal_variant_matches_at_nonword_boundaries(edge_journal):
    _write_entity(edge_journal, "acme_target", "Acme Inc.")
    _write_transcript(
        edge_journal,
        [
            "Acme Inc. shipped the release.",
            "Acme Inc.orporated should not match.",
        ],
    )

    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_a"},
        ],
    )

    mentions = [row for row in rows if row["kind"] == "mentioned"]
    assert [(row["dst"], row["label"], row["weight"]) for row in mentions] == [
        ("acme_target", "Acme Inc.", 1)
    ]


def test_punctuation_initial_aka_matches_at_nonword_boundaries(edge_journal):
    _write_entity(edge_journal, "dotnet_target", "Dot Net Entity", aka=[".NET"])
    _write_transcript(
        edge_journal,
        [
            "We worked on .NET today.",
            "foo.NET should not match.",
        ],
    )

    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_a"},
        ],
    )

    mentions = [row for row in rows if row["kind"] == "mentioned"]
    assert [(row["dst"], row["label"], row["weight"]) for row in mentions] == [
        ("dotnet_target", ".NET", 1)
    ]


def test_unicode_casefold_roundtrip_miss_is_silent(edge_journal):
    _write_entity(edge_journal, "iris_target", "Iris Example")
    _write_transcript(edge_journal, ["İris Example joined."])

    rows = _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])

    assert [row for row in rows if row["kind"] == "mentioned"] == []


def test_ac5_self_mentions_emit_no_rows(edge_journal):
    _write_entity(edge_journal, "speaker_a", "Alice Able", aka=["Captain A"])
    _write_transcript(edge_journal, ["Alice Able and Captain A checked in."])

    rows = _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])

    assert [row for row in rows if row["kind"] == "mentioned"] == []


def test_ac6_null_speaker_sentences_emit_no_mentions(edge_journal):
    _write_entity(edge_journal, "target_c", "Cora Candidate")
    _write_transcript(
        edge_journal,
        [
            "Cora Candidate is named here.",
            "This sentence has a known speaker but no entity.",
        ],
    )

    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": None},
            {"sentence_id": 2, "speaker": "speaker_a"},
        ],
    )

    assert [row for row in rows if row["kind"] == "mentioned"] == []


def test_ac7_blocked_entities_are_never_mention_targets(edge_journal):
    _write_entity(edge_journal, "target_c", "Cora Candidate", blocked=True)
    _write_transcript(edge_journal, ["Cora Candidate is named here."])

    rows = _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])

    assert [row for row in rows if row["kind"] == "mentioned"] == []


def test_ac8_missing_transcript_keeps_spoke_with_and_warns_once(
    edge_journal,
    caplog,
):
    caplog.set_level(logging.WARNING, logger=speaker_edges.__name__)

    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_b"},
        ],
    )

    assert [row for row in rows if row["kind"] == "spoke-with"]
    assert [row for row in rows if row["kind"] == "mentioned"] == []
    records = [
        record for record in caplog.records if record.name == speaker_edges.__name__
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert COMPOSITE_ID in records[0].getMessage()


def test_ac8_npz_without_transcript_warns_once(edge_journal, caplog):
    _touch_npz(edge_journal, "audio")
    caplog.set_level(logging.WARNING, logger=speaker_edges.__name__)

    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_b"},
        ],
    )

    assert [row for row in rows if row["kind"] == "spoke-with"]
    assert [row for row in rows if row["kind"] == "mentioned"] == []
    records = [
        record for record in caplog.records if record.name == speaker_edges.__name__
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert COMPOSITE_ID in records[0].getMessage()


def test_ac9_index_file_replaces_speaker_label_rows_by_path(edge_journal):
    _write_entity(edge_journal, "target_c", "Cora Candidate")
    _write_labels(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_b"},
        ],
    )
    _write_transcript(edge_journal, ["Cora Candidate is named.", "No mention here."])

    assert index_file(str(edge_journal), LABEL_REL) is True
    conn, _ = get_journal_index(str(edge_journal))
    first_count = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    conn.close()

    assert index_file(str(edge_journal), LABEL_REL) is True
    conn, _ = get_journal_index(str(edge_journal))
    second_count = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    edge_file = conn.execute(
        "SELECT path FROM edge_files WHERE path=?",
        (LABEL_REL,),
    ).fetchone()
    conn.close()

    assert first_count == 2
    assert second_count == first_count
    assert edge_file == (LABEL_REL,)


def test_ac10_scan_journal_full_is_content_hash_idempotent(edge_journal):
    _write_entity(edge_journal, "target_c", "Cora Candidate")
    _write_labels(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_b"},
        ],
    )
    _write_transcript(edge_journal, ["Cora Candidate is named.", "No mention here."])

    assert scan_journal(str(edge_journal), full=True) is True
    conn, _ = get_journal_index(str(edge_journal))
    first_hash = edges_content_hash(conn)
    conn.close()

    scan_journal(str(edge_journal), full=True)
    conn, _ = get_journal_index(str(edge_journal))
    second_hash = edges_content_hash(conn)
    conn.close()

    assert second_hash == first_hash


def test_d2_npz_stem_wins_over_other_transcripts(edge_journal):
    _write_entity(edge_journal, "audio_target", "Audio Target")
    _write_entity(edge_journal, "mic_target", "Mic Target")
    _write_transcript(edge_journal, ["Audio Target is in the plain transcript."])
    _write_transcript(
        edge_journal, ["Mic Target is in the npz transcript."], stem="mic_audio"
    )
    _touch_npz(edge_journal, "mic_audio")

    rows = _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])

    mentions = [row for row in rows if row["kind"] == "mentioned"]
    assert [(row["dst"], row["label"]) for row in mentions] == [
        ("mic_target", "Mic Target")
    ]


def test_d2_single_jsonl_fallback_without_npz_emits_mentions(edge_journal, caplog):
    _write_entity(edge_journal, "target_c", "Cora Candidate")
    _write_transcript(edge_journal, ["Cora Candidate is named."])
    caplog.set_level(logging.WARNING, logger=speaker_edges.__name__)

    rows = _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])

    assert [
        (row["dst"], row["label"]) for row in rows if row["kind"] == "mentioned"
    ] == [("target_c", "Cora Candidate")]
    assert [
        record for record in caplog.records if record.name == speaker_edges.__name__
    ] == []


def test_d2_ambiguous_multi_jsonl_without_npz_warns_and_skips_mentions(
    edge_journal,
    caplog,
):
    _write_entity(edge_journal, "target_c", "Cora Candidate")
    _write_transcript(edge_journal, ["Cora Candidate is named."])
    _write_transcript(edge_journal, ["Cora Candidate is named."], stem="mic_audio")
    caplog.set_level(logging.WARNING, logger=speaker_edges.__name__)

    rows = _extract(
        edge_journal,
        [
            {"sentence_id": 1, "speaker": "speaker_a"},
            {"sentence_id": 2, "speaker": "speaker_b"},
        ],
    )

    assert [row for row in rows if row["kind"] == "spoke-with"]
    assert [row for row in rows if row["kind"] == "mentioned"] == []
    records = [
        record for record in caplog.records if record.name == speaker_edges.__name__
    ]
    assert len(records) == 1
    assert COMPOSITE_ID in records[0].getMessage()


def test_d3_ambiguous_casefolded_variant_is_dropped(edge_journal):
    _write_entity(edge_journal, "chris_one", "Chris One", aka=["Chris Ray"])
    _write_entity(edge_journal, "chris_two", "Chris Two", aka=["chris ray"])
    _write_transcript(edge_journal, ["Chris Ray was mentioned."])

    rows = _extract(edge_journal, [{"sentence_id": 1, "speaker": "speaker_a"}])

    assert [row for row in rows if row["kind"] == "mentioned"] == []

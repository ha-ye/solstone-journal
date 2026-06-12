# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

import pytest

from solstone.think.day_accumulator import append_record, read_latest, read_records


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


def _accumulator_path(journal: Path, day: str, name: str) -> Path:
    return journal / "chronicle" / day / "talents" / f"{name}.jsonl"


def test_first_write_creates_file_and_stamps_ts(journal):
    day = "20260611"

    append_record(day, "pulse", {"summary": "a"})

    path = _accumulator_path(journal, day, "pulse")
    assert path.is_file()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    records = read_records(day, "pulse")
    assert len(records) == 1
    assert records[0]["summary"] == "a"
    assert "ts" in records[0]


def test_append_record_does_not_overwrite_existing_records(journal):
    day = "20260611"

    append_record(day, "pulse", {"summary": "a", "ts": 100})
    append_record(day, "pulse", {"summary": "b", "ts": 200})

    records = read_records(day, "pulse")
    assert [record["summary"] for record in records] == ["a", "b"]
    assert [record["ts"] for record in records] == [100, 200]


def test_read_latest_uses_highest_ts_and_last_file_position_for_ties(journal):
    day = "20260611"

    append_record(day, "pulse", {"summary": "a", "ts": 100})
    append_record(day, "pulse", {"summary": "b", "ts": 300})
    append_record(day, "pulse", {"summary": "c", "ts": 200})

    assert read_latest(day, "pulse")["summary"] == "b"

    append_record(day, "tie", {"summary": "first", "ts": 100})
    append_record(day, "tie", {"summary": "second", "ts": 100})

    assert read_latest(day, "tie")["summary"] == "second"


def test_read_latest_lookback_window(journal):
    base_day = "20260611"

    append_record("20260608", "pulse", {"summary": "lookback", "ts": 100})

    assert read_latest(base_day, "pulse", lookback_days=7)["summary"] == "lookback"
    assert read_latest(base_day, "pulse", lookback_days=2) is None

    append_record("20260601", "far", {"summary": "too far", "ts": 100})

    assert read_latest(base_day, "far", lookback_days=7) is None


def test_read_records_returns_ascending_stable_ts_order(journal):
    day = "20260611"

    append_record(day, "pulse", {"summary": "three", "ts": 300})
    append_record(day, "pulse", {"summary": "one", "ts": 100})
    append_record(day, "pulse", {"summary": "two", "ts": 200})

    records = read_records(day, "pulse")
    assert [record["summary"] for record in records] == ["one", "two", "three"]
    assert [record["ts"] for record in records] == [100, 200, 300]


def test_malformed_lines_are_skipped_and_dropped_on_subsequent_append(journal):
    day = "20260611"

    append_record(day, "pulse", {"summary": "valid", "ts": 100})
    path = _accumulator_path(journal, day, "pulse")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")

    records = read_records(day, "pulse")
    assert len(records) == 1
    assert records[0]["summary"] == "valid"
    assert read_latest(day, "pulse")["summary"] == "valid"

    append_record(day, "pulse", {"summary": "second", "ts": 200})

    records = read_records(day, "pulse")
    assert len(records) == 2
    assert "not json" not in path.read_text(encoding="utf-8")


def test_missing_day_reads_do_not_create_directories_or_files(journal):
    day = "20200101"
    path = _accumulator_path(journal, day, "pulse")

    assert read_records(day, "pulse") == []
    assert read_latest(day, "pulse") is None
    assert not path.exists()
    assert not path.parent.exists()


def test_append_record_indexes_under_accumulator_name(journal):
    day = "20260611"

    append_record(
        day,
        "pulse",
        {
            "title": "Focus block",
            "one_sentence": "The owner ran a deep focus block.",
            "full_details": "The morning centered on a deep focus block.",
            "needs_you": [],
            "model": "test-model",
            "generated_at": "2026-06-11T12:00:00Z",
            "ts": 100,
        },
    )

    from solstone.think.indexer.journal import search_journal

    total, results = search_journal("focus", agent="pulse")
    assert total >= 1
    assert any("focus" in hit["text"].lower() for hit in results)

    control_total, control_results = search_journal("focus", agent="steward")
    assert control_total == 0
    assert control_results == []

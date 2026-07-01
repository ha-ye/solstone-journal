# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.observe.processing_record import (
    HANDLER_DESCRIBE,
    REASON_NO_DECODABLE_FRAMES,
    STATE_EMPTY,
    build_processing_record,
)
from solstone.think import backfill_processing_records as mod
from solstone.think.backfill_processing_records import (
    Outcome,
    classify_output,
    run_backfill,
    stamp_empty_record,
)
from solstone.think.cluster import _detect_data_state


def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _seg(
    journal: Path,
    day: str = "20990101",
    stream: str = "archon",
    segkey: str = "090000_300",
) -> Path:
    path = journal / "chronicle" / day / stream / segkey
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_jsonl(path: Path, *records: dict) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _read_header(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def _has_record(path: Path) -> bool:
    return isinstance(_read_header(path).get("_solstone_processing"), dict)


def _seed_screen_header_only(seg_path: Path, name: str = "screen") -> Path:
    (seg_path / f"{name}.webm").write_bytes(b"raw-screen")
    jsonl = seg_path / f"{name}.jsonl"
    _write_jsonl(
        jsonl,
        {
            "raw": f"{name}.webm",
            "first_hash": None,
            "last_hash": None,
            "qualified_count": 0,
        },
    )
    return jsonl


def _seed_audio_header_only(seg_path: Path, name: str = "mic_audio") -> Path:
    (seg_path / f"{name}.flac").write_bytes(b"raw-audio")
    jsonl = seg_path / f"{name}.jsonl"
    _write_jsonl(jsonl, {"raw": f"{name}.flac", "backend": "test"})
    return jsonl


def test_backfills_header_only_screen_output(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    jsonl = _seed_screen_header_only(_seg(journal), "x_screen")

    counts = run_backfill("20990101", commit=True)

    assert counts[Outcome.STAMP_EMPTY] == 1
    header = _read_header(jsonl)
    record = header["_solstone_processing"]
    assert record["state"] == "empty"
    assert record["reason_code"] == "no_decodable_frames"
    assert record["source"] == "backfill"
    assert header["raw"] == "x_screen.webm"
    assert header["qualified_count"] == 0
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 1


def test_backfills_header_only_audio_output(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    jsonl = _seed_audio_header_only(_seg(journal), "mic_audio")

    counts = run_backfill("20990101", commit=True)

    assert counts[Outcome.STAMP_EMPTY] == 1
    record = _read_header(jsonl)["_solstone_processing"]
    assert record["state"] == "empty"
    assert record["reason_code"] == "no_decodable_audio"
    assert record["source"] == "backfill"


def test_chunk_bearing_screen_is_untouched(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    (seg_path / "screen.webm").write_bytes(b"raw")
    jsonl = seg_path / "screen.jsonl"
    _write_jsonl(jsonl, {"raw": "screen.webm"}, {"timestamp": 0, "content": {}})
    before = jsonl.read_bytes()

    outcome, _spec = classify_output("archon", seg_path, jsonl)
    counts = run_backfill("20990101", commit=True)

    assert outcome == Outcome.SKIP_CHUNK_BEARING
    assert counts[Outcome.SKIP_CHUNK_BEARING] == 1
    assert jsonl.read_bytes() == before


def test_existing_record_is_untouched_and_idempotent(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    (seg_path / "screen.webm").write_bytes(b"raw")
    jsonl = seg_path / "screen.jsonl"
    record = build_processing_record(
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_FRAMES,
        handler=HANDLER_DESCRIBE,
        input_size=0,
    )
    _write_jsonl(jsonl, {"raw": "screen.webm", "_solstone_processing": record})
    before = jsonl.read_bytes()

    first = run_backfill("20990101", commit=True)
    second = run_backfill("20990101", commit=True)

    assert first[Outcome.SKIP_HAS_RECORD] == 1
    assert second[Outcome.SKIP_HAS_RECORD] == 1
    assert jsonl.read_bytes() == before


@pytest.mark.parametrize(
    ("marker", "expected_modality"),
    [
        (".analyze_failed_audio", "audio"),
        (".analyzing_screen", "screen"),
    ],
)
def test_marker_outputs_are_untouched(tmp_path, monkeypatch, marker, expected_modality):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    jsonl = (
        _seed_audio_header_only(seg_path)
        if expected_modality == "audio"
        else _seed_screen_header_only(seg_path)
    )
    (seg_path / marker).write_text("{}\n", encoding="utf-8")
    before = jsonl.read_bytes()

    counts = run_backfill("20990101", commit=True)

    assert counts[Outcome.SKIP_MARKER] == 1
    assert jsonl.read_bytes() == before


def test_import_and_extraction_shapes_are_ineligible(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    import_audio = _seg(journal, stream="import.audio")
    (import_audio / "imported_audio.mp3").write_bytes(b"raw")
    imported_jsonl = import_audio / "imported_audio.jsonl"
    _write_jsonl(imported_jsonl, {"raw": "imported_audio.mp3"})

    import_chat = _seg(journal, stream="import.chatgpt", segkey="091000_300")
    conversation = import_chat / "conversation_transcript.jsonl"
    _write_jsonl(conversation, {"imported": {"id": "fixture"}})

    extraction = _seg(journal, segkey="092000_300")
    (extraction / "report_audio.pdf").write_bytes(b"%PDF")
    report = extraction / "report_audio.jsonl"
    _write_jsonl(report, {"raw": "report_audio.pdf", "kind": "document"})

    counts = run_backfill("20990101", commit=True)

    assert counts[Outcome.SKIP_INELIGIBLE] == 3
    assert not _has_record(imported_jsonl)
    assert not _has_record(conversation)
    assert not _has_record(report)


def test_multiple_screen_outputs_preserve_chunks_win_aggregation(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    a = _seed_screen_header_only(seg_path, "a_screen")
    (seg_path / "b_screen.webm").write_bytes(b"raw")
    b = seg_path / "b_screen.jsonl"
    _write_jsonl(b, {"raw": "b_screen.webm"}, {"timestamp": 0, "content": {}})
    before_b = b.read_bytes()

    run_backfill("20990101", commit=True)

    assert _has_record(a)
    assert b.read_bytes() == before_b
    assert _detect_data_state(seg_path)["screen"] == "analyzed"


def test_trailing_junk_non_chunk_rows_are_preserved(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    jsonl = _seed_audio_header_only(seg_path)
    original = '{"raw": "mic_audio.flac"}\nnot json\n{"speaker": "Human"}\n'
    jsonl.write_text(original, encoding="utf-8")

    counts = run_backfill("20990101", commit=True)
    lines = jsonl.read_text(encoding="utf-8").splitlines(keepends=True)

    assert counts[Outcome.STAMP_EMPTY] == 1
    assert json.loads(lines[0])["_solstone_processing"]["source"] == "backfill"
    assert lines[1:] == ["not json\n", '{"speaker": "Human"}\n']


def test_start_row_is_chunk_bearing_and_untouched(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    jsonl = _seed_audio_header_only(seg_path)
    _write_jsonl(jsonl, {"raw": "mic_audio.flac"}, {"start": "00:00:00", "text": "hi"})
    before = jsonl.read_bytes()

    counts = run_backfill("20990101", commit=True)

    assert counts[Outcome.SKIP_CHUNK_BEARING] == 1
    assert jsonl.read_bytes() == before


def test_malformed_header_is_unreadable_and_does_not_raise(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    (seg_path / "screen.webm").write_bytes(b"raw")
    jsonl = seg_path / "screen.jsonl"
    jsonl.write_text("not json\n", encoding="utf-8")

    counts = run_backfill("20990101", commit=True)

    assert counts[Outcome.SKIP_UNREADABLE] == 1
    assert jsonl.read_text(encoding="utf-8") == "not json\n"


def test_atomic_replace_failure_preserves_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    journal = _journal(tmp_path, monkeypatch)
    seg_path = _seg(journal)
    jsonl = _seed_screen_header_only(seg_path)
    before = jsonl.read_bytes()
    _outcome, spec = classify_output("archon", seg_path, jsonl)

    def fail_replace(_src, _dst):
        raise OSError("boom")

    import solstone.think.journal_io.atomic as atomic

    monkeypatch.setattr(atomic.os, "replace", fail_replace)
    with pytest.raises(OSError):
        stamp_empty_record(seg_path, jsonl, spec)

    assert jsonl.read_bytes() == before
    assert json.loads(jsonl.read_text(encoding="utf-8"))
    assert list(seg_path.glob(".tmp_*")) == []


def test_interrupted_commit_can_be_rerun_without_double_writes(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    paths = []
    for index in range(4):
        seg_path = _seg(journal, segkey=f"090{index}00_300")
        paths.append(_seed_screen_header_only(seg_path))

    original_stamp = mod.stamp_empty_record
    calls = 0

    def interrupt_after_two(seg_path, jsonl_path, spec):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("interrupted")
        original_stamp(seg_path, jsonl_path, spec)

    monkeypatch.setattr(mod, "stamp_empty_record", interrupt_after_two)
    with pytest.raises(RuntimeError):
        run_backfill("20990101", commit=True)

    assert sum(1 for path in paths if _has_record(path)) == 2

    monkeypatch.setattr(mod, "stamp_empty_record", original_stamp)
    run_backfill("20990101", commit=True)

    assert all(_has_record(path) for path in paths)
    for path in paths:
        assert path.read_text(encoding="utf-8").count("_solstone_processing") == 1


def test_dry_run_counts_match_commit_counts_and_empty_day_is_zero(
    tmp_path, monkeypatch
):
    journal = _journal(tmp_path, monkeypatch)
    _seed_screen_header_only(_seg(journal, segkey="090000_300"))

    recorded_seg = _seg(journal, segkey="091000_300")
    recorded = _seed_screen_header_only(recorded_seg)
    header = _read_header(recorded)
    header["_solstone_processing"] = build_processing_record(
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_FRAMES,
        handler=HANDLER_DESCRIBE,
        input_size=0,
    )
    _write_jsonl(recorded, header)

    chunk_seg = _seg(journal, segkey="092000_300")
    chunk = _seed_screen_header_only(chunk_seg)
    _write_jsonl(chunk, {"raw": "screen.webm"}, {"timestamp": 0})

    marker_seg = _seg(journal, segkey="093000_300")
    _seed_audio_header_only(marker_seg)
    (marker_seg / ".analyzing_audio").write_text("{}\n", encoding="utf-8")

    _write_jsonl(
        _seg(journal, stream="import.chatgpt").joinpath("audio.jsonl"), {"raw": "x"}
    )

    unreadable_seg = _seg(journal, segkey="094000_300")
    (unreadable_seg / "screen.webm").write_bytes(b"raw")
    (unreadable_seg / "screen.jsonl").write_text("[1, 2]\n", encoding="utf-8")

    dry_counts = run_backfill("20990101", commit=False)
    commit_counts = run_backfill("20990101", commit=True)

    assert dry_counts == commit_counts
    assert run_backfill("20990102", commit=False) == {outcome: 0 for outcome in Outcome}


def test_build_processing_record_source_is_backfill_only():
    without_source = build_processing_record(
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_FRAMES,
        handler=HANDLER_DESCRIBE,
        input_size=0,
    )
    with_source = build_processing_record(
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_FRAMES,
        handler=HANDLER_DESCRIBE,
        input_size=0,
        source="backfill",
    )

    assert "source" not in without_source
    assert with_source["source"] == "backfill"

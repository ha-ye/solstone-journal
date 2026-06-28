# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for deleting observer source streams."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import solstone.apps.observer.share_delete as share_delete
from solstone.apps.observer.share_delete import (
    LOCATION_STREAM,
    delete_source_stream,
)
from solstone.apps.observer.source_discovery import (
    LocationSource,
    find_location_sources,
)
from solstone.apps.observer.utils import (
    append_history_record,
    get_hist_dir,
    load_history,
    prune_history_by_stream,
    save_observer,
)
from solstone.think.streams import update_stream, write_segment_stream


def _set_journal(tmp_path, monkeypatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return journal


def _observer_prefix(prefix: str, name: str = "share-observer") -> str:
    key = prefix + ("f" * (64 - len(prefix)))
    assert save_observer(
        {
            "key": key,
            "name": name,
            "created_at": 1,
            "last_seen": None,
            "last_segment": None,
            "enabled": True,
            "stats": {
                "segments_received": 0,
                "bytes_received": 0,
            },
        }
    )
    return key[:8]


def _write_segment(
    journal: Path,
    day: str,
    segment: str,
    stream: str,
    *,
    original_name: str = "doc.pdf",
    derived_name: str = "doc.jsonl",
    history_prefix: str | None = None,
) -> Path:
    seg_dir = journal / "chronicle" / day / stream / segment
    seg_dir.mkdir(parents=True)
    (seg_dir / original_name).write_bytes(b"original")
    (seg_dir / derived_name).write_text('{"text": "derived"}\n', encoding="utf-8")
    (seg_dir / "item.json").write_text("{}\n", encoding="utf-8")
    state = update_stream(
        stream,
        day,
        segment,
        type="import" if stream.startswith("import.") else "observer",
    )
    write_segment_stream(
        seg_dir,
        stream,
        state["prev_day"],
        state["prev_segment"],
        state["seq"],
    )
    if history_prefix is not None:
        append_history_record(
            history_prefix,
            day,
            {
                "ts": 1,
                "segment": segment,
                "stream": stream,
                "files": [{"written": original_name}],
            },
        )
    return seg_dir


def _write_location_segment(
    journal: Path,
    day: str,
    segment: str,
    *,
    history_prefix: str | None = None,
) -> Path:
    seg_dir = journal / "chronicle" / day / LOCATION_STREAM / segment
    seg_dir.mkdir(parents=True)
    (seg_dir / "location.jsonl").write_text(
        '{"lat": 12.34, "lon": 56.78}\n',
        encoding="utf-8",
    )
    state = update_stream(LOCATION_STREAM, day, segment, type="observer")
    write_segment_stream(
        seg_dir,
        LOCATION_STREAM,
        state["prev_day"],
        state["prev_segment"],
        state["seq"],
    )
    if history_prefix is not None:
        append_history_record(
            history_prefix,
            day,
            {
                "ts": 1,
                "segment": segment,
                "stream": LOCATION_STREAM,
                "files": [{"written": "location.jsonl"}],
            },
        )
    return seg_dir


def _write_mixed_mobile_segment(
    journal: Path,
    day: str,
    segment: str,
    stream: str = "pixel",
    *,
    history_prefix: str | None = None,
    location_payload: str = '{"lat": 1.0}\n',
    audio_bytes: bytes = b"AUDIODATA",
) -> Path:
    seg_dir = journal / "chronicle" / day / stream / segment
    seg_dir.mkdir(parents=True)
    (seg_dir / "location.jsonl").write_text(location_payload, encoding="utf-8")
    (seg_dir / "audio.m4a").write_bytes(audio_bytes)
    (seg_dir / "audio.jsonl").write_text('{"text":"x"}\n', encoding="utf-8")
    state = update_stream(stream, day, segment, type="observer")
    write_segment_stream(
        seg_dir,
        stream,
        state["prev_day"],
        state["prev_segment"],
        state["seq"],
    )
    if history_prefix is not None:
        append_history_record(
            history_prefix,
            day,
            {
                "ts": 1,
                "segment": segment,
                "stream": stream,
                "files": [
                    {"written": "audio.m4a"},
                    {"written": "location.jsonl"},
                    {"written": "screen.mp4"},
                ],
            },
        )
    return seg_dir


def test_delete_source_stream_rejects_unsupported_stream():
    with pytest.raises(ValueError):
        delete_source_stream("import.audio")
    with pytest.raises(ValueError):
        delete_source_stream("import.share")


def test_removes_location_across_two_days_leaves_other_streams(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("loc0010")

    location_day_1 = _write_location_segment(
        journal,
        "20260106",
        "090000_300",
        history_prefix=prefix,
    )
    location_day_2 = _write_location_segment(
        journal,
        "20260107",
        "100000_300",
        history_prefix=prefix,
    )
    apple_segment = _write_segment(
        journal,
        "20260106",
        "120000_300",
        "import.apple",
    )

    receipt = delete_source_stream(LOCATION_STREAM)

    assert not location_day_1.exists()
    assert not location_day_2.exists()
    assert not (journal / "chronicle" / "20260106" / LOCATION_STREAM).exists()
    assert not (journal / "chronicle" / "20260107" / LOCATION_STREAM).exists()
    assert not (journal / "streams" / f"{LOCATION_STREAM}.json").exists()
    assert apple_segment.exists()
    assert (journal / "streams" / "import.apple.json").exists()
    assert receipt["removed"] == {
        "originals": 2,
        "segments": 2,
        "mixed_segments": 0,
        "in_segment_derived": 0,
        "index_chunks": 0,
        "stream_identity": 1,
        "history_rows": 2,
        "days": 2,
    }
    assert receipt["target"]["stream"] == LOCATION_STREAM


def test_location_non_attributable_aggregate_is_not_confirmed(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    location_segment = _write_location_segment(journal, "20260109", "090000_300")
    aggregate = journal / "facets" / "work" / "entities" / "20260109.jsonl"
    aggregate.parent.mkdir(parents=True)
    (journal / "facets" / "work" / "facet.json").write_text(
        json.dumps({"title": "Work"}),
        encoding="utf-8",
    )
    aggregate.write_bytes(b"\xff")

    receipt = delete_source_stream(LOCATION_STREAM)

    assert aggregate.exists()
    assert not location_segment.exists()
    assert receipt["not_confirmed"] == [
        {
            "what": "work 2026-01-09: people and topics",
            "plain_reason": "This was merged into this day's people and topics; can't remove just this source's part.",
        }
    ]


def test_location_idempotent_zero_count(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    zero_removed = {
        "originals": 0,
        "segments": 0,
        "mixed_segments": 0,
        "in_segment_derived": 0,
        "index_chunks": 0,
        "stream_identity": 0,
        "history_rows": 0,
        "days": 0,
    }

    absent = delete_source_stream(LOCATION_STREAM)
    assert absent["removed"] == zero_removed
    assert absent["not_confirmed"] == []
    assert absent["not_removed"] == []

    _write_location_segment(journal, "20260110", "090000_300")

    first = delete_source_stream(LOCATION_STREAM)
    second = delete_source_stream(LOCATION_STREAM)

    assert first["removed"]["segments"] == 1
    assert second["removed"] == zero_removed
    assert second["not_confirmed"] == []
    assert second["not_removed"] == []


def test_location_failure_uses_location_in_not_removed(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("loc0030")
    seg_dir = _write_location_segment(
        journal,
        "20260111",
        "090000_300",
        history_prefix=prefix,
    )

    def fail_rmtree(path):
        raise OSError("permission denied")

    monkeypatch.setattr(share_delete.shutil, "rmtree", fail_rmtree)

    receipt = delete_source_stream(LOCATION_STREAM)

    assert seg_dir.exists()
    assert receipt["removed"]["segments"] == 0
    assert receipt["not_removed"] == [
        {
            "what": "location 2026-01-11 090000_300: segment",
            "plain_reason": "This segment could not be removed from disk. Try again after checking file permissions.",
        }
    ]


def test_mixed_segment_removes_only_location(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("loc0040")
    seg_dir = _write_mixed_mobile_segment(
        journal,
        "20260112",
        "090000_300",
        history_prefix=prefix,
    )

    receipt = delete_source_stream(LOCATION_STREAM)

    assert not (seg_dir / "location.jsonl").exists()
    assert (seg_dir / "audio.m4a").exists()
    assert (seg_dir / "audio.jsonl").exists()
    assert (seg_dir / "stream.json").exists()
    assert seg_dir.exists()
    assert receipt["removed"]["mixed_segments"] == 1
    assert receipt["removed"]["segments"] == 0
    assert receipt["removed"]["originals"] == 1
    assert receipt["removed"]["history_rows"] == 0
    assert (journal / "streams" / "pixel.json").exists()
    assert load_history(prefix, "20260112")
    assert receipt["not_confirmed"] == [
        {
            "what": "pixel: import history",
            "plain_reason": "This source was imported together with others in one record; its history entry can't be removed on its own.",
        }
    ]


def test_mixed_segment_idempotent(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    seg_dir = _write_mixed_mobile_segment(journal, "20260113", "090000_300")

    delete_source_stream(LOCATION_STREAM)
    second = delete_source_stream(LOCATION_STREAM)

    assert second["removed"]["mixed_segments"] == 0
    assert second["removed"]["originals"] == 0
    assert second["not_removed"] == []
    assert (seg_dir / "audio.m4a").exists()


def test_find_location_sources_sees_then_stops(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    _write_mixed_mobile_segment(journal, "20260114", "090000_300")

    sources = find_location_sources()

    assert len(sources) == 1
    assert isinstance(sources[0], LocationSource)
    assert sources[0].is_mixed is True
    assert sources[0].stream == "pixel"

    delete_source_stream(LOCATION_STREAM)

    assert find_location_sources() == []


def test_mobile_location_only_segment_removed(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("loc0050")
    day = "20260115"
    segment = "090000_300"
    stream = "pixel"
    seg_dir = journal / "chronicle" / day / stream / segment
    seg_dir.mkdir(parents=True)
    (seg_dir / "location.jsonl").write_text('{"lat": 1.0}\n', encoding="utf-8")
    state = update_stream(stream, day, segment, type="observer")
    write_segment_stream(
        seg_dir,
        stream,
        state["prev_day"],
        state["prev_segment"],
        state["seq"],
    )
    append_history_record(
        prefix,
        day,
        {
            "ts": 1,
            "segment": segment,
            "stream": stream,
            "files": [{"written": "location.jsonl"}],
        },
    )

    receipt = delete_source_stream(LOCATION_STREAM)

    assert not seg_dir.exists()
    assert receipt["removed"]["segments"] == 1
    assert receipt["removed"]["mixed_segments"] == 0
    assert (journal / "streams" / "pixel.json").exists()
    assert {
        "what": "pixel: import history",
        "plain_reason": "This source was imported together with others in one record; its history entry can't be removed on its own.",
    } in receipt["not_confirmed"]


def test_no_artifact_values_leak(tmp_path, monkeypatch, caplog):
    journal = _set_journal(tmp_path, monkeypatch)
    _write_mixed_mobile_segment(
        journal,
        "20260116",
        "090000_300",
        location_payload='{"secret":"LOCSENTINEL123"}\n',
        audio_bytes=b"AUDIOSENTINEL456",
    )

    with caplog.at_level(logging.INFO):
        receipt = delete_source_stream(LOCATION_STREAM)

    receipt_json = json.dumps(receipt)
    assert "LOCSENTINEL123" not in receipt_json
    assert "AUDIOSENTINEL456" not in receipt_json
    assert "LOCSENTINEL123" not in caplog.text
    assert "AUDIOSENTINEL456" not in caplog.text


def test_prune_history_by_stream_across_prefixes(tmp_path, monkeypatch):
    _set_journal(tmp_path, monkeypatch)
    prefix_1 = _observer_prefix("hist0010", "first")
    prefix_2 = _observer_prefix("hist0020", "second")

    append_history_record(
        prefix_1,
        "20260106",
        {"ts": 1, "segment": "090000_300", "stream": "import.image", "files": []},
    )
    append_history_record(
        prefix_1,
        "20260106",
        {"ts": 2, "segment": "091000_300", "stream": "import.apple", "files": []},
    )
    append_history_record(
        prefix_2,
        "20260106",
        {"ts": 3, "segment": "092000_300", "stream": "import.image", "files": []},
    )

    assert prune_history_by_stream("import.image") == 2
    assert load_history(prefix_1, "20260106") == [
        {"ts": 2, "segment": "091000_300", "stream": "import.apple", "files": []}
    ]
    assert load_history(prefix_2, "20260106") == []
    assert prune_history_by_stream("import.image") == 0
    assert (get_hist_dir(prefix_2, ensure_exists=False) / "20260106.jsonl").read_text(
        encoding="utf-8"
    ) == ""

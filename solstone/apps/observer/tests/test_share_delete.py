# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for deleting observer source streams."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import solstone.apps.observer.share_delete as share_delete
from solstone.apps.observer.share_delete import (
    LOCATION_STREAM,
    SHARE_STREAM,
    delete_source_stream,
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


def test_removes_across_two_days_leaves_other_stream(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("share001")

    share_day_1 = _write_segment(
        journal,
        "20260101",
        "090000_300",
        SHARE_STREAM,
        original_name="doc.pdf",
        derived_name="doc.jsonl",
        history_prefix=prefix,
    )
    share_day_2 = _write_segment(
        journal,
        "20260102",
        "100000_300",
        SHARE_STREAM,
        original_name="photo.png",
        derived_name="photo.jsonl",
        history_prefix=prefix,
    )
    other_segment = _write_segment(
        journal,
        "20260101",
        "110000_300",
        "import.apple",
        original_name="apple.pdf",
        derived_name="apple.jsonl",
    )

    receipt = delete_source_stream(SHARE_STREAM)

    assert not share_day_1.exists()
    assert not share_day_2.exists()
    assert not (journal / "chronicle" / "20260101" / SHARE_STREAM).exists()
    assert not (journal / "chronicle" / "20260102" / SHARE_STREAM).exists()
    assert other_segment.exists()
    assert not (journal / "streams" / f"{SHARE_STREAM}.json").exists()
    assert (journal / "streams" / "import.apple.json").exists()
    assert receipt["removed"] == {
        "originals": 2,
        "segments": 2,
        "in_segment_derived": 2,
        "index_chunks": 0,
        "stream_identity": 1,
        "history_rows": 2,
    }
    assert receipt["target"]["stream"] == SHARE_STREAM
    assert receipt["target"]["journal"] == str(journal.resolve())
    assert receipt["not_confirmed"] == []
    assert receipt["not_removed"] == []
    assert receipt["backup_hosted"] == "not confirmed"


def test_non_attributable_aggregate_is_not_confirmed(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    _write_segment(journal, "20260103", "090000_300", SHARE_STREAM)
    aggregate = journal / "facets" / "work" / "entities" / "20260103.jsonl"
    aggregate.parent.mkdir(parents=True)
    (journal / "facets" / "work" / "facet.json").write_text(
        json.dumps({"title": "Work"}),
        encoding="utf-8",
    )
    aggregate.write_bytes(b"\xff")

    receipt = delete_source_stream(SHARE_STREAM)

    assert aggregate.exists()
    assert receipt["removed"]["segments"] == 1
    assert receipt["removed"]["in_segment_derived"] == 1
    assert receipt["not_confirmed"] == [
        {
            "what": "work 2026-01-03: people and topics",
            "plain_reason": "This was merged into this day's people and topics; can't remove just this source's part.",
        }
    ]
    assert receipt["not_removed"] == []


def test_surface_failure_goes_to_not_removed(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("share002")
    seg_dir = _write_segment(
        journal,
        "20260104",
        "090000_300",
        SHARE_STREAM,
        history_prefix=prefix,
    )

    def fail_rmtree(path):
        raise OSError("permission denied")

    monkeypatch.setattr(share_delete.shutil, "rmtree", fail_rmtree)

    receipt = delete_source_stream(SHARE_STREAM)

    assert seg_dir.exists()
    assert receipt["removed"]["segments"] == 0
    assert receipt["removed"]["stream_identity"] == 1
    assert receipt["removed"]["history_rows"] == 1
    assert receipt["not_confirmed"] == []
    assert receipt["not_removed"] == [
        {
            "what": "import.share 2026-01-04 090000_300: segment",
            "plain_reason": "This segment could not be removed from disk. Try again after checking file permissions.",
        }
    ]


def test_backup_status_and_idempotent_second_run(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    _write_segment(journal, "20260105", "090000_300", SHARE_STREAM)

    first = delete_source_stream(SHARE_STREAM)
    second = delete_source_stream(SHARE_STREAM)

    assert first["backup_hosted"] == "not confirmed"
    assert second["backup_hosted"] == "not confirmed"
    assert second["removed"] == {
        "originals": 0,
        "segments": 0,
        "in_segment_derived": 0,
        "index_chunks": 0,
        "stream_identity": 0,
        "history_rows": 0,
    }
    assert second["not_confirmed"] == []
    assert second["not_removed"] == []


def test_delete_source_stream_rejects_unsupported_stream():
    with pytest.raises(ValueError):
        delete_source_stream("import.audio")


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
    share_segment = _write_segment(
        journal,
        "20260106",
        "110000_300",
        SHARE_STREAM,
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
    assert share_segment.exists()
    assert (journal / "streams" / f"{SHARE_STREAM}.json").exists()
    assert apple_segment.exists()
    assert (journal / "streams" / "import.apple.json").exists()
    assert receipt["removed"] == {
        "originals": 2,
        "segments": 2,
        "in_segment_derived": 0,
        "index_chunks": 0,
        "stream_identity": 1,
        "history_rows": 2,
        "days": 2,
    }
    assert receipt["target"]["stream"] == LOCATION_STREAM


def test_delete_import_share_leaves_location(tmp_path, monkeypatch):
    journal = _set_journal(tmp_path, monkeypatch)
    prefix = _observer_prefix("loc0020")
    share_segment = _write_segment(
        journal,
        "20260108",
        "090000_300",
        SHARE_STREAM,
        history_prefix=prefix,
    )
    location_segment = _write_location_segment(
        journal,
        "20260108",
        "100000_300",
        history_prefix=prefix,
    )

    receipt = delete_source_stream(SHARE_STREAM)

    assert not share_segment.exists()
    assert location_segment.exists()
    assert (journal / "streams" / f"{LOCATION_STREAM}.json").exists()
    assert load_history(prefix, "20260108") == [
        {
            "ts": 1,
            "segment": "100000_300",
            "stream": LOCATION_STREAM,
            "files": [{"written": "location.jsonl"}],
        }
    ]
    assert receipt["target"]["stream"] == SHARE_STREAM


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


def test_prune_history_by_stream_across_prefixes(tmp_path, monkeypatch):
    _set_journal(tmp_path, monkeypatch)
    prefix_1 = _observer_prefix("hist0010", "first")
    prefix_2 = _observer_prefix("hist0020", "second")

    append_history_record(
        prefix_1,
        "20260106",
        {"ts": 1, "segment": "090000_300", "stream": SHARE_STREAM, "files": []},
    )
    append_history_record(
        prefix_1,
        "20260106",
        {"ts": 2, "segment": "091000_300", "stream": "import.apple", "files": []},
    )
    append_history_record(
        prefix_2,
        "20260106",
        {"ts": 3, "segment": "092000_300", "stream": SHARE_STREAM, "files": []},
    )

    assert prune_history_by_stream(SHARE_STREAM) == 2
    assert load_history(prefix_1, "20260106") == [
        {"ts": 2, "segment": "091000_300", "stream": "import.apple", "files": []}
    ]
    assert load_history(prefix_2, "20260106") == []
    assert prune_history_by_stream(SHARE_STREAM) == 0
    assert (get_hist_dir(prefix_2, ensure_exists=False) / "20260106.jsonl").read_text(
        encoding="utf-8"
    ) == ""

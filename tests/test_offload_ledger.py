# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from solstone.think.offload_ledger import (
    EVENT_OFFLOAD,
    EVENT_RESTORE,
    OffloadFile,
    append_offload_event,
    append_restore_event,
    ledger_path_for_day,
    summarize_day,
    summarize_journal,
    summarize_segment,
)

DAY = "20260101"
STREAM = "archon"
SEGMENT = "120000_300"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _is_ledger_fd_fsynced(
    monkeypatch: pytest.MonkeyPatch, ledger_path: Path, writer: Callable[[], None]
) -> bool:
    calls = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        fd_stat = os.fstat(fd)
        try:
            ledger_stat = ledger_path.stat()
        except FileNotFoundError:
            ledger_stat = None
        if (
            ledger_stat is not None
            and stat.S_ISREG(fd_stat.st_mode)
            and (fd_stat.st_dev, fd_stat.st_ino)
            == (ledger_stat.st_dev, ledger_stat.st_ino)
        ):
            calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("solstone.think.journal_io.append.os.fsync", spy)
    writer()
    return bool(calls)


def _write_plain_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        handle.write((json.dumps(record) + "\n").encode("utf-8"))
        handle.flush()


def _offload_record(
    *,
    day: str,
    stream: str,
    segment: str,
    snapshot_id: str,
    size: int,
    name: str = "audio.wav",
    sha256: str = SHA_A,
    time: int = 1,
) -> dict:
    return {
        "event_kind": EVENT_OFFLOAD,
        "time": time,
        "day": day,
        "stream": stream,
        "segment": segment,
        "snapshot_id": snapshot_id,
        "files": [{"name": name, "bytes": size, "sha256": sha256}],
    }


def _restore_record(
    *,
    day: str,
    stream: str,
    segment: str,
    time: int = 1,
) -> dict:
    return {
        "event_kind": EVENT_RESTORE,
        "time": time,
        "day": day,
        "stream": stream,
        "segment": segment,
    }


def test_offload_append_writes_media_day_ledger_and_segment_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    append_offload_event(
        day=DAY,
        stream=STREAM,
        segment=SEGMENT,
        snapshot_id="snap-1",
        files=[
            OffloadFile(name="audio.wav", bytes=10, sha256=SHA_A),
            OffloadFile(name="screen.mp4", bytes=20, sha256=SHA_B),
        ],
        time=10,
    )

    ledger_path = ledger_path_for_day(DAY)
    assert ledger_path == tmp_path / "health" / "offload" / f"{DAY}.jsonl"
    record = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    assert "schema_version" not in record
    assert record["event_kind"] == EVENT_OFFLOAD
    assert record["snapshot_id"] == "snap-1"
    assert record["files"] == [
        {"name": "audio.wav", "bytes": 10, "sha256": SHA_A},
        {"name": "screen.mp4", "bytes": 20, "sha256": SHA_B},
    ]
    assert "hash" not in record["files"][0]

    summary = summarize_segment(DAY, STREAM, SEGMENT)
    assert summary.currently_offloaded is True
    assert summary.snapshot_id == "snap-1"
    assert summary.offloaded_bytes == 30
    assert summary.offloaded_file_count == 2
    assert summary.files[0].sha256 == SHA_A


def test_restore_and_reoffload_fold_by_append_order_not_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    append_offload_event(
        day=DAY,
        stream=STREAM,
        segment=SEGMENT,
        snapshot_id="snap-old",
        files=[OffloadFile(name="audio.wav", bytes=10, sha256=SHA_A)],
        time=300,
    )
    append_restore_event(day=DAY, stream=STREAM, segment=SEGMENT, time=200)

    restored = summarize_segment(DAY, STREAM, SEGMENT)
    assert restored.currently_offloaded is False
    assert restored.offloaded_bytes == 0
    assert restored.snapshot_id is None

    append_offload_event(
        day=DAY,
        stream=STREAM,
        segment=SEGMENT,
        snapshot_id="snap-new",
        files=[OffloadFile(name="audio.wav", bytes=11, sha256=SHA_B)],
        time=100,
    )

    reoffloaded = summarize_segment(DAY, STREAM, SEGMENT)
    assert reoffloaded.currently_offloaded is True
    assert reoffloaded.snapshot_id == "snap-new"
    assert reoffloaded.offloaded_bytes == 11


def test_append_fsyncs_ledger_fd_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    ledger_path = ledger_path_for_day(DAY)

    with monkeypatch.context() as context:
        assert _is_ledger_fd_fsynced(
            context,
            ledger_path,
            lambda: append_offload_event(
                day=DAY,
                stream=STREAM,
                segment=SEGMENT,
                snapshot_id="snap-1",
                files=[OffloadFile(name="audio.wav", bytes=10, sha256=SHA_A)],
                time=1,
            ),
        )

    plain_path = tmp_path / "health" / "offload" / "plain.jsonl"
    with monkeypatch.context() as context:
        assert not _is_ledger_fd_fsynced(
            context,
            plain_path,
            lambda: _write_plain_jsonl(
                plain_path,
                _offload_record(
                    day=DAY,
                    stream=STREAM,
                    segment=SEGMENT,
                    snapshot_id="plain",
                    size=10,
                ),
            ),
        )


def test_day_and_journal_summaries_fold_mixed_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    append_offload_event(
        day="20260101",
        stream=STREAM,
        segment="120000_300",
        snapshot_id="full",
        files=[
            OffloadFile(name="a.wav", bytes=10, sha256=SHA_A),
            OffloadFile(name="b.mp4", bytes=5, sha256=SHA_B),
        ],
        time=1,
    )
    append_offload_event(
        day="20260102",
        stream=STREAM,
        segment="120000_300",
        snapshot_id="restored",
        files=[OffloadFile(name="a.wav", bytes=20, sha256=SHA_A)],
        time=1,
    )
    append_restore_event(day="20260102", stream=STREAM, segment="120000_300", time=2)
    append_offload_event(
        day="20260103",
        stream=STREAM,
        segment="120000_300",
        snapshot_id="mixed-kept",
        files=[OffloadFile(name="a.wav", bytes=30, sha256=SHA_A)],
        time=1,
    )
    append_offload_event(
        day="20260103",
        stream=STREAM,
        segment="121000_300",
        snapshot_id="mixed-restored",
        files=[OffloadFile(name="b.wav", bytes=40, sha256=SHA_B)],
        time=1,
    )
    append_restore_event(day="20260103", stream=STREAM, segment="121000_300", time=2)

    assert summarize_day("20260101").offloaded_bytes == 15
    assert summarize_day("20260102").offloaded_bytes == 0
    mixed = summarize_day("20260103")
    assert mixed.offloaded_bytes == 30
    assert mixed.offloaded_segments == 1

    journal = summarize_journal()
    assert [day.day for day in journal.days] == ["20260101", "20260102", "20260103"]
    assert journal.offloaded_bytes == 45
    assert journal.offloaded_file_count == 3
    assert journal.offloaded_segments == 2
    assert journal.offloaded_days == 2


def test_malformed_line_warns_counts_skipped_and_keeps_valid_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    ledger_path = ledger_path_for_day(DAY)
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "\n".join(
            [
                json.dumps(
                    _offload_record(
                        day=DAY,
                        stream=STREAM,
                        segment="120000_300",
                        snapshot_id="snap-1",
                        size=10,
                    )
                ),
                "{bad",
                json.dumps(
                    _offload_record(
                        day=DAY,
                        stream=STREAM,
                        segment="121000_300",
                        snapshot_id="snap-2",
                        size=20,
                        sha256=SHA_B,
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="solstone.think.offload_ledger"):
        summary = summarize_day(DAY)

    assert summary.offloaded_bytes == 30
    assert summary.skipped_records == 1
    assert any(str(ledger_path) in record.message for record in caplog.records)
    assert any("malformed" in record.message for record in caplog.records)


def test_null_time_record_is_skipped_not_fabricated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    ledger_path = ledger_path_for_day(DAY)
    invalid = _offload_record(
        day=DAY,
        stream=STREAM,
        segment="120000_300",
        snapshot_id="bad-time",
        size=20,
    )
    invalid["time"] = None
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        "\n".join(
            [
                json.dumps(
                    _offload_record(
                        day=DAY,
                        stream=STREAM,
                        segment="121000_300",
                        snapshot_id="valid",
                        size=10,
                    )
                ),
                json.dumps(invalid),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="solstone.think.offload_ledger"):
        summary = summarize_day(DAY)

    assert summary.offloaded_bytes == 10
    assert summary.skipped_records == 1
    assert any(str(ledger_path) in record.message for record in caplog.records)


def test_undecodable_ledger_degrades_without_clean_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    ledger_path = ledger_path_for_day(DAY)
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_bytes(b"\xff")

    with caplog.at_level(logging.WARNING, logger="solstone.think.offload_ledger"):
        summary = summarize_day(DAY)

    assert summary.offloaded_bytes == 0
    assert summary.degraded is True
    assert summary.unreadable_ledgers == (str(ledger_path),)
    assert any(str(ledger_path) in record.message for record in caplog.records)


def test_absent_or_empty_ledger_is_clean_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    missing = summarize_journal()
    assert missing.offloaded_bytes == 0
    assert missing.skipped_records == 0
    assert missing.degraded is False

    ledger_path = ledger_path_for_day(DAY)
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text("", encoding="utf-8")

    empty = summarize_day(DAY)
    assert empty.offloaded_bytes == 0
    assert empty.skipped_records == 0
    assert empty.degraded is False

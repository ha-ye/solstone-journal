# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from solstone.think import catchup_state

DAY = "20260101"
CMD_DAILY = ["journal", "think", "-v", "--day", DAY]
CMD_FROM_SCRATCH = ["journal", "think", "-v", "--day", DAY, "--from-scratch"]
CMD_SEGMENT = ["journal", "think", "-v", "--day", DAY, "--segment", "120000_300"]


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


def _day_dir(journal: Path, day: str = DAY) -> Path:
    path = journal / "chronicle" / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def _segment(
    journal: Path,
    day: str = DAY,
    stream: str = "default",
    segment: str = "120000_300",
) -> Path:
    path = _day_dir(journal, day) / stream / segment
    path.mkdir(parents=True, exist_ok=True)
    return path


def _touch_marker(journal: Path, day: str, name: str, mtime: float) -> Path:
    path = _day_dir(journal, day) / "health" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (mtime, mtime))
    return path


def _state_file(journal: Path) -> Path:
    path = journal / "health" / "catchup-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_state(journal: Path, entries: dict[str, dict]) -> None:
    _state_file(journal).write_text(
        json.dumps({"version": catchup_state.STATE_VERSION, "entries": entries}),
        encoding="utf-8",
    )


def _read_entries(journal: Path) -> dict:
    return json.loads(_state_file(journal).read_text(encoding="utf-8"))["entries"]


def _record(
    day: str,
    kind: str,
    *,
    attempts: int = 1,
    consecutive: int = 0,
    last_outcome: str = "",
    next_retry_at: float = 0,
    entered_backoff_at: float | None = None,
    active: dict | None = None,
    fingerprint: str | None = None,
) -> dict:
    return {
        "day": day,
        "command_kind": kind,
        "attempts": attempts,
        "consecutive_non_completion": consecutive,
        "last_attempt_at": 0,
        "last_outcome": last_outcome,
        "next_retry_at": next_retry_at,
        "entered_backoff_at": entered_backoff_at,
        "notified_at": entered_backoff_at,
        "fingerprint": fingerprint,
        "active": active,
    }


def test_derive_command_kind_and_extract_day():
    assert (
        catchup_state.derive_command_kind(CMD_DAILY) == catchup_state.KIND_DAILY_CATCHUP
    )
    assert (
        catchup_state.derive_command_kind(CMD_FROM_SCRATCH)
        == catchup_state.KIND_DAILY_FROM_SCRATCH
    )
    assert catchup_state.derive_command_kind(CMD_SEGMENT) == catchup_state.KIND_SEGMENT
    assert (
        catchup_state.derive_command_kind(
            ["journal", "think", "--day", DAY, "--segments"]
        )
        == catchup_state.KIND_SEGMENT
    )
    for flag in ("--flush", "--activity", "--weekly", "--cadence"):
        assert (
            catchup_state.derive_command_kind(["journal", "think", "--day", DAY, flag])
            is None
        )
    assert catchup_state.derive_command_kind(["journal", "think"]) is None
    assert catchup_state.extract_day(CMD_DAILY) == DAY
    assert catchup_state.extract_day(["journal", "think", "--day"]) is None


def test_raw_input_fingerprint_stability_and_allowlist(journal):
    first = _segment(journal)
    (first / "audio.jsonl").write_text('{"raw":"a.flac"}\n', encoding="utf-8")
    (first / "screen.webm").write_bytes(b"aa")
    (first / "stream.json").write_text("{}\n", encoding="utf-8")
    talents = first / "talents"
    talents.mkdir()
    (talents / "x.md").write_text("derived\n", encoding="utf-8")
    (first / ".analyzing_screen").write_text("busy\n", encoding="utf-8")
    (_day_dir(journal) / "health").mkdir(exist_ok=True)
    (_day_dir(journal) / "health" / "segment.jsonl").write_text(
        "derived\n", encoding="utf-8"
    )

    original = catchup_state.read_raw_input_fingerprint(DAY)
    assert catchup_state.read_raw_input_fingerprint(DAY) == original

    (first / "stream.json").write_text('{"stream":"default"}\n', encoding="utf-8")
    (talents / "x.md").write_text("changed\n", encoding="utf-8")
    (first / ".analyzing_screen").write_text("changed\n", encoding="utf-8")
    (_day_dir(journal) / "health" / "segment.jsonl").write_text(
        "changed\n", encoding="utf-8"
    )
    assert catchup_state.read_raw_input_fingerprint(DAY) == original

    (first / "audio.jsonl").write_text('{"raw":"a.flac"}\n', encoding="utf-8")
    assert catchup_state.read_raw_input_fingerprint(DAY) == original

    (first / "screen.webm").write_bytes(b"bb")
    assert catchup_state.read_raw_input_fingerprint(DAY) == original

    (first / "screen.webm").write_bytes(b"bbb")
    media_changed = catchup_state.read_raw_input_fingerprint(DAY)
    assert media_changed != original

    (first / "audio.jsonl").write_text('{"raw":"different.flac"}\n', encoding="utf-8")
    raw_changed = catchup_state.read_raw_input_fingerprint(DAY)
    assert raw_changed != media_changed

    second = _segment(journal, segment="120500_300")
    (second / "conversation_transcript.jsonl").write_text("{}\n", encoding="utf-8")
    assert catchup_state.read_raw_input_fingerprint(DAY) != raw_changed


def test_raw_input_fingerprint_skips_file_deleted_mid_scan(journal, monkeypatch):
    first = _segment(journal)
    deleted = first / "audio.jsonl"
    survivor = first / "screen.jsonl"
    deleted.write_text("deleted\n", encoding="utf-8")
    survivor.write_text("survivor\n", encoding="utf-8")
    real_sha256 = catchup_state._file_sha256

    def flaky_sha256(path: Path) -> str:
        if path == deleted:
            raise OSError("deleted")
        return real_sha256(path)

    monkeypatch.setattr(catchup_state, "_file_sha256", flaky_sha256)

    digest = catchup_state.read_raw_input_fingerprint(DAY)

    expected_entries = [
        [
            survivor.relative_to(_day_dir(journal)).as_posix(),
            hashlib.sha256(survivor.read_bytes()).hexdigest(),
        ]
    ]
    payload = json.dumps(expected_entries, separators=(",", ":"), ensure_ascii=True)
    assert digest == hashlib.sha256(payload.encode()).hexdigest()
    assert catchup_state.read_raw_input_fingerprint(DAY) == digest


def test_record_attempt_sets_active_and_resets_backoff_on_fingerprint_change(journal):
    _segment(journal).joinpath("audio.jsonl").write_text("{}\n", encoding="utf-8")
    _touch_marker(journal, DAY, "stream.updated", 100)
    marker = _touch_marker(journal, DAY, "daily.updated", 123)
    key = f"{DAY}:{catchup_state.KIND_DAILY_CATCHUP}"
    _write_state(
        journal,
        {
            key: _record(
                DAY,
                catchup_state.KIND_DAILY_CATCHUP,
                attempts=5,
                consecutive=3,
                next_retry_at=9999,
                entered_backoff_at=10,
                fingerprint="old",
            )
        },
    )

    catchup_state.record_attempt(CMD_DAILY, DAY, "ref-1", started_at=200)

    record = catchup_state.read_day_record(DAY, catchup_state.KIND_DAILY_CATCHUP)
    assert record["attempts"] == 6
    assert record["consecutive_non_completion"] == 0
    assert record["entered_backoff_at"] is None
    assert record["notified_at"] is None
    assert record["next_retry_at"] == 0
    assert record["fingerprint"] != "old"
    assert record["active"] == {
        "ref": "ref-1",
        "started_at": 200,
        "marker_mtime_at_start": marker.stat().st_mtime,
    }


def test_record_outcome_uses_daily_marker_delta(journal):
    _segment(journal).joinpath("audio.jsonl").write_text("{}\n", encoding="utf-8")

    catchup_state.record_attempt(CMD_DAILY, DAY, "complete-new", started_at=10)
    _touch_marker(journal, DAY, "stream.updated", 100)
    _touch_marker(journal, DAY, "daily.updated", 200)
    result = catchup_state.record_outcome(
        CMD_DAILY, DAY, "complete-new", exit_status="ok", ended_at=300
    )
    assert result.completed is True
    assert catchup_state.read_day_record(DAY, catchup_state.KIND_DAILY_CATCHUP) is None

    day2 = "20260102"
    cmd2 = ["journal", "think", "-v", "--day", day2]
    _segment(journal, day=day2).joinpath("audio.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    _touch_marker(journal, day2, "stream.updated", 100)
    catchup_state.record_attempt(cmd2, day2, "no-marker", started_at=10)
    result = catchup_state.record_outcome(
        cmd2, day2, "no-marker", exit_status="ok", ended_at=300
    )
    record = catchup_state.read_day_record(day2, catchup_state.KIND_DAILY_CATCHUP)
    assert result.completed is False
    assert record["last_outcome"] == "ran-not-completed"
    assert record["consecutive_non_completion"] == 1
    assert record["next_retry_at"] == 900

    day3 = "20260103"
    cmd3 = ["journal", "think", "-v", "--day", day3]
    _segment(journal, day=day3).joinpath("audio.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    _touch_marker(journal, day3, "stream.updated", 1000)
    _touch_marker(journal, day3, "daily.updated", 1000)
    catchup_state.record_attempt(cmd3, day3, "error-complete", started_at=10)
    _touch_marker(journal, day3, "stream.updated", 2000)
    _touch_marker(journal, day3, "daily.updated", 3000)
    result = catchup_state.record_outcome(
        cmd3, day3, "error-complete", exit_status="error", ended_at=4000
    )
    assert result.completed is True
    assert catchup_state.read_day_record(day3, catchup_state.KIND_DAILY_CATCHUP) is None


def test_daily_backoff_schedule_and_single_transition(journal):
    _segment(journal).joinpath("audio.jsonl").write_text("{}\n", encoding="utf-8")
    _touch_marker(journal, DAY, "stream.updated", 100)
    entered_at = None

    for index, expected_delta in enumerate((600, 1200, 2400), start=1):
        catchup_state.record_attempt(CMD_DAILY, DAY, f"ref-{index}", started_at=index)
        ended_at = 1000 * index
        result = catchup_state.record_outcome(
            CMD_DAILY, DAY, f"ref-{index}", exit_status="ok", ended_at=ended_at
        )
        assert result.next_retry_at - ended_at == expected_delta
        assert result.entered_backoff is (index == 3)
        if index == 3:
            entered_at = catchup_state.read_day_record(
                DAY, catchup_state.KIND_DAILY_CATCHUP
            )["entered_backoff_at"]

    catchup_state.record_attempt(CMD_DAILY, DAY, "ref-4", started_at=4)
    result = catchup_state.record_outcome(
        CMD_DAILY, DAY, "ref-4", exit_status="ok", ended_at=4000
    )
    assert result.entered_backoff is False
    assert (
        catchup_state.read_day_record(DAY, catchup_state.KIND_DAILY_CATCHUP)[
            "entered_backoff_at"
        ]
        == entered_at
    )


def test_day_eligible_to_drain(journal, monkeypatch):
    _segment(journal).joinpath("audio.jsonl").write_text("one\n", encoding="utf-8")
    fingerprint = catchup_state.read_raw_input_fingerprint(DAY)
    key = f"{DAY}:{catchup_state.KIND_DAILY_CATCHUP}"
    monkeypatch.setattr(catchup_state.time, "time", lambda: 100)

    assert catchup_state.day_eligible_to_drain(DAY, catchup_state.KIND_DAILY_CATCHUP)

    _write_state(
        journal,
        {
            key: _record(
                DAY,
                catchup_state.KIND_DAILY_CATCHUP,
                next_retry_at=1000,
                fingerprint=fingerprint,
                active={
                    "ref": "active",
                    "started_at": 1,
                    "marker_mtime_at_start": None,
                },
            )
        },
    )
    assert not catchup_state.day_eligible_to_drain(
        DAY, catchup_state.KIND_DAILY_CATCHUP
    )

    _write_state(
        journal,
        {
            key: _record(
                DAY,
                catchup_state.KIND_DAILY_CATCHUP,
                next_retry_at=1000,
                fingerprint=fingerprint,
            )
        },
    )
    assert not catchup_state.day_eligible_to_drain(
        DAY, catchup_state.KIND_DAILY_CATCHUP
    )

    _segment(journal).joinpath("audio.jsonl").write_text("two\n", encoding="utf-8")
    assert catchup_state.day_eligible_to_drain(DAY, catchup_state.KIND_DAILY_CATCHUP)

    monkeypatch.setattr(catchup_state.time, "time", lambda: 2000)
    assert catchup_state.day_eligible_to_drain(DAY, catchup_state.KIND_DAILY_CATCHUP)


def test_reconcile_interrupted_attempts(journal, monkeypatch):
    incomplete = "20260101"
    complete = "20260102"
    _segment(journal, day=incomplete).joinpath("audio.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    _touch_marker(journal, incomplete, "stream.updated", 100)
    _segment(journal, day=complete).joinpath("audio.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    _touch_marker(journal, complete, "stream.updated", 100)
    _touch_marker(journal, complete, "daily.updated", 200)
    incomplete_key = f"{incomplete}:{catchup_state.KIND_DAILY_CATCHUP}"
    complete_key = f"{complete}:{catchup_state.KIND_DAILY_CATCHUP}"
    _write_state(
        journal,
        {
            incomplete_key: _record(
                incomplete,
                catchup_state.KIND_DAILY_CATCHUP,
                attempts=3,
                consecutive=2,
                active={
                    "ref": "active-1",
                    "started_at": 1,
                    "marker_mtime_at_start": None,
                },
            ),
            complete_key: _record(
                complete,
                catchup_state.KIND_DAILY_CATCHUP,
                attempts=1,
                active={
                    "ref": "active-2",
                    "started_at": 1,
                    "marker_mtime_at_start": None,
                },
            ),
        },
    )
    monkeypatch.setattr(catchup_state.time, "time", lambda: 1000)

    transitions = catchup_state.reconcile_interrupted_attempts()

    entries = _read_entries(journal)
    assert len(transitions) == 1
    assert transitions[0].day == incomplete
    assert entries[incomplete_key]["last_outcome"] == "interrupted"
    assert entries[incomplete_key]["consecutive_non_completion"] == 3
    assert entries[incomplete_key]["active"] is None
    assert complete_key not in entries


def test_prune_removes_old_cleared_entries_but_keeps_old_stuck(journal):
    old_day = "20260101"
    recent_day = "20260214"
    newest_day = "20260215"
    for day in (old_day, recent_day, newest_day):
        _day_dir(journal, day)
    old_completed = f"{old_day}:{catchup_state.KIND_SEGMENT}"
    old_stuck = f"{old_day}:{catchup_state.KIND_DAILY_CATCHUP}"
    recent = f"{recent_day}:{catchup_state.KIND_SEGMENT}"
    _write_state(
        journal,
        {
            old_completed: _record(
                old_day, catchup_state.KIND_SEGMENT, last_outcome="completed"
            ),
            old_stuck: _record(
                old_day,
                catchup_state.KIND_DAILY_CATCHUP,
                consecutive=3,
                next_retry_at=9999,
                entered_backoff_at=1,
                last_outcome="timeout",
            ),
            recent: _record(
                recent_day, catchup_state.KIND_SEGMENT, last_outcome="completed"
            ),
        },
    )

    catchup_state.clear_day_backoff("20990101")

    entries = _read_entries(journal)
    assert old_completed not in entries
    assert old_stuck in entries
    assert recent in entries


def test_from_scratch_completion_clears_daily_catchup_record(journal):
    _segment(journal).joinpath("audio.jsonl").write_text("{}\n", encoding="utf-8")
    catchup_key = f"{DAY}:{catchup_state.KIND_DAILY_CATCHUP}"
    _write_state(
        journal,
        {
            catchup_key: _record(
                DAY,
                catchup_state.KIND_DAILY_CATCHUP,
                consecutive=3,
                next_retry_at=9999,
                entered_backoff_at=1,
                last_outcome="timeout",
            )
        },
    )

    catchup_state.record_attempt(CMD_FROM_SCRATCH, DAY, "scratch", started_at=10)
    _touch_marker(journal, DAY, "stream.updated", 100)
    _touch_marker(journal, DAY, "daily.updated", 200)
    result = catchup_state.record_outcome(
        CMD_FROM_SCRATCH, DAY, "scratch", exit_status="error", ended_at=300
    )

    assert result.completed is True
    assert catchup_state.read_day_record(DAY, catchup_state.KIND_DAILY_CATCHUP) is None
    assert (
        catchup_state.read_day_record(DAY, catchup_state.KIND_DAILY_FROM_SCRATCH)
        is None
    )

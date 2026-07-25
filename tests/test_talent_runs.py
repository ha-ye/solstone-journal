# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for talent run day-index readers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from freezegun import freeze_time

from solstone.think.talent_runs import (
    AgentFailure,
    read_unresolved_agent_failures,
)


def _write_day(journal: Path, day: str, *rows: dict | str) -> Path:
    talents = journal / "talents"
    talents.mkdir(parents=True, exist_ok=True)
    path = talents / f"{day}.jsonl"
    path.write_text(
        "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows)
        + "\n"
    )
    return path


def test_read_unresolved_agent_failures_absent_file_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is True
    assert scan.failures == []


def test_read_unresolved_agent_failures_unreadable_file_degraded(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = _write_day(tmp_path, "20260608", {"name": "flow"})
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs) -> str:
        if self == path:
            raise OSError("cannot read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is False
    assert scan.failures == []


def test_read_unresolved_agent_failures_skips_malformed_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_day(
        tmp_path,
        "20260608",
        "{not json",
        ["not", "an", "object"],
        {
            "use_id": "1",
            "name": "flow",
            "ts": 1000,
            "status": "error",
        },
    )

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is True
    assert [
        (failure.use_id, failure.name, failure.ts) for failure in scan.failures
    ] == [("1", "flow", 1000)]


def test_read_unresolved_agent_failures_self_heals_earlier_occurrence(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_day(
        tmp_path,
        "20260608",
        {"use_id": "old", "name": "flow", "ts": 1000, "status": "error"},
        {"use_id": "ok", "name": "flow", "ts": 2000, "status": "completed"},
    )

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is True
    assert scan.failures == []


def test_read_unresolved_agent_failures_counts_later_occurrences(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_day(
        tmp_path,
        "20260608",
        {"use_id": "old", "name": "flow", "ts": 1000, "status": "error"},
        {"use_id": "ok", "name": "flow", "ts": 2000, "status": "completed"},
        {"use_id": "new", "name": "flow", "ts": 3000, "status": "error"},
    )

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is True
    assert [
        (failure.use_id, failure.name, failure.ts) for failure in scan.failures
    ] == [("new", "flow", 3000)]


def test_read_unresolved_agent_failures_counts_multiple_same_agent_occurrences(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_day(
        tmp_path,
        "20260608",
        {
            "use_id": "2",
            "name": "flow",
            "ts": 2000,
            "status": "error",
            "reason_code": "provider_key_missing",
            "provider": "anthropic",
            "model": "claude-test",
        },
        {
            "use_id": "1",
            "name": "flow",
            "ts": 1000,
            "status": "error",
            "reason_code": "provider_unavailable",
            "provider": "openai",
            "model": "gpt-test",
        },
    )

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is True
    assert scan.failures == [
        AgentFailure(
            use_id="1",
            name="flow",
            ts=1000,
            reason_code="provider_unavailable",
            provider="openai",
            model="gpt-test",
        ),
        AgentFailure(
            use_id="2",
            name="flow",
            ts=2000,
            reason_code="provider_key_missing",
            provider="anthropic",
            model="claude-test",
        ),
    ]


def test_read_unresolved_agent_failures_counts_by_agent_success(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_day(
        tmp_path,
        "20260608",
        {"use_id": "flow-error", "name": "flow", "ts": 1000, "status": "error"},
        {
            "use_id": "meetings-ok",
            "name": "meetings",
            "ts": 2000,
            "status": "completed",
        },
    )

    scan = read_unresolved_agent_failures("20260608")

    assert scan.ok is True
    assert [(failure.use_id, failure.name) for failure in scan.failures] == [
        ("flow-error", "flow")
    ]


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_counts_today_execution_in_old_journal_day(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    now = datetime.now()
    _write_day(
        tmp_path,
        "20260710",
        {
            "use_id": "today-old-index",
            "name": "flow",
            "day": "20260710",
            "ts": _epoch_ms(now),
            "status": "error",
        },
    )

    scan = read_unresolved_agent_failures()

    assert scan.ok is True
    assert [(failure.use_id, failure.name, failure.ts) for failure in scan.failures] == [
        ("today-old-index", "flow", _epoch_ms(now))
    ]


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_excludes_old_execution_in_today_index(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    old_ts = _epoch_ms(datetime.now() - timedelta(days=3))
    _write_day(
        tmp_path,
        "20260725",
        {
            "use_id": "old-run",
            "name": "flow",
            "day": "20260725",
            "ts": old_ts,
            "status": "error",
        },
    )

    scan = read_unresolved_agent_failures()

    assert scan.ok is True
    assert scan.failures == []


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_resolves_across_journal_day_indexes(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    now = datetime.now()
    _write_day(
        tmp_path,
        "20260710",
        {
            "use_id": "old-error",
            "name": "flow",
            "day": "20260710",
            "ts": _epoch_ms(now),
            "status": "error",
        },
    )
    _write_day(
        tmp_path,
        "20260711",
        {
            "use_id": "later-success",
            "name": "flow",
            "day": "20260711",
            "ts": _epoch_ms(now + timedelta(minutes=1)),
            "status": "completed",
        },
    )

    scan = read_unresolved_agent_failures()

    assert scan.ok is True
    assert scan.failures == []


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_keeps_later_failure_across_journal_day_indexes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    now = datetime.now()
    _write_day(
        tmp_path,
        "20260710",
        {
            "use_id": "success",
            "name": "flow",
            "day": "20260710",
            "ts": _epoch_ms(now),
            "status": "completed",
        },
    )
    _write_day(
        tmp_path,
        "20260711",
        {
            "use_id": "later-error",
            "name": "flow",
            "day": "20260711",
            "ts": _epoch_ms(now + timedelta(minutes=1)),
            "status": "error",
        },
    )

    scan = read_unresolved_agent_failures()

    assert scan.ok is True
    assert [(failure.use_id, failure.name) for failure in scan.failures] == [
        ("later-error", "flow")
    ]


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_unreadable_index_returns_partial_degraded(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    now = datetime.now()
    readable = _write_day(
        tmp_path,
        "20260710",
        {
            "use_id": "known-error",
            "name": "flow",
            "day": "20260710",
            "ts": _epoch_ms(now),
            "status": "error",
        },
    )
    unreadable = _write_day(
        tmp_path,
        "20260711",
        {
            "use_id": "unreadable-error",
            "name": "meetings",
            "day": "20260711",
            "ts": _epoch_ms(now),
            "status": "error",
        },
    )
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs) -> str:
        if self == unreadable:
            raise OSError("cannot read")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    scan = read_unresolved_agent_failures()

    assert readable.exists()
    assert scan.ok is False
    assert [(failure.use_id, failure.name) for failure in scan.failures] == [
        ("known-error", "flow")
    ]


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_uses_ts_not_mtime_for_execution_day(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    now = datetime.now()
    old_mtime_today_run = _write_day(
        tmp_path,
        "20260710",
        {
            "use_id": "old-mtime-today-run",
            "name": "flow",
            "day": "20260710",
            "ts": _epoch_ms(now),
            "status": "error",
        },
    )
    fresh_mtime_old_run = _write_day(
        tmp_path,
        "20260725",
        {
            "use_id": "fresh-mtime-old-run",
            "name": "meetings",
            "day": "20260725",
            "ts": _epoch_ms(now - timedelta(days=2)),
            "status": "error",
        },
    )
    old_mtime = (now - timedelta(days=90)).timestamp()
    os.utime(old_mtime_today_run, (old_mtime, old_mtime))
    fresh_mtime_old_run.touch()

    scan = read_unresolved_agent_failures()

    assert scan.ok is True
    assert [(failure.use_id, failure.name) for failure in scan.failures] == [
        ("old-mtime-today-run", "flow")
    ]


@freeze_time("2026-07-25 12:00:00")
def test_default_scan_excludes_entries_without_today_attributable_ts(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    now = datetime.now()
    _write_day(
        tmp_path,
        "20260710",
        {"use_id": "missing", "name": "flow", "status": "error"},
        {"use_id": "zero", "name": "flow", "ts": 0, "status": "error"},
        {"use_id": "bool", "name": "flow", "ts": True, "status": "error"},
        {
            "use_id": "valid",
            "name": "flow",
            "ts": _epoch_ms(now),
            "status": "error",
        },
    )

    scan = read_unresolved_agent_failures()

    assert scan.ok is True
    assert [failure.use_id for failure in scan.failures] == ["valid"]


@freeze_time("2026-07-25 12:00:00")
def test_explicit_day_scan_keeps_journal_day_timestamp_semantics(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    old_ts = _epoch_ms(datetime.now() - timedelta(days=4))
    _write_day(
        tmp_path,
        "20260725",
        {
            "use_id": "explicit-old-ts",
            "name": "flow",
            "day": "20260725",
            "ts": old_ts,
            "status": "error",
        },
    )

    scan = read_unresolved_agent_failures("20260725")

    assert scan.ok is True
    assert [(failure.use_id, failure.ts) for failure in scan.failures] == [
        ("explicit-old-ts", old_ts)
    ]

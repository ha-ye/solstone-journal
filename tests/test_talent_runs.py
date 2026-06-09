# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for talent run day-index readers."""

from __future__ import annotations

import json
from pathlib import Path

from solstone.think.talent_runs import (
    AgentFailure,
    read_unresolved_agent_failures,
)


def _write_day(journal: Path, day: str, *rows: dict | str) -> Path:
    talents = journal / "talents"
    talents.mkdir(parents=True)
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

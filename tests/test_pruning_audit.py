# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the shared pruning audit writer."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import solstone.think.pruning_audit as pruning_audit
import solstone.think.utils as think_utils
from solstone.think.pruning_audit import write_prune_audit

FIXED_NOW = datetime(2026, 4, 15, 10, 0, 0)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return FIXED_NOW.replace(tzinfo=tz)
        return FIXED_NOW


@pytest.fixture
def journal(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    root.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(root))
    think_utils._journal_path_cache = None
    monkeypatch.setattr(pruning_audit, "datetime", FixedDateTime)
    return root


def test_write_prune_audit_appends_day_logs_and_global_record(journal):
    record = {"kind": "journal_logs", "files_deleted": 1}

    outcome = write_prune_audit(
        journal,
        kind="journal_logs",
        run_record=record,
        per_day_messages={"20260315": "log-retention: pruned 1 file"},
    )

    assert outcome.global_record_written is True
    assert outcome.global_record_error is None
    assert outcome.per_day_failures == {}
    assert outcome.partial_error is False
    task_log = journal / "chronicle" / "20260315" / "task_log.txt"
    assert "log-retention: pruned 1 file" in task_log.read_text(encoding="utf-8")
    run_log = journal / "health" / "pruning-runs" / "20260415.jsonl"
    assert json.loads(run_log.read_text(encoding="utf-8")) == record


def test_write_prune_audit_collects_day_failures_and_continues(journal, monkeypatch):
    def fail_day_log(day, message):
        if day == "20260315":
            raise OSError("blocked")

    monkeypatch.setattr(pruning_audit, "day_log_checked", fail_day_log)
    record = {"kind": "journal_logs", "files_deleted": 1}

    outcome = write_prune_audit(
        journal,
        kind="journal_logs",
        run_record=record,
        per_day_messages={"20260315": "blocked"},
    )

    assert outcome.global_record_written is True
    assert outcome.per_day_failures == {"20260315": "blocked"}
    assert outcome.partial_error is True


def test_write_prune_audit_reports_global_record_failure(journal, monkeypatch):
    def fail_open(*args, **kwargs):
        raise OSError("global blocked")

    monkeypatch.setattr(pruning_audit, "open", fail_open, raising=False)

    outcome = write_prune_audit(
        journal,
        kind="journal_logs",
        run_record={"kind": "journal_logs"},
        per_day_messages={},
    )

    assert outcome.global_record_written is False
    assert outcome.global_record_error == "global blocked"
    assert outcome.partial_error is True


def test_write_prune_audit_rejects_unknown_kind(journal):
    with pytest.raises(ValueError, match="kind must be"):
        write_prune_audit(
            journal,
            kind="other",
            run_record={},
            per_day_messages={},
        )

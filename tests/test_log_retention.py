# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for think.log_retention."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import solstone.think.log_retention as log_retention
import solstone.think.pruning_audit as pruning_audit
import solstone.think.utils as think_utils
from solstone.think.log_retention import (
    LogRetentionConfig,
    load_log_retention_config,
    prune,
)

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
    monkeypatch.setattr(log_retention, "datetime", FixedDateTime)
    monkeypatch.setattr(pruning_audit, "datetime", FixedDateTime)
    return root


def _day(days_before_now: int) -> str:
    return (FIXED_NOW.date() - timedelta(days=days_before_now)).strftime("%Y%m%d")


def _epoch_ms(day: str) -> str:
    dt = datetime.strptime(day, "%Y%m%d").replace(hour=12)
    return str(int(dt.timestamp() * 1000))


def _epoch_seconds(day: str) -> str:
    dt = datetime.strptime(day, "%Y%m%d").replace(hour=12)
    return str(int(dt.timestamp()))


def _root_task_log_line(day: str, message: str) -> bytes:
    return f"{_epoch_seconds(day)}\t{message}\n".encode("utf-8")


def _write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _set_mtime(path: Path, day: str) -> None:
    dt = datetime.strptime(day, "%Y%m%d").replace(hour=12)
    ts = dt.timestamp()
    os.utime(path, (ts, ts))


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_ac1_load_log_retention_config_defaults_per_field(journal):
    assert load_log_retention_config() == LogRetentionConfig(enabled=True, days=30)

    config_path = journal / "config" / "journal.json"
    _write(config_path, json.dumps({"retention": {}}))
    assert load_log_retention_config() == LogRetentionConfig(enabled=True, days=30)

    _write(config_path, json.dumps({"retention": {"journal_logs": {"days": 12}}}))
    assert load_log_retention_config() == LogRetentionConfig(enabled=True, days=12)

    _write(
        config_path,
        json.dumps({"retention": {"journal_logs": {"enabled": False}}}),
    )
    assert load_log_retention_config() == LogRetentionConfig(enabled=False, days=30)

    _write(config_path, json.dumps({"retention": {"journal_logs": {"days": True}}}))
    with pytest.raises(ValueError, match="days must be a positive integer"):
        load_log_retention_config()


def test_ac3_disabled_config_deletes_nothing_and_writes_no_audit(journal):
    old_day = _day(31)
    old_file = _write(journal / "tokens" / f"{old_day}.jsonl")
    root_log = journal / "task_log.txt"
    root_log.write_bytes(_root_task_log_line(old_day, "old root line"))
    root_before = root_log.read_bytes()

    result = prune(config=LogRetentionConfig(enabled=False, days=30))

    assert old_file.exists()
    assert root_log.read_bytes() == root_before
    assert result.enabled is False
    assert result.files_deleted == 0
    assert result.root_task_log["lines_removed"] == 0
    assert result.audit_written is False
    assert not (journal / "health" / "pruning-runs").exists()
    assert not (journal / "chronicle" / old_day / "task_log.txt").exists()


def test_local_inference_telemetry_follows_journal_log_retention(journal):
    old_day = _day(31)
    recent_day = _day(1)
    old = _write(journal / "health" / "local-inference" / f"{old_day}.jsonl")
    recent = _write(journal / "health" / "local-inference" / f"{recent_day}.jsonl")

    result = prune(config=LogRetentionConfig(days=30))

    assert not old.exists()
    assert recent.exists()
    assert result.by_class["local_inference"]["files_deleted"] == 1


def test_ac4_dry_run_reports_candidates_without_deleting_or_audit(journal):
    old_day = _day(31)
    old_token = _write(journal / "tokens" / f"{old_day}.jsonl", "old")

    result = prune(dry_run=True, config=LogRetentionConfig(days=30))

    assert old_token.exists()
    assert result.dry_run is True
    assert result.files_deleted == 1
    assert result.by_class["tokens"]["files_deleted"] == 1
    assert result.by_day[old_day]["files_deleted"] == 1
    assert result.audit_written is False
    assert not (journal / "health" / "pruning-runs").exists()


def test_root_task_log_dry_run_reports_without_rewrite_or_audit(journal):
    old_line = _root_task_log_line(_day(31), "old root line")
    cutoff_line = _root_task_log_line(_day(30), "cutoff root line")
    recent_line = _root_task_log_line(_day(1), "recent root line")
    malformed_line = b"not-an-epoch\tkeep this line\n"
    no_tab_line = b"missing separator\n"
    root_log = journal / "task_log.txt"
    original = old_line + cutoff_line + recent_line + malformed_line + no_tab_line
    root_log.write_bytes(original)

    result = prune(dry_run=True, config=LogRetentionConfig(days=30))

    stats = result.root_task_log
    assert root_log.read_bytes() == original
    assert stats["exists"] is True
    assert stats["lines_total"] == 5
    assert stats["lines_removed"] == 1
    assert stats["lines_kept"] == 4
    assert stats["unparseable_lines_kept"] == 2
    assert stats["bytes_freed"] == len(old_line)
    assert stats["rewritten"] is False
    assert result.bytes_freed == len(old_line)
    assert result.audit_written is False
    assert not (journal / "health" / "pruning-runs").exists()


def test_root_task_log_compacts_old_epoch_lines_and_writes_audit(journal):
    old_line = _root_task_log_line(_day(31), "old root line")
    cutoff_line = _root_task_log_line(_day(30), "cutoff root line")
    recent_line = _root_task_log_line(_day(1), "recent root line")
    impossible_epoch_line = b"999999999999999999999999\tkeep malformed\n"
    root_log = journal / "task_log.txt"
    root_log.write_bytes(old_line + cutoff_line + recent_line + impossible_epoch_line)

    result = prune(config=LogRetentionConfig(days=30))

    expected = cutoff_line + recent_line + impossible_epoch_line
    stats = result.root_task_log
    assert root_log.exists()
    assert root_log.read_bytes() == expected
    assert result.files_deleted == 0
    assert result.dirs_deleted == 0
    assert result.bytes_freed == len(old_line)
    assert result.audit_written is True
    assert stats["exists"] is True
    assert stats["lines_total"] == 4
    assert stats["lines_removed"] == 1
    assert stats["lines_kept"] == 3
    assert stats["unparseable_lines_kept"] == 1
    assert stats["bytes_freed"] == len(old_line)
    assert stats["rewritten"] is True
    assert stats["errors"] == []

    run_log = journal / "health" / "pruning-runs" / f"{FIXED_NOW:%Y%m%d}.jsonl"
    records = _read_jsonl(run_log)
    assert len(records) == 1
    record = records[0]
    assert record["totals"]["files_deleted"] == 0
    assert record["totals"]["dirs_deleted"] == 0
    assert record["totals"]["bytes_freed"] == len(old_line)
    assert record["root_task_log"]["lines_removed"] == 1
    assert not (journal / "chronicle" / _day(31) / "task_log.txt").exists()


def test_root_task_log_all_old_lines_leaves_file_in_place(journal):
    old_line = _root_task_log_line(_day(31), "old root line")
    root_log = journal / "task_log.txt"
    root_log.write_bytes(old_line)

    result = prune(config=LogRetentionConfig(days=30))

    assert root_log.exists()
    assert root_log.read_bytes() == b""
    assert result.root_task_log["lines_removed"] == 1
    assert result.root_task_log["rewritten"] is True


def test_root_task_log_missing_or_no_old_lines_writes_no_audit(journal):
    missing = prune(config=LogRetentionConfig(days=30))

    assert missing.root_task_log["exists"] is False
    assert missing.audit_written is False
    assert not (journal / "health" / "pruning-runs").exists()

    root_log = journal / "task_log.txt"
    recent_line = _root_task_log_line(_day(1), "recent root line")
    root_log.write_bytes(recent_line)

    recent_only = prune(config=LogRetentionConfig(days=30))

    assert root_log.read_bytes() == recent_line
    assert recent_only.root_task_log["exists"] is True
    assert recent_only.root_task_log["lines_removed"] == 0
    assert recent_only.root_task_log["rewritten"] is False
    assert recent_only.audit_written is False
    assert not (journal / "health" / "pruning-runs").exists()


def test_root_task_log_rewrite_failure_preserves_original_and_reports_partial(
    journal,
    monkeypatch,
):
    old_line = _root_task_log_line(_day(31), "old root line")
    recent_line = _root_task_log_line(_day(1), "recent root line")
    root_log = journal / "task_log.txt"
    root_log.write_bytes(old_line + recent_line)
    original = root_log.read_bytes()

    def fail_replace(_path, _data):
        raise OSError("blocked")

    monkeypatch.setattr(log_retention, "_atomic_replace_file", fail_replace)

    result = prune(config=LogRetentionConfig(days=30))

    assert root_log.read_bytes() == original
    assert result.audit_written is False
    assert result.partial_error is True
    assert result.bytes_freed == 0
    assert result.root_task_log["lines_removed"] == 1
    assert result.root_task_log["bytes_freed"] == 0
    assert result.root_task_log["rewritten"] is False
    assert any(
        error["reason"] == "root_task_log_rewrite_failed" for error in result.errors
    )
    assert not (journal / "health" / "pruning-runs").exists()


def test_ac5_boundary_keeps_cutoff_day_and_deletes_older(journal):
    old_day = _day(31)
    cutoff_day = _day(30)
    old_file = _write(journal / "tokens" / f"{old_day}.jsonl")
    cutoff_file = _write(journal / "tokens" / f"{cutoff_day}.jsonl")

    result = prune(config=LogRetentionConfig(days=30))

    assert not old_file.exists()
    assert cutoff_file.exists()
    assert result.cutoff_day == cutoff_day
    assert result.files_deleted == 1
    assert old_day in result.by_day
    assert cutoff_day not in result.by_day


def test_ac6_days_override_is_one_run_only_and_config_file_is_unchanged(journal):
    config_path = journal / "config" / "journal.json"
    _write(
        config_path,
        json.dumps({"retention": {"journal_logs": {"enabled": True, "days": 90}}}),
    )
    eleven_days_old = _day(11)
    candidate = _write(journal / "tokens" / f"{eleven_days_old}.jsonl")

    result = prune(days=10)

    assert result.days == 10
    assert not candidate.exists()
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["retention"]["journal_logs"]["days"] == 90


def test_ac7_chronicle_allowlist_and_task_log_day_routing(journal):
    old_day = _day(31)
    health = journal / "chronicle" / old_day / "health"
    old_log = _write(health / "service.log", "log")
    old_jsonl = _write(health / "service.jsonl", "{}\n")
    task_log = _write(journal / "chronicle" / old_day / "task_log.txt", "keep\n")
    marker = _write(health / "stream.updated", "keep")
    json_marker = _write(health / "state.json", "{}")
    talent_file = _write(
        journal / "chronicle" / old_day / "talents" / "flow.jsonl",
        "{}\n",
    )
    segment_jsonl = _write(
        journal / "chronicle" / old_day / "default" / "120000_300" / "audio.jsonl",
        "{}\n",
    )

    result = prune(config=LogRetentionConfig(days=30))

    assert not old_log.exists()
    assert not old_jsonl.exists()
    for protected in (task_log, marker, json_marker, talent_file, segment_jsonl):
        assert protected.exists()
    assert result.by_class["chronicle_health_logs"]["files_deleted"] == 2
    task_text = task_log.read_text(encoding="utf-8")
    assert "log-retention: pruned 2 operational-log file(s)" in task_text
    assert task_text.count("log-retention:") == 1


def test_ac8_ac9_talent_logs_indexes_and_malformed_names(journal):
    old_day = _day(31)
    older_day = _day(32)
    malformed_day = _day(33)
    empty_day = _day(34)
    invalid_utf8_day = _day(35)
    recent_day = _day(1)
    talent_dir = journal / "talents" / "default"
    old_run = _write(talent_dir / f"{_epoch_ms(old_day)}.jsonl")
    recent_run = _write(talent_dir / f"{_epoch_ms(recent_day)}.jsonl")
    active = _write(talent_dir / f"{_epoch_ms(old_day)}_active.jsonl")
    nonnumeric = _write(talent_dir / "not-a-time.jsonl")
    regular_log = _write(journal / "talents" / "default.log")
    log_target = _write(journal / "outside-default.log")
    log_link = journal / "talents" / "linked.log"
    log_link.parent.mkdir(parents=True, exist_ok=True)
    log_link.symlink_to(log_target)
    old_all_valid_old_index = _write(
        journal / "talents" / f"{old_day}.jsonl",
        json.dumps({"ts": int(_epoch_ms(old_day)), "status": "completed"}) + "\n",
    )
    old_recent_ts_index = _write(
        journal / "talents" / f"{older_day}.jsonl",
        json.dumps({"ts": int(_epoch_ms(recent_day)), "status": "completed"}) + "\n",
    )
    old_malformed_index = _write(journal / "talents" / f"{malformed_day}.jsonl")
    old_empty_index = _write(journal / "talents" / f"{empty_day}.jsonl", "\n\n")
    old_invalid_utf8_index = journal / "talents" / f"{invalid_utf8_day}.jsonl"
    old_invalid_utf8_index.parent.mkdir(parents=True, exist_ok=True)
    old_invalid_utf8_index.write_bytes(b"\xff")
    recent_index = _write(journal / "talents" / f"{recent_day}.jsonl", "x")
    bad_index = _write(journal / "talents" / "20260231.jsonl")

    result = prune(config=LogRetentionConfig(days=30))

    assert not old_run.exists()
    assert not old_all_valid_old_index.exists()
    for protected in (
        recent_run,
        active,
        nonnumeric,
        regular_log,
        log_link,
        log_target,
        old_recent_ts_index,
        old_malformed_index,
        old_empty_index,
        old_invalid_utf8_index,
        recent_index,
        bad_index,
    ):
        assert protected.exists()
    reasons = {error["reason"] for error in result.errors}
    assert "malformed_talent_timestamp" in reasons
    assert "malformed_date" in reasons
    assert "malformed_talent_index_row" in reasons
    assert "unreadable_talent_index" in reasons


def test_ac10_ac11_symlink_unlinked_only_and_cache_mtime_day_logged(journal):
    old_day = _day(31)
    cutoff_day = _day(30)
    recent_day = _day(1)
    target = _write(journal / "kept-target.txt", "target")
    token_link = journal / "tokens" / f"{old_day}.jsonl"
    token_link.parent.mkdir(parents=True, exist_ok=True)
    token_link.symlink_to(target)
    cache_root = journal / ".cache" / "cogitate-history"
    old_cache = cache_root / "old-session"
    old_cache.mkdir(parents=True)
    _write(old_cache / "events" / "event-00000-abc.json", "{}")
    cutoff_cache = cache_root / "cutoff-session"
    cutoff_cache.mkdir()
    recent_cache = cache_root / "recent-session"
    recent_cache.mkdir()
    _set_mtime(old_cache, old_day)
    _set_mtime(cutoff_cache, cutoff_day)
    _set_mtime(recent_cache, recent_day)

    result = prune(config=LogRetentionConfig(days=30))

    assert not token_link.exists()
    assert target.exists()
    assert not old_cache.exists()
    assert cutoff_cache.exists()
    assert recent_cache.exists()
    assert result.by_class["tokens"]["files_deleted"] == 1
    assert result.by_class["cogitate_history_cache"]["dirs_deleted"] == 1
    task_log = journal / "chronicle" / old_day / "task_log.txt"
    assert "cache dir(s)" in task_log.read_text(encoding="utf-8")


def test_ac12_ac14_dated_classes_delete_and_root_health_is_unreachable(journal):
    old_day = _day(31)
    recent_day = _day(1)
    paths = [
        _write(journal / "tokens" / f"{old_day}.jsonl"),
        _write(journal / "awareness" / f"{old_day}.jsonl"),
        _write(journal / "config" / "actions" / f"{old_day}.jsonl"),
        _write(journal / "facets" / "work" / "logs" / f"{old_day}.jsonl"),
        _write(
            journal
            / "apps"
            / "observer"
            / "observers"
            / "local"
            / "hist"
            / f"{old_day}.jsonl"
        ),
    ]
    protected = [
        _write(journal / "awareness" / "current.json", "{}"),
        _write(journal / "tokens" / f"{recent_day}.jsonl"),
        _write(journal / "health" / "system.log", "root"),
        _write(journal / "health" / "pruning-runs" / f"{old_day}.jsonl", "{}\n"),
    ]

    result = prune(config=LogRetentionConfig(days=30))

    assert all(not path.exists() for path in paths)
    assert all(path.exists() for path in protected)
    assert result.by_class["tokens"]["files_deleted"] == 1
    assert result.by_class["awareness_logs"]["files_deleted"] == 1
    assert result.by_class["config_actions"]["files_deleted"] == 1
    assert result.by_class["facet_logs"]["files_deleted"] == 1
    assert result.by_class["observer_history"]["files_deleted"] == 1


def test_ac15_ac17_ac22_global_record_historical_day_and_idempotency(journal):
    old_day = _day(31)
    old_file = _write(journal / "tokens" / f"{old_day}.jsonl")
    assert not (journal / "chronicle" / old_day).exists()

    first = prune(config=LogRetentionConfig(days=30))

    assert not old_file.exists()
    assert first.audit_written is True
    run_log = journal / "health" / "pruning-runs" / f"{FIXED_NOW:%Y%m%d}.jsonl"
    records = _read_jsonl(run_log)
    assert len(records) == 1
    record = records[0]
    assert record["timestamp"] == FIXED_NOW.isoformat()
    assert record["kind"] == "journal_logs"
    assert record["dry_run"] is False
    assert record["days"] == 30
    assert record["cutoff_day"] == _day(30)
    assert record["by_day"][old_day]["files_deleted"] == 1
    assert record["by_class"]["tokens"]["files_deleted"] == 1
    assert record["totals"]["files_deleted"] == 1
    assert "errors" in record
    assert "audit" not in record
    assert (journal / "chronicle" / old_day / "task_log.txt").exists()

    second = prune(config=LogRetentionConfig(days=30))

    assert second.files_deleted == 0
    assert second.audit_written is False
    assert len(_read_jsonl(run_log)) == 1


def test_ac16_delete_failed_and_global_record_failed_surface(journal, monkeypatch):
    old_day = _day(31)
    blocked = _write(journal / "tokens" / f"{old_day}.jsonl")
    original_unlink = Path.unlink

    def fail_one_unlink(self, *args, **kwargs):
        if self == blocked:
            raise OSError("blocked")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one_unlink)

    delete_failed = prune(config=LogRetentionConfig(days=30))

    assert blocked.exists()
    assert delete_failed.partial_error is True
    delete_errors = [
        error for error in delete_failed.errors if error["reason"] == "delete_failed"
    ]
    assert delete_errors
    assert delete_errors[0]["hint"]

    monkeypatch.setattr(Path, "unlink", original_unlink)
    blocked.unlink()
    other = _write(journal / "tokens" / f"{old_day}.jsonl")

    def fail_global_open(*args, **kwargs):
        raise OSError("audit blocked")

    monkeypatch.setattr(pruning_audit, "open", fail_global_open, raising=False)

    global_failed = prune(config=LogRetentionConfig(days=30))

    assert not other.exists()
    assert global_failed.audit_written is False
    assert global_failed.partial_error is True
    assert any(
        error["reason"] == "global_record_failed" for error in global_failed.errors
    )


def test_ac16_task_log_append_failure_surfaces_as_partial_error(journal, monkeypatch):
    old_day = _day(31)
    old_file = _write(journal / "tokens" / f"{old_day}.jsonl")

    def fail_day_log(day, message):
        raise OSError(f"blocked {day}")

    monkeypatch.setattr(pruning_audit, "day_log_checked", fail_day_log)

    result = prune(config=LogRetentionConfig(days=30))

    assert not old_file.exists()
    assert result.audit_written is True
    assert result.partial_error is True
    assert any(
        error["reason"] == "task_log_append_failed" and error["day"] == old_day
        for error in result.errors
    )
    assert (journal / "health" / "pruning-runs" / f"{FIXED_NOW:%Y%m%d}.jsonl").exists()

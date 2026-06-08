# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from solstone.think import schedule_config
from solstone.think.journal_io.errors import MalformedDataError


def _use_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return tmp_path / "config" / "schedules.json"


def _write_schedules(path: Path, data: Any) -> bytes:
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path.read_bytes()


def _read_schedules(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_set_schedule_metadata_writes_daily_time_and_preserves_entries(
    tmp_path, monkeypatch
):
    path = _use_journal(tmp_path, monkeypatch)
    _write_schedules(
        path,
        {"sync:plaud": {"cmd": ["sol", "import"], "every": "hourly"}},
    )

    schedule_config.set_schedule_metadata({"daily_time": "03:00"})

    raw = _read_schedules(path)
    assert raw["daily_time"] == "03:00"
    assert raw["sync:plaud"] == {"cmd": ["sol", "import"], "every": "hourly"}


def test_set_schedule_entries_preserves_metadata_and_unrelated_entries(
    tmp_path, monkeypatch
):
    path = _use_journal(tmp_path, monkeypatch)
    _write_schedules(
        path,
        {
            "daily_time": "03:00",
            "custom": {"cmd": ["journal", "heartbeat"], "every": "daily"},
            "sync:plaud": {"cmd": ["old"], "every": "hourly"},
        },
    )

    schedule_config.set_schedule_entries(
        {"sync:plaud": {"cmd": ["sol", "import"], "every": "hourly"}}
    )

    raw = _read_schedules(path)
    assert raw["daily_time"] == "03:00"
    assert raw["custom"] == {"cmd": ["journal", "heartbeat"], "every": "daily"}
    assert raw["sync:plaud"] == {"cmd": ["sol", "import"], "every": "hourly"}


def test_set_schedule_entries_applies_multiple_entries_in_one_call(
    tmp_path, monkeypatch
):
    path = _use_journal(tmp_path, monkeypatch)

    schedule_config.set_schedule_entries(
        {
            "one": {"cmd": ["journal", "heartbeat"], "every": "daily"},
            "two": {"cmd": ["journal", "providers", "check"], "every": "weekly"},
        }
    )

    assert _read_schedules(path) == {
        "one": {"cmd": ["journal", "heartbeat"], "every": "daily"},
        "two": {"cmd": ["journal", "providers", "check"], "every": "weekly"},
    }


def test_mutation_preserves_schema_invalid_unrelated_entry_and_load_config_forgives(
    tmp_path, monkeypatch
):
    path = _use_journal(tmp_path, monkeypatch)
    _write_schedules(
        path,
        {
            "daily_time": "03:00",
            "good": {"cmd": ["journal", "heartbeat"], "every": "daily"},
            "badentry": {"cmd": "notalist", "every": "nope"},
        },
    )

    schedule_config.set_schedule_metadata({"weekly_day": "sunday"})

    raw = _read_schedules(path)
    assert raw == {
        "daily_time": "03:00",
        "good": {"cmd": ["journal", "heartbeat"], "every": "daily"},
        "badentry": {"cmd": "notalist", "every": "nope"},
        "weekly_day": "sunday",
    }

    import solstone.think.scheduler as scheduler

    entries = scheduler.load_config()
    assert entries == {"good": {"cmd": ["journal", "heartbeat"], "every": "daily"}}
    assert scheduler._daily_time == "03:00"
    assert scheduler._weekly_day == "sunday"


def test_write_failure_leaves_bytes_intact_and_no_temp_file(tmp_path, monkeypatch):
    path = _use_journal(tmp_path, monkeypatch)
    before = _write_schedules(
        path,
        {"existing": {"cmd": ["journal", "heartbeat"], "every": "daily"}},
    )

    def _boom(_path, _data):
        raise OSError("boom")

    monkeypatch.setattr(schedule_config, "atomic_replace", _boom)

    with pytest.raises(OSError):
        schedule_config.set_schedule_entries(
            {"new": {"cmd": ["journal", "providers", "check"], "every": "daily"}}
        )

    assert path.read_bytes() == before
    assert list(path.parent.glob(".tmp_*")) == []


@pytest.mark.parametrize("payload", ["{ not json", "[]"])
def test_invalid_file_fail_visible_and_preserves_bytes(tmp_path, monkeypatch, payload):
    path = _use_journal(tmp_path, monkeypatch)
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(MalformedDataError):
        schedule_config.set_schedule_entries(
            {"new": {"cmd": ["journal", "heartbeat"], "every": "daily"}}
        )

    assert path.read_bytes() == before


def test_concurrent_read_modify_write_preserves_both_updates(tmp_path, monkeypatch):
    path = _use_journal(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _set_metadata():
        try:
            barrier.wait()
            schedule_config.set_schedule_metadata({"daily_time": "03:00"})
        except BaseException as exc:
            errors.append(exc)

    def _set_entry():
        try:
            barrier.wait()
            schedule_config.set_schedule_entries(
                {"sync:plaud": {"cmd": ["sol", "import"], "every": "hourly"}}
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_set_metadata),
        threading.Thread(target=_set_entry),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    raw = _read_schedules(path)
    assert raw["daily_time"] == "03:00"
    assert raw["sync:plaud"] == {"cmd": ["sol", "import"], "every": "hourly"}


def test_reserved_key_collision_guards(tmp_path, monkeypatch):
    _use_journal(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        schedule_config.set_schedule_entries(
            {"daily_time": {"cmd": ["journal", "heartbeat"], "every": "daily"}}
        )

    with pytest.raises(ValueError):
        schedule_config.remove_schedule_entry("weekly_day")


def test_remove_schedule_entry_deletes_present_and_absent_is_noop(
    tmp_path, monkeypatch
):
    path = _use_journal(tmp_path, monkeypatch)
    _write_schedules(
        path,
        {
            "daily_time": "03:00",
            "remove-me": {"cmd": ["journal", "heartbeat"], "every": "daily"},
            "keep-me": {"cmd": ["journal", "providers", "check"], "every": "daily"},
        },
    )

    schedule_config.remove_schedule_entry("remove-me")

    raw = _read_schedules(path)
    assert "remove-me" not in raw
    assert raw["daily_time"] == "03:00"
    assert raw["keep-me"] == {
        "cmd": ["journal", "providers", "check"],
        "every": "daily",
    }
    before = path.read_bytes()

    schedule_config.remove_schedule_entry("absent")

    assert path.read_bytes() == before


def test_remove_absent_entry_does_not_create_file(tmp_path, monkeypatch):
    path = _use_journal(tmp_path, monkeypatch)

    schedule_config.remove_schedule_entry("absent")

    assert not path.exists()

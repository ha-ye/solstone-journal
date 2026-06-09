# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for legacy timeline rollup schedule migration."""

from __future__ import annotations

import importlib
import json

from solstone.apps.timeline.tests.conftest import write_json

mod = importlib.import_module(
    "solstone.apps.timeline.maint.002_migrate_rollup_schedules"
)


def _schedules_path(journal):
    return journal / "config" / "schedules.json"


def _read_schedules(journal):
    return json.loads(_schedules_path(journal).read_text(encoding="utf-8"))


def test_removes_exact_legacy_entries(timeline_journal):
    write_json(_schedules_path(timeline_journal), dict(mod.LEGACY_ENTRIES))

    summary = mod.run_migration()

    data = _read_schedules(timeline_journal)
    assert summary.removed == 2
    assert summary.absent == 0
    assert "timeline-rollup-day" not in data
    assert "timeline-rollup-master" not in data


def test_preserves_divergent_disabled_entry(timeline_journal):
    divergent = {**mod.LEGACY_ENTRIES["timeline-rollup-day"], "enabled": False}
    write_json(
        _schedules_path(timeline_journal),
        {
            "timeline-rollup-day": divergent,
            "timeline-rollup-master": mod.LEGACY_ENTRIES["timeline-rollup-master"],
        },
    )

    summary = mod.run_migration()

    data = _read_schedules(timeline_journal)
    assert summary.preserved == 1
    assert summary.removed == 1
    assert data["timeline-rollup-day"] == divergent
    assert "timeline-rollup-master" not in data
    assert summary.preserved_names == ["timeline-rollup-day"]


def test_preserves_changed_cmd_entry(timeline_journal):
    divergent = {
        "cmd": ["custom"],
        "every": "daily",
        "max_runtime": "30m",
    }
    write_json(
        _schedules_path(timeline_journal),
        {"timeline-rollup-day": divergent},
    )

    summary = mod.run_migration()

    data = _read_schedules(timeline_journal)
    assert summary.preserved == 1
    assert summary.removed == 0
    assert summary.absent == 1
    assert data["timeline-rollup-day"] == divergent
    assert summary.preserved_names == ["timeline-rollup-day"]


def test_absent_entries_noop_without_write(timeline_journal):
    schedules_path = _schedules_path(timeline_journal)
    write_json(schedules_path, {})
    before = schedules_path.read_bytes()

    summary = mod.run_migration()

    assert summary.absent == 2
    assert summary.removed == 0
    assert schedules_path.read_bytes() == before


def test_idempotent_second_run_reports_absent(timeline_journal):
    write_json(_schedules_path(timeline_journal), dict(mod.LEGACY_ENTRIES))

    first = mod.run_migration()
    second = mod.run_migration()

    assert first.removed == 2
    assert second.removed == 0
    assert second.absent == 2


def test_dry_run_reports_removal_without_writing(timeline_journal):
    schedules_path = _schedules_path(timeline_journal)
    write_json(schedules_path, dict(mod.LEGACY_ENTRIES))
    before = schedules_path.read_bytes()

    summary = mod.run_migration(dry_run=True)

    assert summary.removed == 2
    assert schedules_path.read_bytes() == before
    assert set(_read_schedules(timeline_journal)) == set(mod.LEGACY_ENTRIES)

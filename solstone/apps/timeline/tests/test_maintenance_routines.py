# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for timeline app-owned maintenance routine descriptors."""

from __future__ import annotations

from solstone.think.maintenance import (
    discover_routines,
    expected_schedule_entry,
    maintenance_schedule_name,
)


def test_timeline_rollup_routines_are_discovered():
    routines = discover_routines()

    assert "timeline:rollup-day" in routines
    assert "timeline:rollup-master" in routines
    assert routines["timeline:rollup-day"].every == "daily"
    assert routines["timeline:rollup-day"].max_runtime == "30m"
    assert routines["timeline:rollup-master"].every == "daily"
    assert routines["timeline:rollup-master"].max_runtime == "30m"
    assert expected_schedule_entry(
        "timeline:rollup-day", routines["timeline:rollup-day"]
    ) == {
        "cmd": ["journal", "maintenance", "run", "timeline:rollup-day"],
        "every": "daily",
        "enabled": True,
        "max_runtime": "30m",
    }
    assert expected_schedule_entry(
        "timeline:rollup-master", routines["timeline:rollup-master"]
    ) == {
        "cmd": ["journal", "maintenance", "run", "timeline:rollup-master"],
        "every": "daily",
        "enabled": True,
        "max_runtime": "30m",
    }
    assert maintenance_schedule_name("timeline:rollup-day") == (
        "maintenance:timeline:rollup-day"
    )

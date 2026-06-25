# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import pytest

import solstone.think.utils as think_utils
from solstone.think.utils import day_log, day_log_checked, parse_duration_seconds


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (30, 30),
        ("45s", 45),
        ("30m", 1800),
        ("1h", 3600),
    ],
)
def test_parse_duration_seconds_valid(spec, expected):
    assert parse_duration_seconds(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        0,
        -5,
        "garbage",
        "5x",
        "30 m",
        None,
        [],
    ],
)
def test_parse_duration_seconds_invalid(spec):
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration_seconds(spec)


def test_day_log_checked_raises_while_day_log_swallows(monkeypatch):
    def fail_write(dir_path, message):
        raise OSError("blocked")

    monkeypatch.setattr(think_utils, "_write_task_log", fail_write)

    with pytest.raises(OSError, match="blocked"):
        day_log_checked("20260315", "checked")

    day_log("20260315", "best effort")

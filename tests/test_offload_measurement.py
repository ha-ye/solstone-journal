# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think import offload_measurement
from solstone.think.offload_measurement import (
    device_free_bytes,
    device_total_bytes,
    measure_raw_media_usage,
    suggest_offload_defaults,
)
from solstone.think.retention import compute_storage_summary

GB = 10**9


def _segment(journal: Path, day: str, stream: str = "archon") -> Path:
    path = journal / "chronicle" / day / stream / "120000_300"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_bytes(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_raw_media_measurement_matches_retention_predicate_non_recursive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    segment = _segment(tmp_path, "20260101")
    _write_bytes(segment / "audio.wav", 10)
    _write_bytes(segment / "clip.mp4", 20)
    _write_bytes(segment / "frame.png", 30)
    _write_bytes(segment / "monitor_0_diff.png", 40)
    _write_bytes(segment / "audio.jsonl", 50)
    _write_bytes(segment / "note.json", 60)
    _write_bytes(segment / "talents" / "frame.png", 70)

    usage = measure_raw_media_usage()

    assert usage.total_bytes == 100
    assert usage.total_files == 4
    assert usage.per_day == (offload_measurement.RawMediaDayUsage("20260101", 100, 4),)
    assert compute_storage_summary().raw_media_bytes == usage.total_bytes


def test_raw_media_per_day_breakdown_is_chronological(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_bytes(_segment(tmp_path, "20260301") / "audio.wav", 3)
    _write_bytes(_segment(tmp_path, "20260101") / "audio.wav", 1)
    _write_bytes(_segment(tmp_path, "20260201") / "audio.wav", 2)

    usage = measure_raw_media_usage()

    assert [day.day for day in usage.per_day] == [
        "20260101",
        "20260201",
        "20260301",
    ]
    assert [day.bytes for day in usage.per_day] == [1, 2, 3]


def test_raw_media_measurement_tolerates_file_vanishing_before_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    segment = _segment(tmp_path, "20260101")
    vanishing = _write_bytes(segment / "vanishing.wav", 10)
    stable = _write_bytes(segment / "stable.wav", 20)

    def fake_get_raw_media_files(_segment_path: Path) -> list[Path]:
        vanishing.unlink()
        return [vanishing, stable]

    monkeypatch.setattr(
        offload_measurement, "get_raw_media_files", fake_get_raw_media_files
    )

    usage = measure_raw_media_usage()

    assert usage.total_bytes == 20
    assert usage.total_files == 1


def test_device_free_bytes_uses_disk_usage_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    def fake_disk_usage(path: Path) -> SimpleNamespace:
        assert path == tmp_path
        return SimpleNamespace(total=1000 * GB, used=100 * GB, free=850 * GB)

    monkeypatch.setattr(offload_measurement.shutil, "disk_usage", fake_disk_usage)

    assert device_free_bytes() == 850 * GB


def test_device_total_bytes_uses_disk_usage_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    def fake_disk_usage(path: Path) -> SimpleNamespace:
        assert path == tmp_path
        return SimpleNamespace(total=1000 * GB, used=100 * GB, free=850 * GB)

    monkeypatch.setattr(offload_measurement.shutil, "disk_usage", fake_disk_usage)

    assert device_total_bytes() == 1000 * GB


@pytest.mark.parametrize(
    ("total", "budget", "floor"),
    [
        (1000 * GB, 500 * GB, 100 * GB),
        (100 * GB, 50 * GB, 20 * GB),
        (30 * GB, 15 * GB, 7_500_000_000),
    ],
)
def test_suggest_offload_defaults_decimal_gb(
    total: int, budget: int, floor: int
) -> None:
    defaults = suggest_offload_defaults(total)

    assert defaults.budget_bytes == budget
    assert defaults.floor_bytes == floor

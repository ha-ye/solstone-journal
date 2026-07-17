# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import solstone.think.entities.observations as observations


@pytest.fixture(autouse=True)
def _fast_observation_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fast_atomic_replace(
        path: Path, data: str | bytes, *, mode: int | None = None
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)

    monkeypatch.setattr(observations, "atomic_replace", fast_atomic_replace)


def _set_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _row(
    content: str,
    observed_at: int,
    source_day: str = "20260301",
) -> dict[str, Any]:
    return {
        "content": content,
        "observed_at": observed_at,
        "source_day": source_day,
    }


def _write_observations(facet: str, name: str, rows: list[dict[str, Any]]) -> Path:
    observations.save_observations(facet, name, rows)
    return observations.observations_file_path(facet, name)


def _read_raw(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_raw_observation_lines(facet: str, name: str, lines: list[str]) -> Path:
    path = observations.observations_file_path(facet, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _install_clock(monkeypatch: pytest.MonkeyPatch, *ticks: int) -> None:
    values = iter(ticks)
    monkeypatch.setattr(observations, "now_ms", lambda: next(values))


def test_observation_day_counts_counts_only_valid_source_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_raw_observation_lines(
        "work",
        "Alice Example",
        [
            json.dumps({"content": "First", "source_day": "20260401"}),
            json.dumps({"content": "Second", "source_day": "20260401"}),
            json.dumps({"content": "Third", "source_day": "20260402"}),
            json.dumps({"content": "Undated"}),
            json.dumps({"content": "Integer day", "source_day": 1}),
            json.dumps({"content": "Dashed day", "source_day": "2026-04-03"}),
            json.dumps("not a dict"),
            "{malformed",
            "",
        ],
    )

    assert observations.observation_day_counts("work", "Alice Example") == {
        "20260401": 2,
        "20260402": 1,
    }


def test_observation_day_counts_empty_for_missing_file_and_empty_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)

    assert observations.observation_day_counts("work", "Missing Entity") == {}
    assert observations.observation_day_counts("work", "") == {}


def test_observation_count_predicates_can_diverge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    facet = "work"
    name = "Predicate Divergence"
    d1 = "20260401"
    d2 = "20260402"
    _write_raw_observation_lines(
        facet,
        name,
        [
            json.dumps({"content": "First dated", "source_day": d1}),
            json.dumps({"content": "Second dated", "source_day": d1}),
            json.dumps({"content": "Third dated", "source_day": d2}),
            json.dumps({"content": "Parseable undated"}),
            "{malformed",
        ],
    )

    assert observations.count_observations(facet, name) == 5
    assert len(observations.load_observations(facet, name)) == 4
    assert observations.observation_day_counts(facet, name) == {
        d1: 2,
        d2: 1,
    }


def test_record_observation_ops_consolidates_remote_control_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _install_clock(monkeypatch, 1000, 1001)
    facet = "work"
    name = "remote_control_project"
    initial = [
        _row(
            "Remote Control Project supports browser automation for remote support.",
            1,
        ),
        _row(
            "Remote Control Project requires operator confirmation before connecting.",
            2,
        ),
        _row(
            "Remote Control Project supports browser automation for remote support.",
            3,
        ),
        _row("Remote Control Project stores session audit records.", 4),
        _row(
            "Remote Control Project supports browser automation for remote support.",
            5,
        ),
        _row("Remote Control Project uses a relay for live control sessions.", 6),
    ]
    _write_observations(facet, name, initial)

    counts = observations.record_observation_ops(
        facet,
        name,
        [
            {
                "op": "drop",
                "target_index": 2,
                "target_quote": "browser automation",
            },
            {
                "op": "update",
                "target_index": 0,
                "content": (
                    "Remote Control Project supports browser automation for remote "
                    "support with operator confirmation."
                ),
                "target_quote": "SUPPORTS BROWSER AUTOMATION",
            },
            {
                "op": "drop",
                "target_index": 4,
                "target_quote": "browser automation",
            },
            {
                "op": "add",
                "content": (
                    "Remote Control Project treats remote access as an "
                    "operator-owned workflow."
                ),
            },
        ],
        "20260401",
    )

    assert counts == {"update": 1, "add": 1, "drop": 2, "keep": 0, "skipped": 0}
    assert observations.load_observations(facet, name) == [
        {
            "content": (
                "Remote Control Project supports browser automation for remote "
                "support with operator confirmation."
            ),
            "observed_at": 1000,
            "source_day": "20260401",
        },
        initial[1],
        initial[3],
        initial[5],
        {
            "content": (
                "Remote Control Project treats remote access as an "
                "operator-owned workflow."
            ),
            "observed_at": 1001,
            "source_day": "20260401",
        },
    ]


def test_record_observation_ops_skips_invalid_inputs_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    facet = "work"
    name = "remote_control_project"
    initial = [_row("Remote Control Project requires operator confirmation.", 1)]
    path = _write_observations(facet, name, initial)
    before = _read_raw(path)

    counts = observations.record_observation_ops(
        facet,
        name,
        [
            {
                "op": "drop",
                "target_index": 99,
                "target_quote": "operator confirmation",
            },
            {
                "op": "update",
                "target_index": 0,
                "content": "Remote Control Project requires two-person approval.",
                "target_quote": "missing quote",
            },
            {"op": "add", "content": "  "},
        ],
        "20260401",
    )

    assert counts == {"update": 0, "add": 0, "drop": 0, "keep": 0, "skipped": 3}
    assert observations.load_observations(facet, name) == initial
    assert _read_raw(path) == before


def test_record_observation_ops_uses_original_snapshot_indices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _install_clock(monkeypatch, 2000)
    facet = "work"
    name = "remote_control_project"
    initial = [
        _row("Index zero should be dropped.", 1),
        _row("Index one should survive unchanged.", 2),
        _row("Index two should be updated.", 3),
    ]
    _write_observations(facet, name, initial)

    counts = observations.record_observation_ops(
        facet,
        name,
        [
            {"op": "drop", "target_index": 0, "target_quote": "dropped"},
            {
                "op": "update",
                "target_index": 2,
                "content": "Index two was updated using the original index.",
                "target_quote": "Index two",
            },
        ],
        "20260402",
    )

    assert counts == {"update": 1, "add": 0, "drop": 1, "keep": 0, "skipped": 0}
    assert observations.load_observations(facet, name) == [
        initial[1],
        {
            "content": "Index two was updated using the original index.",
            "observed_at": 2000,
            "source_day": "20260402",
        },
    ]


def test_record_observation_ops_holds_lock_across_read_and_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _install_clock(monkeypatch, 3000)
    facet = "work"
    name = "remote_control_project"
    initial = [_row("Remote Control Project has a lock-sensitive row.", 1)]
    _write_observations(facet, name, initial)
    expected_path = observations.observations_file_path(facet, name)
    lock_active = False
    entered_paths: list[Path] = []
    read_inside_lock: list[bool] = []
    write_inside_lock: list[bool] = []
    real_load = observations.load_observations
    real_save = observations.save_observations

    @contextmanager
    def spy_lock(path: Path) -> Iterator[None]:
        nonlocal lock_active
        entered_paths.append(path)
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    def spy_load(facet_arg: str, name_arg: str) -> list[dict[str, Any]]:
        read_inside_lock.append(lock_active)
        return real_load(facet_arg, name_arg)

    def spy_save(facet_arg: str, name_arg: str, rows: list[dict[str, Any]]) -> None:
        write_inside_lock.append(lock_active)
        real_save(facet_arg, name_arg, rows)

    monkeypatch.setattr(observations, "hold_lock", spy_lock)
    monkeypatch.setattr(observations, "load_observations", spy_load)
    monkeypatch.setattr(observations, "save_observations", spy_save)

    counts = observations.record_observation_ops(
        facet,
        name,
        [
            {
                "op": "update",
                "target_index": 0,
                "content": "Remote Control Project updates while locked.",
            }
        ],
        "20260403",
    )

    assert counts == {"update": 1, "add": 0, "drop": 0, "keep": 0, "skipped": 0}
    assert entered_paths == [expected_path]
    assert read_inside_lock == [True]
    assert write_inside_lock == [True]


def test_record_observation_ops_on_zero_observations_skips_indexed_ops_and_adds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _install_clock(monkeypatch, 4000)
    facet = "work"
    name = "remote_control_project"

    counts = observations.record_observation_ops(
        facet,
        name,
        [
            {"op": "drop", "target_index": 0},
            {
                "op": "update",
                "target_index": 0,
                "content": "This update has no target.",
            },
            {
                "op": "add",
                "content": "Remote Control Project starts with an added observation.",
            },
        ],
        "20260404",
    )

    assert counts == {"update": 0, "add": 1, "drop": 0, "keep": 0, "skipped": 2}
    assert observations.load_observations(facet, name) == [
        {
            "content": "Remote Control Project starts with an added observation.",
            "observed_at": 4000,
            "source_day": "20260404",
        }
    ]


def test_record_observation_ops_empty_ops_do_not_churn_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    facet = "work"
    absent_name = "remote_control_project"
    absent_path = observations.observations_file_path(facet, absent_name)

    absent_counts = observations.record_observation_ops(facet, absent_name, [])

    assert absent_counts == {
        "update": 0,
        "add": 0,
        "drop": 0,
        "keep": 0,
        "skipped": 0,
    }
    assert not absent_path.exists()

    existing_name = "existing_remote_control_project"
    existing_rows = [_row("Existing observation remains unchanged.", 1)]
    existing_path = _write_observations(facet, existing_name, existing_rows)
    before = _read_raw(existing_path)

    existing_counts = observations.record_observation_ops(facet, existing_name, [])

    assert existing_counts == {
        "update": 0,
        "add": 0,
        "drop": 0,
        "keep": 0,
        "skipped": 0,
    }
    assert _read_raw(existing_path) == before


def test_record_observation_ops_keep_only_does_not_churn_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    facet = "work"
    name = "remote_control_project"
    initial = [_row("Remote Control Project keeps this observation.", 1)]
    path = _write_observations(facet, name, initial)
    before = _read_raw(path)

    counts = observations.record_observation_ops(
        facet,
        name,
        [{"op": "keep", "target_index": 0, "target_quote": "keeps this"}],
    )

    assert counts == {"update": 0, "add": 0, "drop": 0, "keep": 1, "skipped": 0}
    assert observations.load_observations(facet, name) == initial
    assert _read_raw(path) == before

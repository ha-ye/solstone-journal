# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

from solstone.apps.transcripts import routes
from solstone.think.journal_stats import SCHEMA_VERSION, JournalStats

REFERENCE_DAY = "20260304"
SIBLING_DAY = "20260305"
MONTH = "202603"
REFERENCE_COUNT = 6


def _populate_day_caches(journal: Path) -> None:
    JournalStats().scan(str(journal))


def _day_dir(journal: Path, day: str = REFERENCE_DAY) -> Path:
    return journal / "chronicle" / day


def _cache_file(journal: Path, day: str = REFERENCE_DAY) -> Path:
    return _day_dir(journal, day) / "stats.json"


def _make_cache_newer_than_inputs(journal: Path, day: str = REFERENCE_DAY) -> None:
    day_dir = _day_dir(journal, day)
    cache_file = _cache_file(journal, day)
    newest = max(path.stat().st_mtime for path in day_dir.rglob("*") if path.is_file())
    os.utime(cache_file, (newest + 2, newest + 2))


def _write_old_schema_cache(journal: Path, day: str = REFERENCE_DAY) -> None:
    cache_file = _cache_file(journal, day)
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    payload["schema_version"] = SCHEMA_VERSION - 1
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    _make_cache_newer_than_inputs(journal, day)


def _write_corrupt_cache(journal: Path, day: str = REFERENCE_DAY) -> None:
    cache_file = _cache_file(journal, day)
    cache_file.write_text("{bad json\n", encoding="utf-8")
    _make_cache_newer_than_inputs(journal, day)


def _make_cache_stale(journal: Path, day: str = REFERENCE_DAY) -> None:
    cache_file = _cache_file(journal, day)
    source = next(_day_dir(journal, day).glob("*/*/*audio.jsonl"))
    newer = cache_file.stat().st_mtime + 2
    os.utime(source, (newer, newer))


def _stats_snapshot(journal: Path) -> dict[Path, int]:
    return {
        path: path.stat().st_mtime_ns
        for path in sorted((journal / "chronicle").glob("*/stats.json"))
    }


def _assert_stats_snapshot_unchanged(journal: Path, before: dict[Path, int]) -> None:
    assert _stats_snapshot(journal) == before


def _get_month(client: Any) -> dict[str, int]:
    response = client.get(f"/app/transcripts/api/stats/{MONTH}")
    assert response.status_code == 200
    return response.get_json()


def test_hit_returns_count_without_raw_scanner(
    client: Any,
    journal_copy: Path,
    monkeypatch: Any,
) -> None:
    _populate_day_caches(journal_copy)
    scanner = Mock(side_effect=AssertionError("raw scanner should not be called"))
    monkeypatch.setattr(routes, "cluster_scan", scanner)

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT
    scanner.assert_not_called()


def test_fallback_on_missing_cache(
    client: Any,
    journal_copy: Path,
    monkeypatch: Any,
) -> None:
    _populate_day_caches(journal_copy)
    _cache_file(journal_copy).unlink()
    real_cluster_scan = routes.cluster_scan
    scanner = Mock(side_effect=real_cluster_scan)
    monkeypatch.setattr(routes, "cluster_scan", scanner)

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT
    scanner.assert_has_calls([call(REFERENCE_DAY)])


def test_fallback_on_old_schema(client: Any, journal_copy: Path) -> None:
    _populate_day_caches(journal_copy)
    _write_old_schema_cache(journal_copy)

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT


def test_fallback_on_stale_current_schema(
    client: Any,
    journal_copy: Path,
) -> None:
    _populate_day_caches(journal_copy)
    _make_cache_stale(journal_copy)

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT


def test_fallback_on_corrupt_cache(client: Any, journal_copy: Path) -> None:
    _populate_day_caches(journal_copy)
    _write_corrupt_cache(journal_copy)

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT


def test_mixed_month_isolates_fallback(
    client: Any,
    journal_copy: Path,
    monkeypatch: Any,
) -> None:
    _populate_day_caches(journal_copy)
    _write_corrupt_cache(journal_copy, SIBLING_DAY)
    real_cluster_scan = routes.cluster_scan
    scanner = Mock(side_effect=real_cluster_scan)
    monkeypatch.setattr(routes, "cluster_scan", scanner)

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT
    assert body[SIBLING_DAY] == REFERENCE_COUNT
    assert scanner.call_args_list == [call(SIBLING_DAY)]


def test_read_only_across_all_paths(client: Any, journal_copy: Path) -> None:
    scenarios = (
        lambda: None,
        lambda: _make_cache_stale(journal_copy),
        lambda: _write_corrupt_cache(journal_copy),
        lambda: _cache_file(journal_copy).unlink(),
    )

    for mutate in scenarios:
        _populate_day_caches(journal_copy)
        mutate()
        before = _stats_snapshot(journal_copy)

        body = _get_month(client)

        assert body[REFERENCE_DAY] == REFERENCE_COUNT
        _assert_stats_snapshot_unchanged(journal_copy, before)


def test_response_shape_and_invalid_month(client: Any, journal_copy: Path) -> None:
    _populate_day_caches(journal_copy)
    zero_day = journal_copy / "chronicle" / "20260331"
    zero_day.mkdir(parents=True)

    body = _get_month(client)

    assert "20260331" not in body

    response = client.get("/app/transcripts/api/stats/notamonth")
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_month"


def test_root_aggregate_independence(client: Any, journal_copy: Path) -> None:
    _populate_day_caches(journal_copy)
    (journal_copy / "stats.json").write_text("{bad json\n", encoding="utf-8")

    body = _get_month(client)

    assert body[REFERENCE_DAY] == REFERENCE_COUNT

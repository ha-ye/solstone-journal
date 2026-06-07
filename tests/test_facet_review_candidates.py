# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

import solstone.think.facet_review_candidates as mod
from solstone.think.facet_review_candidates import (
    accept_candidate,
    candidate_key,
    dismiss_candidate,
    facet_review_candidates_dir,
    facet_review_candidates_path,
    find_candidate,
    load_candidates,
    locked_modify_candidates,
    record_facet_candidate,
    save_candidates,
    touch_updated,
    utc_now_iso,
)


@pytest.fixture
def candidate_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return Path(tmp_path)


def test_facet_review_candidates_dir_creates_dir(candidate_journal):
    path = facet_review_candidates_dir()

    assert path == candidate_journal / "facets"
    assert path.exists()
    assert path.is_dir()


def test_path_helpers_return_expected_names(candidate_journal):
    assert (
        facet_review_candidates_path()
        == candidate_journal / "facets" / "review-candidates.jsonl"
    )


def test_load_candidates_missing_file_returns_empty(candidate_journal):
    assert load_candidates() == []


def test_save_and_load_candidates_roundtrip(candidate_journal):
    rows = [
        {"name": "Home Reno", "name_key": "home reno"},
        {"name": "Field Notes", "name_key": "field notes"},
    ]

    save_candidates(rows)

    assert load_candidates() == rows


def test_save_candidates_empty_list_writes_empty_file(candidate_journal):
    save_candidates([])

    assert facet_review_candidates_path().read_text(encoding="utf-8") == ""


def test_load_candidates_skips_malformed_line(candidate_journal, caplog):
    facet_review_candidates_path().write_text(
        '{"name_key": "home reno"}\nnot-json\n',
        encoding="utf-8",
    )

    rows = load_candidates()

    assert rows == [{"name_key": "home reno"}]
    assert "malformed JSONL line 2" in caplog.text


def test_load_candidates_warns_on_non_dict_line(candidate_journal, caplog):
    facet_review_candidates_path().write_text(
        '{"name_key": "home reno"}\n[1, 2]\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        rows = load_candidates()

    assert rows == [{"name_key": "home reno"}]
    assert "non-object JSONL line 2" in caplog.text
    assert "list" in caplog.text


def test_candidate_key_is_deterministic_and_distinct(candidate_journal):
    assert candidate_key("home reno") == candidate_key("home reno")
    assert candidate_key("home reno") != candidate_key("home-reno")
    assert candidate_key("home reno") != candidate_key("field notes")


def test_find_candidate_returns_row_or_none(candidate_journal):
    rows = [
        {"name": "Home Reno", "name_key": "home reno"},
        {"name": "Field Notes", "name_key": "field notes"},
    ]

    assert find_candidate(rows, "field notes") == rows[1]
    assert find_candidate(rows, "missing") is None


def test_utc_now_iso_ends_with_z(candidate_journal):
    assert utc_now_iso().endswith("Z")


def test_touch_updated_sets_updated_at(candidate_journal):
    row = {}

    touch_updated(row)

    assert row["updated_at"].endswith("Z")


def test_accept_candidate_sets_status_and_updated_at(candidate_journal, monkeypatch):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    save_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 3,
                "updated_at": "2026-06-02T17:30:00Z",
            }
        ]
    )

    row = accept_candidate("home reno")

    assert row is not None
    assert row["status"] == "accepted"
    assert row["updated_at"] == "2026-06-03T17:30:00Z"
    assert load_candidates()[0]["status"] == "accepted"


def test_accept_candidate_missing_returns_none(candidate_journal):
    assert accept_candidate("missing") is None


def test_dismiss_candidate_sets_status_watermark_and_updated_at(
    candidate_journal, monkeypatch
):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    save_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 4,
                "updated_at": "2026-06-02T17:30:00Z",
            }
        ]
    )

    row = dismiss_candidate("home reno")

    assert row is not None
    assert row["status"] == "dismissed"
    assert row["dismissed_count"] == 4
    assert row["updated_at"] == "2026-06-03T17:30:00Z"
    assert load_candidates()[0]["dismissed_count"] == 4


def test_dismiss_candidate_missing_returns_none(candidate_journal):
    assert dismiss_candidate("missing") is None


def test_locked_modify_candidates_applies_fn_and_persists(candidate_journal):
    def mutate(rows):
        return list(rows) + [{"name": "Home Reno", "name_key": "home reno"}]

    updated = locked_modify_candidates(mutate)

    assert updated == [{"name": "Home Reno", "name_key": "home reno"}]
    assert load_candidates() == [{"name": "Home Reno", "name_key": "home reno"}]


def test_locked_modify_candidates_serializes_threads(candidate_journal):
    barrier = threading.Barrier(4)
    exceptions: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()

            def mutate(rows):
                next_rows = list(rows)
                next_rows.append(
                    {
                        "name": f"Candidate {i}",
                        "name_key": f"candidate {i}",
                    }
                )
                return next_rows

            locked_modify_candidates(mutate)
        except BaseException as exc:  # pragma: no cover - assertion surface
            exceptions.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert exceptions == []
    rows = load_candidates()
    assert sorted(row["name_key"] for row in rows) == [
        "candidate 0",
        "candidate 1",
        "candidate 2",
        "candidate 3",
    ]


def test_record_facet_candidate_creates_one_row(candidate_journal, monkeypatch):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-02T17:30:00Z")
    samples = [{"day": "20260602", "stream": "archon", "segment": "090000_300"}]

    row = record_facet_candidate(
        "Home Reno",
        "home reno",
        3,
        14,
        samples,
        "20260602",
    )

    rows = load_candidates()
    assert rows == [row]
    assert row["name"] == "Home Reno"
    assert row["name_key"] == "home reno"
    assert row["status"] == "open"
    assert row["count"] == 3
    assert row["window_days"] == 14
    assert row["evidence"] == {"samples": samples}
    assert row["first_surfaced"] == "20260602"
    assert row["last_surfaced"] == "20260602"
    assert row["created_at"] == "2026-06-02T17:30:00Z"
    assert row["updated_at"] == "2026-06-02T17:30:00Z"


def test_record_facet_candidate_upserts_idempotently(candidate_journal):
    first_samples = [{"day": "20260602", "stream": "archon", "segment": "090000_300"}]
    second_samples = [{"day": "20260603", "stream": "archon", "segment": "100000_300"}]

    record_facet_candidate("Home Reno", "home reno", 3, 14, first_samples, "20260602")
    record_facet_candidate("home reno", "home reno", 4, 14, second_samples, "20260603")

    rows = load_candidates()
    assert len(rows) == 1
    assert rows[0]["name"] == "Home Reno"
    assert rows[0]["count"] == 4
    assert rows[0]["evidence"]["samples"] == second_samples


def test_record_facet_candidate_update_refreshes_expected_fields(
    candidate_journal, monkeypatch
):
    times = iter(["2026-06-02T17:30:00Z", "2026-06-03T17:30:00Z"])
    monkeypatch.setattr(mod, "utc_now_iso", lambda: next(times))
    first_samples = [{"day": "20260602", "stream": "archon", "segment": "090000_300"}]
    second_samples = [
        {"day": "20260603", "stream": "archon", "segment": "100000_300"},
        {"day": "20260603", "stream": "archon", "segment": "103000_300"},
    ]

    record_facet_candidate("Home Reno", "home reno", 3, 14, first_samples, "20260602")
    row = record_facet_candidate(
        "home reno",
        "home reno",
        5,
        21,
        second_samples,
        "20260603",
    )

    assert row["name"] == "Home Reno"
    assert row["name_key"] == "home reno"
    assert row["count"] == 5
    assert row["window_days"] == 21
    assert row["evidence"]["samples"] == second_samples
    assert row["first_surfaced"] == "20260602"
    assert row["last_surfaced"] == "20260603"
    assert row["created_at"] == "2026-06-02T17:30:00Z"
    assert row["updated_at"] == "2026-06-03T17:30:00Z"


def test_record_facet_candidate_preserves_status_and_unknown_keys(
    candidate_journal, monkeypatch
):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    old_samples = [{"day": "20260602", "stream": "archon", "segment": "090000_300"}]
    new_samples = [{"day": "20260603", "stream": "archon", "segment": "100000_300"}]
    save_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "dismissed",
                "count": 3,
                "window_days": 14,
                "evidence": {"samples": old_samples, "review_note": "preserve"},
                "first_surfaced": "20260602",
                "last_surfaced": "20260602",
                "created_at": "2026-06-02T17:30:00Z",
                "updated_at": "2026-06-02T17:30:00Z",
                "note": "keep me",
            }
        ]
    )

    row = record_facet_candidate(
        "home reno",
        "home reno",
        4,
        14,
        new_samples,
        "20260603",
    )

    assert row["status"] == "dismissed"
    assert row["note"] == "keep me"
    assert row["evidence"]["review_note"] == "preserve"
    assert row["evidence"]["samples"] == new_samples

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from solstone.think.entities.review_candidates import (
    accept_candidate,
    candidate_key,
    dismiss_candidate,
    find_candidate,
    load_candidates,
    locked_modify_candidates,
    review_candidates_dir,
    review_candidates_path,
    save_candidates,
    touch_updated,
    utc_now_iso,
)


@pytest.fixture
def candidate_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return Path(tmp_path)


def test_review_candidates_dir_creates_dir(candidate_journal):
    path = review_candidates_dir()

    assert path == candidate_journal / "entities"
    assert path.exists()
    assert path.is_dir()


def test_path_helpers_return_expected_names(candidate_journal):
    assert (
        review_candidates_path()
        == candidate_journal / "entities" / "review-candidates.jsonl"
    )


def test_load_candidates_missing_file_returns_empty(candidate_journal):
    assert load_candidates() == []


def test_save_and_load_candidates_roundtrip(candidate_journal):
    rows = [
        {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        {"facet": "personal", "source_slug": "bob", "target_slug": "robert"},
    ]

    save_candidates(rows)

    assert load_candidates() == rows


def test_save_candidates_empty_list_writes_empty_file(candidate_journal):
    save_candidates([])

    assert review_candidates_path().read_text(encoding="utf-8") == ""


def test_load_candidates_skips_malformed_line(candidate_journal, caplog):
    review_candidates_path().write_text(
        '{"source_slug": "kognova_inc"}\nnot-json\n',
        encoding="utf-8",
    )

    rows = load_candidates()

    assert rows == [{"source_slug": "kognova_inc"}]
    assert "malformed JSONL line 2" in caplog.text


def test_load_candidates_warns_on_non_dict_line(candidate_journal, caplog):
    review_candidates_path().write_text(
        '{"source_slug": "kognova_inc"}\n[1, 2]\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        rows = load_candidates()

    assert rows == [{"source_slug": "kognova_inc"}]
    assert "non-object JSONL line 2" in caplog.text
    assert "list" in caplog.text


def test_candidate_key_is_deterministic_and_distinct(candidate_journal):
    assert candidate_key("work", "source", "target") == candidate_key(
        "work", "source", "target"
    )
    assert candidate_key("work", "source", "target") != candidate_key(
        "work", "target", "source"
    )
    assert candidate_key("work", "source", "target") != candidate_key(
        "personal", "source", "target"
    )


def test_find_candidate_returns_row_or_none(candidate_journal):
    rows = [
        {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        {"facet": "personal", "source_slug": "bob", "target_slug": "robert"},
    ]

    assert find_candidate(rows, "personal", "bob", "robert") == rows[1]
    assert find_candidate(rows, "work", "missing", "kognova") is None


def test_utc_now_iso_ends_with_z(candidate_journal):
    assert utc_now_iso().endswith("Z")


def test_touch_updated_sets_updated_at(candidate_journal):
    row = {}

    touch_updated(row)

    assert row["updated_at"].endswith("Z")


def test_accept_candidate_sets_status_and_updated_at(candidate_journal, monkeypatch):
    import solstone.think.entities.review_candidates as mod

    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    save_candidates(
        [
            {
                "facet": "work",
                "source_slug": "kognova_inc",
                "target_slug": "kognova",
                "status": "open",
                "updated_at": "2026-06-02T17:30:00Z",
            }
        ]
    )

    row = accept_candidate("work", "kognova_inc", "kognova")

    assert row is not None
    assert row["status"] == "accepted"
    assert row["updated_at"] == "2026-06-03T17:30:00Z"
    assert load_candidates()[0]["status"] == "accepted"


def test_accept_candidate_missing_returns_none(candidate_journal):
    assert accept_candidate("work", "missing", "kognova") is None


def test_dismiss_candidate_sets_status_watermark_and_updated_at(
    candidate_journal, monkeypatch
):
    import solstone.think.entities.review_candidates as mod

    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    save_candidates(
        [
            {
                "facet": "work",
                "source_slug": "kognova_inc",
                "target_slug": "kognova",
                "status": "open",
                "evidence": {"detection_count": 4},
                "updated_at": "2026-06-02T17:30:00Z",
            }
        ]
    )

    row = dismiss_candidate("work", "kognova_inc", "kognova")

    assert row is not None
    assert row["status"] == "dismissed"
    assert row["dismissed_detection_count"] == 4
    assert row["updated_at"] == "2026-06-03T17:30:00Z"
    assert load_candidates()[0]["dismissed_detection_count"] == 4


def test_dismiss_candidate_missing_returns_none(candidate_journal):
    assert dismiss_candidate("work", "missing", "kognova") is None


def test_locked_modify_candidates_applies_fn_and_persists(candidate_journal):
    def mutate(rows):
        return list(rows) + [{"facet": "work", "source_slug": "s", "target_slug": "t"}]

    updated = locked_modify_candidates(mutate)

    assert updated == [{"facet": "work", "source_slug": "s", "target_slug": "t"}]
    assert load_candidates() == [
        {"facet": "work", "source_slug": "s", "target_slug": "t"}
    ]


def test_locked_modify_candidates_does_not_retry_on_write_error(candidate_journal):
    def mutate(rows):
        return list(rows) + [{"facet": "work", "source_slug": "s", "target_slug": "t"}]

    with patch(
        "solstone.think.entities.review_candidates.atomic_replace",
        side_effect=PermissionError("Simulated write error"),
    ) as atomic_replace:
        with pytest.raises(OSError):
            locked_modify_candidates(mutate)

    assert atomic_replace.call_count == 1


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
                        "facet": "work",
                        "source_slug": f"s{i}",
                        "target_slug": "target",
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
    assert sorted(row["source_slug"] for row in rows) == ["s0", "s1", "s2", "s3"]

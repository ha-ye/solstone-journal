# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

import solstone.think.speaker_review_candidates as mod
from solstone.think.speaker_review_candidates import (
    accept_candidate,
    candidate_key,
    dismiss_candidate,
    find_candidate,
    load_candidates,
    locked_modify_candidates,
    record_name_variant_candidate,
    review_candidates_dir,
    review_candidates_path,
    save_candidates,
    touch_updated,
    utc_now_iso,
)


@pytest.fixture
def candidate_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def test_review_candidates_dir_creates_dir(candidate_journal):
    path = review_candidates_dir()

    assert path == candidate_journal / "speakers"
    assert path.exists()
    assert path.is_dir()


def test_path_helpers_return_expected_names(candidate_journal):
    assert (
        review_candidates_path()
        == candidate_journal / "speakers" / "review-candidates.jsonl"
    )


def test_load_candidates_missing_file_returns_empty(candidate_journal):
    assert load_candidates() == []


def test_save_and_load_candidates_roundtrip(candidate_journal):
    rows = [
        {"source_id": "alice", "target_id": "alice_johnson"},
        {"source_id": "bob", "target_id": "bob_smith"},
    ]

    save_candidates(rows)

    assert load_candidates() == rows


def test_save_candidates_empty_list_writes_empty_file(candidate_journal):
    save_candidates([])

    assert review_candidates_path().read_text(encoding="utf-8") == ""


def test_load_candidates_skips_malformed_line(candidate_journal, caplog):
    review_candidates_path().write_text(
        '{"source_id": "alice"}\nnot-json\n',
        encoding="utf-8",
    )

    rows = load_candidates()

    assert rows == [{"source_id": "alice"}]
    assert "malformed JSONL line 2" in caplog.text


def test_load_candidates_warns_on_non_dict_line(candidate_journal, caplog):
    review_candidates_path().write_text(
        '{"source_id": "alice"}\n[1, 2]\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        rows = load_candidates()

    assert rows == [{"source_id": "alice"}]
    assert "non-object JSONL line 2" in caplog.text
    assert "list" in caplog.text


def test_candidate_key_is_order_independent(candidate_journal):
    assert candidate_key("alice", "alice_johnson") == "alice|alice_johnson"
    assert candidate_key("alice", "alice_johnson") == candidate_key(
        "alice_johnson", "alice"
    )
    assert candidate_key("alice", "alice_johnson") != candidate_key(
        "alice", "alice_smith"
    )


def test_find_candidate_returns_row_or_none(candidate_journal):
    rows = [
        {"source_id": "alice", "target_id": "alice_johnson"},
        {"source_id": "bob", "target_id": "bob_smith"},
    ]

    assert find_candidate(rows, "alice_johnson", "alice") == rows[0]
    assert find_candidate(rows, "alice", "missing") is None


def test_utc_now_iso_ends_with_z(candidate_journal):
    assert utc_now_iso().endswith("Z")


def test_touch_updated_sets_updated_at(candidate_journal):
    row = {}

    touch_updated(row)

    assert row["updated_at"].endswith("Z")


def test_record_name_variant_candidate_creates_exact_open_row(
    candidate_journal, monkeypatch
):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")

    row, created = record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.9876,
    )

    assert created is True
    assert row == {
        "source_id": "alice",
        "source_label": "Alice",
        "target_id": "alice_johnson",
        "target_label": "Alice Johnson",
        "status": "open",
        "similarity": 0.9876,
        "readiness": "ready",
        "evidence": {
            "basis": "speaker-name-variant",
            "summary": (
                "Alice and Alice Johnson have matching speaker voiceprints "
                "(similarity 0.9876)."
            ),
            "similarity": 0.9876,
            "detection_count": 1,
            "readiness": "ready",
        },
        "first_surfaced": "2026-06-03T17:30:00Z",
        "last_surfaced": "2026-06-03T17:30:00Z",
        "created_at": "2026-06-03T17:30:00Z",
        "updated_at": "2026-06-03T17:30:00Z",
    }
    assert load_candidates() == [row]


def test_record_name_variant_candidate_upserts_opposite_order_and_label_change(
    candidate_journal, monkeypatch
):
    times = iter(["2026-06-03T17:30:00Z", "2026-06-04T17:30:00Z"])
    monkeypatch.setattr(mod, "utc_now_iso", lambda: next(times))

    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.91,
    )
    row, created = record_name_variant_candidate(
        source_id="alice_johnson",
        source_label="Alice J.",
        target_id="alice",
        target_label="Alice A.",
        similarity=0.92,
    )

    assert created is False
    rows = load_candidates()
    assert rows == [row]
    assert row["source_id"] == "alice_johnson"
    assert row["source_label"] == "Alice J."
    assert row["target_id"] == "alice"
    assert row["target_label"] == "Alice A."
    assert row["similarity"] == 0.92
    assert row["evidence"]["similarity"] == 0.92
    assert row["evidence"]["detection_count"] == 2
    assert row["first_surfaced"] == "2026-06-03T17:30:00Z"
    assert row["created_at"] == "2026-06-03T17:30:00Z"
    assert row["last_surfaced"] == "2026-06-04T17:30:00Z"
    assert row["updated_at"] == "2026-06-04T17:30:00Z"


def test_record_name_variant_candidate_preserves_status_and_unknown_keys(
    candidate_journal, monkeypatch
):
    times = iter(
        [
            "2026-06-03T17:30:00Z",
            "2026-06-04T17:30:00Z",
            "2026-06-05T17:30:00Z",
            "2026-06-06T17:30:00Z",
            "2026-06-07T17:30:00Z",
            "2026-06-08T17:30:00Z",
        ]
    )
    monkeypatch.setattr(mod, "utc_now_iso", lambda: next(times))

    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.91,
    )
    accept_candidate("alice", "alice_johnson")
    accepted, _ = record_name_variant_candidate(
        source_id="alice",
        source_label="Alice A.",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.93,
    )
    accepted["custom"] = "keep-me"
    accepted["evidence"]["custom_evidence"] = "keep-me-too"
    save_candidates([accepted])
    dismissed, _ = record_name_variant_candidate(
        source_id="bob",
        source_label="Bob",
        target_id="bob_smith",
        target_label="Bob Smith",
        similarity=0.94,
    )
    dismiss_candidate("bob", "bob_smith")
    dismissed, _ = record_name_variant_candidate(
        source_id="bob",
        source_label="Bobby",
        target_id="bob_smith",
        target_label="Bob Smith",
        similarity=0.95,
    )

    rows = load_candidates()
    accepted_after = find_candidate(rows, "alice", "alice_johnson")
    dismissed_after = find_candidate(rows, "bob", "bob_smith")
    assert accepted_after is not None
    assert dismissed_after is not None
    assert accepted_after["status"] == "accepted"
    assert accepted_after["custom"] == "keep-me"
    assert accepted_after["evidence"]["custom_evidence"] == "keep-me-too"
    assert accepted_after["evidence"]["summary"].startswith("Alice A.")
    assert dismissed_after["status"] == "dismissed"
    assert dismissed_after["evidence"]["summary"].startswith("Bobby")


def test_accept_candidate_sets_status_and_updated_at(candidate_journal, monkeypatch):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    save_candidates(
        [
            {
                "source_id": "alice",
                "target_id": "alice_johnson",
                "status": "open",
                "updated_at": "2026-06-02T17:30:00Z",
            }
        ]
    )

    row = accept_candidate("alice_johnson", "alice")

    assert row is not None
    assert row["status"] == "accepted"
    assert row["updated_at"] == "2026-06-03T17:30:00Z"
    assert load_candidates()[0]["status"] == "accepted"


def test_accept_candidate_missing_returns_none(candidate_journal):
    assert accept_candidate("alice", "missing") is None


def test_dismiss_candidate_sets_status_watermark_and_updated_at(
    candidate_journal, monkeypatch
):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    save_candidates(
        [
            {
                "source_id": "alice",
                "target_id": "alice_johnson",
                "status": "open",
                "evidence": {"detection_count": 4},
                "updated_at": "2026-06-02T17:30:00Z",
            }
        ]
    )

    row = dismiss_candidate("alice_johnson", "alice")

    assert row is not None
    assert row["status"] == "dismissed"
    assert row["dismissed_detection_count"] == 4
    assert row["updated_at"] == "2026-06-03T17:30:00Z"
    assert load_candidates()[0]["dismissed_detection_count"] == 4


def test_dismiss_candidate_missing_returns_none(candidate_journal):
    assert dismiss_candidate("alice", "missing") is None


def test_locked_modify_candidates_applies_fn_and_persists(candidate_journal):
    def mutate(rows):
        return list(rows) + [{"source_id": "alice", "target_id": "alice_johnson"}]

    updated = locked_modify_candidates(mutate)

    assert updated == [{"source_id": "alice", "target_id": "alice_johnson"}]
    assert load_candidates() == [{"source_id": "alice", "target_id": "alice_johnson"}]


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
                        "source_id": f"source_{i}",
                        "target_id": "target",
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
    assert sorted(row["source_id"] for row in rows) == [
        "source_0",
        "source_1",
        "source_2",
        "source_3",
    ]

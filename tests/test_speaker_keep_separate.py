# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

import solstone.think.speaker_keep_separate as store


@pytest.fixture
def keep_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def test_pair_key_is_order_independent(keep_journal):
    assert store.pair_key("alice", "bob") == "alice|bob"
    assert store.pair_key("alice", "bob") == store.pair_key("bob", "alice")


def test_read_paths_do_not_create_speakers_dir(keep_journal):
    assert store.load_events() == []
    assert store.fold_assertions() == []
    assert store.find_assertion("alice", "bob") is None
    assert store.name_variant_pair_suppressed("alice", "bob", 1) is False
    assert store.list_assertions() == []
    assert not (keep_journal / "speakers").exists()


def test_assertion_folds_present_with_watermark(keep_journal, monkeypatch):
    monkeypatch.setattr(store, "utc_now_iso", lambda: "2026-07-20T12:00:00Z")

    store.record_keep_separate_assertion(
        "bob",
        "alice",
        source_kind="explicit_create_near_match",
        operation_id="idop_1",
        detection_count=3,
    )

    assertion = store.find_assertion("alice", "bob")
    assert assertion is not None
    assert assertion.pair_key == "alice|bob"
    assert assertion.entity_id_a == "alice"
    assert assertion.entity_id_b == "bob"
    assert assertion.dismissed_detection_count == 3
    assert assertion.source_count == 1
    assert store.list_assertions()[0]["source_count"] == 1


def test_reassert_higher_detection_count_raises_watermark(keep_journal, monkeypatch):
    times = iter(["2026-07-20T12:00:00Z", "2026-07-20T12:00:01Z"])
    monkeypatch.setattr(store, "utc_now_iso", lambda: next(times))

    store.record_keep_separate_assertion(
        "alice",
        "bob",
        source_kind="explicit_create_near_match",
        operation_id="idop_1",
        detection_count=2,
    )
    store.record_keep_separate_assertion(
        "bob",
        "alice",
        source_kind="explicit_create_near_match",
        operation_id="idop_1",
        detection_count=5,
    )

    assertion = store.find_assertion("alice", "bob")
    assert assertion is not None
    assert assertion.dismissed_detection_count == 5
    assert assertion.source_count == 1
    assert assertion.updated_at == "2026-07-20T12:00:01Z"


def test_tombstone_removes_one_source(keep_journal, monkeypatch):
    times = iter(
        [
            "2026-07-20T12:00:00Z",
            "2026-07-20T12:00:01Z",
            "2026-07-20T12:00:02Z",
        ]
    )
    monkeypatch.setattr(store, "utc_now_iso", lambda: next(times))
    key = store.pair_key("alice", "bob")

    store.record_keep_separate_assertion(
        "alice",
        "bob",
        source_kind="explicit_create_near_match",
        operation_id="idop_1",
        detection_count=2,
    )
    store.record_keep_separate_assertion(
        "alice",
        "bob",
        source_kind="explicit_create_near_match",
        operation_id="idop_2",
        detection_count=7,
    )
    tombstones = store.remove_operation_sources("idop_1", [key])

    assert len(tombstones) == 1
    assertion = store.find_assertion("alice", "bob")
    assert assertion is not None
    assert assertion.dismissed_detection_count == 7
    assert assertion.source_count == 1


def test_all_sources_removed_makes_assertion_absent(keep_journal, monkeypatch):
    times = iter(["2026-07-20T12:00:00Z", "2026-07-20T12:00:01Z"])
    monkeypatch.setattr(store, "utc_now_iso", lambda: next(times))
    key = store.pair_key("alice", "bob")

    store.record_keep_separate_assertion(
        "alice",
        "bob",
        source_kind="explicit_create_near_match",
        operation_id="idop_1",
        detection_count=2,
    )
    store.remove_operation_sources("idop_1", [key])

    assert store.find_assertion("alice", "bob") is None
    assert store.list_assertions() == []


def test_suppression_predicate_boundaries(keep_journal):
    assert store.name_variant_pair_suppressed("alice", "bob", 1) is False

    store.record_keep_separate_assertion(
        "alice",
        "bob",
        source_kind="explicit_create_near_match",
        operation_id="idop_1",
        detection_count=2,
    )

    assert store.name_variant_pair_suppressed("bob", "alice", 1) is True
    assert store.name_variant_pair_suppressed("bob", "alice", 2) is True
    assert store.name_variant_pair_suppressed("bob", "alice", 3) is False


def test_strict_malformed_row_raises(keep_journal):
    store.keep_separate_path(create=True).write_text("not-json\n", encoding="utf-8")

    with pytest.raises(store.KeepSeparateStoreError):
        store.fold_assertions()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

import solstone.think.speaker_cluster_dismissals as store


@pytest.fixture
def dismissal_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def _members(start: int, count: int) -> list[dict[str, object]]:
    return [
        {
            "day": "20260101",
            "stream": "test",
            "segment_key": "090000_300",
            "source": "mic_audio",
            "sentence_id": sid,
        }
        for sid in range(start, start + count)
    ]


def test_identical_members_are_suppressed(dismissal_journal):
    members = _members(1, 3)
    store.record_cluster_dismissal(members, "quiet")

    assert store.cluster_dismissal_suppressed(list(reversed(members))) is True


def test_grown_rescan_sharing_exactly_half_is_suppressed(dismissal_journal):
    store.record_cluster_dismissal(_members(1, 10), "quiet")
    candidate = _members(1, 5) + _members(100, 5)

    assert store.cluster_dismissal_suppressed(candidate) is True


def test_sharing_less_than_half_is_not_suppressed(dismissal_journal):
    store.record_cluster_dismissal(_members(1, 10), "quiet")
    candidate = _members(1, 4) + _members(100, 6)

    assert store.cluster_dismissal_suppressed(candidate) is False


def test_overlapping_events_fold_to_union_without_duplicates(
    dismissal_journal, monkeypatch
):
    times = iter(["2026-07-20T12:00:00Z", "2026-07-20T12:00:01Z"])
    monkeypatch.setattr(store, "utc_now_iso", lambda: next(times))

    store.record_cluster_dismissal(_members(1, 4), "quiet")
    store.record_cluster_dismissal(_members(3, 4), "quiet")

    folded = store.fold_dismissals()
    assert len(folded) == 1
    assert folded[0].member_count == 6
    assert [member["sentence_id"] for member in folded[0].members] == [1, 2, 3, 4, 5, 6]
    assert folded[0].event_count == 2
    assert folded[0].created_at == "2026-07-20T12:00:00Z"
    assert folded[0].updated_at == "2026-07-20T12:00:01Z"


def test_not_a_person_dominates_quiet_on_merge(dismissal_journal, monkeypatch):
    times = iter(["2026-07-20T12:00:00Z", "2026-07-20T12:00:01Z"])
    monkeypatch.setattr(store, "utc_now_iso", lambda: next(times))

    store.record_cluster_dismissal(_members(1, 4), "quiet")
    store.record_cluster_dismissal(_members(3, 4), "not_a_person")

    folded = store.fold_dismissals()
    assert len(folded) == 1
    assert folded[0].disposition == "not_a_person"
    assert store.list_dismissals()[0]["disposition"] == "not_a_person"


def test_second_dismissal_appends_without_rewriting_first_event(dismissal_journal):
    store.record_cluster_dismissal(_members(1, 2), "quiet")
    first = store.cluster_dismissals_path().read_text(encoding="utf-8")

    store.record_cluster_dismissal(_members(10, 2), "quiet")
    second = store.cluster_dismissals_path().read_text(encoding="utf-8")

    assert second.startswith(first)
    assert len(first.splitlines()) == 1
    assert len(second.splitlines()) == 2


def test_strict_malformed_row_raises(dismissal_journal):
    store.cluster_dismissals_path().write_text("not-json\n", encoding="utf-8")

    with pytest.raises(store.ClusterDismissalStoreError):
        store.fold_dismissals()

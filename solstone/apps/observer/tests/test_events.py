# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observer app event handlers."""

from __future__ import annotations

import json

import pytest

from solstone.apps.events import EventContext
from solstone.apps.observer.events import handle_observed, handle_transferred
from solstone.apps.observer.utils import list_observers


@pytest.fixture
def observer_journal(tmp_path, monkeypatch):
    """Create a temporary journal with a observer registered."""
    from solstone.convey import state

    journal = tmp_path / "journal"
    journal.mkdir()

    # Set convey state (used by apps.utils for storage paths)
    monkeypatch.setattr(state, "journal_root", str(journal))

    # Create observers directory
    observers_dir = journal / "apps" / "observer" / "observers"
    observers_dir.mkdir(parents=True)

    # Create a test observer
    observer_data = {
        "key": "testkey123456789abcdef",
        "name": "test-observer",
        "created_at": 1704312000000,
        "last_seen": None,
        "last_segment": None,
        "enabled": True,
        "stats": {
            "segments_received": 5,
            "bytes_received": 1024,
        },
    }
    observer_path = observers_dir / "testkey1.json"
    with open(observer_path, "w") as f:
        json.dump(observer_data, f)

    class Env:
        def __init__(self):
            self.journal = journal
            self.observers_dir = observers_dir
            self.observer_path = observer_path

    return Env()


def test_status_beacon_persisted_allowlisted(observer_env):
    env = observer_env()
    resp = env.register_bound_observer("beacon-test")
    assert resp.status_code == 200
    key = resp.get_json()["key"]

    resp = env.client.post(
        "/app/observer/ingest/event",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "tract": "observe",
            "event": "status",
            "name": "fedora",
            "stream_type": "screen",
            "version": "0.3.1",
            "uptime": 120,
            "last_successful_sync": 1700000000000,
            "pending_queue_depth": "not-an-int",
            "recent_error_count": 1,
            "last_error_reason": "x" * 500,
            "bogus_field": "ignored",
            "uptime_bad": "NaN",
        },
    )

    assert resp.status_code == 200
    record = list_observers()[0]
    beacon = record["health"]["beacon"]
    assert record["last_seen"] is not None
    assert beacon["received_at"] is not None
    assert beacon["name"] == "fedora"
    assert beacon["stream_type"] == "screen"
    assert beacon["version"] == "0.3.1"
    assert beacon["uptime"] == 120
    assert beacon["last_successful_sync"] == 1700000000000
    assert beacon["pending_queue_depth"] is None
    assert beacon["recent_error_count"] == 1
    assert len(beacon["last_error_reason"]) == 200
    assert "bogus_field" not in beacon


def test_legacy_status_event_no_beacon(observer_env):
    env = observer_env()
    resp = env.register_bound_observer("legacy-status-test")
    assert resp.status_code == 200
    key = resp.get_json()["key"]

    resp = env.client.post(
        "/app/observer/ingest/event",
        headers={"Authorization": f"Bearer {key}"},
        json={"tract": "observe", "event": "status"},
    )

    assert resp.status_code == 200
    record = list_observers()[0]
    assert record["last_seen"] is not None
    assert "beacon" not in record.get("health", {})


class TestHandleObserved:
    """Tests for handle_observed event handler."""

    def test_records_observed_for_observer(self, observer_journal):
        """Handler records observed status for observer segment."""
        ctx = EventContext(
            msg={
                "tract": "observe",
                "event": "observed",
                "observer": "test-observer",
                "segment": "120000_300",
                "day": "20250103",
            },
            app="observer",
            tract="observe",
            event="observed",
        )

        handle_observed(ctx)

        # Check history was written
        hist_path = (
            observer_journal.observers_dir / "testkey1" / "hist" / "20250103.jsonl"
        )
        assert hist_path.exists()

        with open(hist_path) as f:
            record = json.loads(f.readline())

        assert record["type"] == "observed"
        assert record["segment"] == "120000_300"
        assert "ts" in record

        # Check stat was incremented
        with open(observer_journal.observer_path) as f:
            data = json.load(f)
        assert data["stats"]["segments_observed"] == 1

    def test_multiple_observed_events(self, observer_journal):
        """Handler appends multiple observed records."""
        for segment in ["120000_300", "130000_300", "140000_300"]:
            ctx = EventContext(
                msg={
                    "tract": "observe",
                    "event": "observed",
                    "observer": "test-observer",
                    "segment": segment,
                    "day": "20250103",
                },
                app="observer",
                tract="observe",
                event="observed",
            )
            handle_observed(ctx)

        # Check all records written
        hist_path = (
            observer_journal.observers_dir / "testkey1" / "hist" / "20250103.jsonl"
        )
        with open(hist_path) as f:
            lines = f.readlines()

        assert len(lines) == 3
        assert json.loads(lines[0])["segment"] == "120000_300"
        assert json.loads(lines[1])["segment"] == "130000_300"
        assert json.loads(lines[2])["segment"] == "140000_300"

        # Check stat incremented 3 times
        with open(observer_journal.observer_path) as f:
            data = json.load(f)
        assert data["stats"]["segments_observed"] == 3

    def test_ignores_non_observer_events(self, observer_journal):
        """Handler ignores events without observer field."""
        ctx = EventContext(
            msg={
                "tract": "observe",
                "event": "observed",
                "segment": "120000_300",
                "day": "20250103",
            },
            app="observer",
            tract="observe",
            event="observed",
        )

        handle_observed(ctx)

        # No history should be created
        hist_dir = observer_journal.observers_dir / "testkey1" / "hist"
        assert not hist_dir.exists()

    def test_ignores_unknown_observer(self, observer_journal):
        """Handler ignores events for unknown observers."""
        ctx = EventContext(
            msg={
                "tract": "observe",
                "event": "observed",
                "observer": "unknown-observer",
                "segment": "120000_300",
                "day": "20250103",
            },
            app="observer",
            tract="observe",
            event="observed",
        )

        handle_observed(ctx)

        # No history should be created for unknown observer
        hist_dir = observer_journal.observers_dir / "testkey1" / "hist"
        assert not hist_dir.exists()

    def test_handles_missing_segment(self, observer_journal):
        """Handler handles events missing segment field."""
        ctx = EventContext(
            msg={
                "tract": "observe",
                "event": "observed",
                "observer": "test-observer",
                "day": "20250103",
            },
            app="observer",
            tract="observe",
            event="observed",
        )

        # Should not raise
        handle_observed(ctx)

        # No history should be created
        hist_dir = observer_journal.observers_dir / "testkey1" / "hist"
        assert not hist_dir.exists()

    def test_handles_missing_day(self, observer_journal):
        """Handler handles events missing day field."""
        ctx = EventContext(
            msg={
                "tract": "observe",
                "event": "observed",
                "observer": "test-observer",
                "segment": "120000_300",
            },
            app="observer",
            tract="observe",
            event="observed",
        )

        # Should not raise
        handle_observed(ctx)

        # No history should be created
        hist_dir = observer_journal.observers_dir / "testkey1" / "hist"
        assert not hist_dir.exists()

    def test_handle_transferred(self, observer_journal, monkeypatch):
        """Handler records transferred status, stats, and queues rescan."""
        import solstone.think.callosum as callosum_module

        calls = []
        monkeypatch.setattr(
            callosum_module,
            "callosum_send",
            lambda *a, **kw: calls.append((a, kw)) or True,
        )

        ctx = EventContext(
            msg={
                "tract": "observe",
                "event": "transferred",
                "observer": "test-observer",
                "segment": "120000_300",
                "day": "20250103",
            },
            app="observer",
            tract="observe",
            event="transferred",
        )

        handle_transferred(ctx)

        hist_path = (
            observer_journal.observers_dir / "testkey1" / "hist" / "20250103.jsonl"
        )
        assert hist_path.exists()
        with open(hist_path) as f:
            record = json.loads(f.readline())

        assert record["type"] == "transferred"
        assert record["segment"] == "120000_300"

        with open(observer_journal.observer_path) as f:
            data = json.load(f)
        assert data["stats"]["segments_transferred"] == 1

        assert calls == [
            (
                ("supervisor", "request"),
                {"cmd": ["journal", "indexer", "--rescan"]},
            )
        ]

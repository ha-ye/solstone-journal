# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for live capture-health derivation."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solstone.think.capture_health import get_capture_health


def _bound_record(**overrides):
    record = {
        "device_binding": {
            "device": "sha256:" + ("a" * 64),
            "kind": "cert",
        }
    }
    record.update(overrides)
    return record


def test_no_last_seen_is_offline(monkeypatch):
    monkeypatch.setattr(
        "solstone.apps.observer.utils.list_observers",
        lambda: [_bound_record(name="x", enabled=True)],
    )

    result = get_capture_health()

    assert result["observers"][0]["status"] == "offline"


def test_disabled_observers_excluded(monkeypatch):
    monkeypatch.setattr(
        "solstone.apps.observer.utils.list_observers",
        lambda: [{"name": "x", "last_seen": 1000, "enabled": False}],
    )

    result = get_capture_health()

    assert result["status"] == "no_observers"


def test_list_observers_raises_returns_unknown(monkeypatch, caplog):
    def _raise() -> list[dict]:
        raise RuntimeError("boom")

    monkeypatch.setattr("solstone.apps.observer.utils.list_observers", _raise)

    with caplog.at_level("DEBUG", logger="solstone.think.capture_health"):
        result = get_capture_health()

    assert result == {"status": "unknown", "observers": []}
    assert "failed to derive capture health" in caplog.text


def test_degraded_status_from_rejection(monkeypatch):
    monkeypatch.setattr("solstone.think.capture_health.now_ms", lambda: 1000)
    monkeypatch.setattr(
        "solstone.apps.observer.utils.list_observers",
        lambda: [
            _bound_record(
                name="fedora",
                enabled=True,
                last_seen=1000,
                health={
                    "ingest_rejection": {
                        "reason_code": "ingest_contract_invalid",
                        "active_count": 1,
                        "first_ts": 900,
                        "latest_ts": 1000,
                        "summary": "screen.jsonl:2: value is not of type 'number'",
                        "stream": "fedora",
                        "version": "0.3.1",
                        "segment": "20260622/120000_300",
                    }
                },
            )
        ],
    )

    result = get_capture_health()

    assert result["status"] == "degraded"
    observer = result["observers"][0]
    assert observer["status"] == "degraded"
    assert observer["ingest_rejection"]["reason_code"] == "ingest_contract_invalid"
    assert "segment" not in observer["ingest_rejection"]


def test_legacy_observer_not_failed(monkeypatch):
    monkeypatch.setattr("solstone.think.capture_health.now_ms", lambda: 1000)
    monkeypatch.setattr(
        "solstone.apps.observer.utils.list_observers",
        lambda: [_bound_record(name="legacy", enabled=True, last_seen=1000)],
    )

    result = get_capture_health()

    assert result["status"] == "active"
    assert result["observers"][0]["status"] == "active"
    assert "ingest_rejection" not in result["observers"][0]

    monkeypatch.setattr(
        "solstone.apps.observer.utils.list_observers",
        lambda: [
            _bound_record(
                name="beacon-only",
                enabled=True,
                last_seen=1000,
                health={"beacon": {"received_at": 1000, "version": "0.3.1"}},
            )
        ],
    )

    result = get_capture_health()

    assert result["status"] == "active"
    assert result["observers"][0]["status"] == "active"
    assert "ingest_rejection" not in result["observers"][0]
    assert result["observers"][0]["beacon"]["version"] == "0.3.1"


def test_unbound_observer_uses_freshness_status(monkeypatch):
    monkeypatch.setattr("solstone.think.capture_health.now_ms", lambda: 1000)
    monkeypatch.setattr(
        "solstone.apps.observer.utils.list_observers",
        lambda: [{"name": "unbound", "enabled": True, "last_seen": 1000}],
    )

    result = get_capture_health()

    assert result["status"] == "active"
    observer = result["observers"][0]
    assert observer["status"] == "active"
    assert "unbound" not in observer

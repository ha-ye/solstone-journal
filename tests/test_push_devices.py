# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.think.push import devices


def _devices_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "push_devices.json"


def _register(
    fingerprint: str,
    token: str,
    *,
    bundle_id: str = "org.solpbc.solstone-swift",
    environment: str = "development",
    platform: str = "ios",
) -> int:
    return devices.register_device(
        fingerprint=fingerprint,
        token=token,
        bundle_id=bundle_id,
        environment=environment,
        platform=platform,
    )


def test_load_devices_returns_empty_for_missing_store(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    assert devices.load_devices() == []


def test_register_one_device_stores_fingerprint_row(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(devices.time, "time", lambda: 1000)

    count = _register("fp-1", "a" * 64)

    assert count == 1
    assert devices.load_devices() == [
        {
            "fingerprint": "fp-1",
            "token": "a" * 64,
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "development",
            "platform": "ios",
            "registered_at": 1000,
        }
    ]
    assert "device_pubkey" not in devices.load_devices()[0]


def test_register_same_fingerprint_replaces_row(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    times = iter([1000, 2000])
    monkeypatch.setattr(devices.time, "time", lambda: next(times))

    first = _register("fp-1", "a" * 64)
    second = _register("fp-1", "b" * 64, environment="production")

    assert first == 1
    assert second == 1
    assert devices.load_devices() == [
        {
            "fingerprint": "fp-1",
            "token": "b" * 64,
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "production",
            "platform": "ios",
            "registered_at": 2000,
        }
    ]


def test_two_fingerprints_coexist(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    assert _register("fp-1", "a" * 64) == 1
    assert _register("fp-2", "b" * 64) == 2

    assert {device["fingerprint"] for device in devices.load_devices()} == {
        "fp-1",
        "fp-2",
    }


def test_one_token_maps_to_one_device(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    token = "c" * 64

    assert _register("fp-1", token) == 1
    assert _register("fp-2", token) == 1

    stored = devices.load_devices()
    assert stored == [
        {
            "fingerprint": "fp-2",
            "token": token,
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "development",
            "platform": "ios",
            "registered_at": stored[0]["registered_at"],
        }
    ]


def test_remove_device_by_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _register("fp-1", "a" * 64)
    _register("fp-2", "b" * 64)

    assert devices.remove_device("fp-1") is True
    assert [device["fingerprint"] for device in devices.load_devices()] == ["fp-2"]

    assert devices.remove_device("missing") is False
    assert [device["fingerprint"] for device in devices.load_devices()] == ["fp-2"]


def test_remove_devices_by_tokens_removes_matching_subset(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _register("fp-1", "a" * 64)
    _register("fp-2", "b" * 64)
    _register("fp-3", "c" * 64)

    removed = devices.remove_devices_by_tokens({"a" * 64, "c" * 64})

    assert removed == 2
    assert [device["fingerprint"] for device in devices.load_devices()] == ["fp-2"]


def test_remove_devices_by_tokens_unknown_token_noops(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _register("fp-1", "a" * 64)
    before = devices.load_devices()

    removed = devices.remove_devices_by_tokens({"z" * 64})

    assert removed == 0
    assert devices.load_devices() == before


def test_remove_devices_by_tokens_empty_set_noops(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    removed = devices.remove_devices_by_tokens(set())

    assert removed == 0
    assert not _devices_path(tmp_path).exists()


def test_load_devices_returns_empty_for_legacy_token_keyed_store(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = _devices_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "token": "a" * 64,
                        "bundle_id": "org.solpbc.solstone-swift",
                        "environment": "development",
                        "platform": "ios",
                        "registered_at": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert devices.load_devices() == []
    assert "push device store unreadable" in caplog.text


def test_load_devices_returns_empty_for_malformed_store(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = _devices_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"devices": "bad"}), encoding="utf-8")

    assert devices.load_devices() == []
    assert "push device store unreadable" in caplog.text


def test_status_view_masks_token_and_formats_timestamp():
    assert devices.status_view(
        {
            "fingerprint": "fp-1",
            "token": "a" * 60 + "bcde",
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "development",
            "platform": "ios",
            "registered_at": 1713528000,
        }
    ) == {
        "token_suffix": "...bcde",
        "bundle_id": "org.solpbc.solstone-swift",
        "environment": "development",
        "platform": "ios",
        "registered_at": "2024-04-19T12:00:00Z",
    }

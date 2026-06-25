# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import os
import time

import pytest

detect_created_mod = importlib.import_module("solstone.think.detect_created")


@pytest.fixture
def tz_los_angeles(monkeypatch):
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    time.tzset()


@pytest.mark.parametrize(
    "filename",
    [
        "2024-01-15_10_30_00_copy.m4a",
        "2024-01-15 10:30:00.wav",
        "2024-01-15_10-30-00.mp3",
        "20240115_103000_2.m4a",
        "20240115103000.mov",
    ],
)
def test_filename_local_formats_and_suffixes(monkeypatch, filename):
    monkeypatch.setattr(detect_created_mod, "_extract_metadata_json", lambda path: {})

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename=filename,
    )

    assert result == {
        "day": "20240115",
        "time": "103000",
        "confidence": "high",
        "source": detect_created_mod.DETERMINISTIC_SOURCE_FILENAME,
        "utc": False,
    }


@pytest.mark.parametrize(
    "filename",
    [
        "2024-01-15.m4a",
        "01-15.m4a",
        "2024-13-15_10_30_00.m4a",
        "2024-02-30_10_30_00.m4a",
        "2024-01-15_25_30_00.m4a",
    ],
)
def test_filename_non_matches_and_invalid_values(monkeypatch, filename):
    monkeypatch.setattr(detect_created_mod, "_extract_metadata_json", lambda path: {})

    assert (
        detect_created_mod.resolve_created_deterministic(
            "/tmp/source",
            original_filename=filename,
        )
        is None
    )


def test_limitless_filename_utc_converts_to_local(monkeypatch, tz_los_angeles):
    def fail_metadata(path):
        raise AssertionError("metadata should not be read for limitless filenames")

    monkeypatch.setattr(detect_created_mod, "_extract_metadata_json", fail_metadata)

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename=(
            "limitless_pendant_2024-01-15T18-30-00_to_2024-01-15T18-45-00.m4a"
        ),
    )

    assert result == {
        "day": "20240115",
        "time": "103000",
        "confidence": "high",
        "source": detect_created_mod.DETERMINISTIC_SOURCE_FILENAME_UTC,
        "utc": True,
    }


def test_limitless_rule_is_source_scoped(monkeypatch):
    monkeypatch.setattr(detect_created_mod, "_extract_metadata_json", lambda path: {})

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename="other_2024-01-15T18-30-00_to_2024-01-15T18-45-00.m4a",
    )

    assert result is None


def test_metadata_local_unambiguous(monkeypatch):
    monkeypatch.setattr(
        detect_created_mod,
        "_extract_metadata_json",
        lambda path: {
            "CreateDate": "2024:01:15 10:30:00",
            "DateTimeOriginal": "2024:01:15 10:30:00",
        },
    )

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename="voice.m4a",
    )

    assert result == {
        "day": "20240115",
        "time": "103000",
        "confidence": "high",
        "source": detect_created_mod.DETERMINISTIC_SOURCE_METADATA_LOCAL,
        "utc": False,
    }


def test_metadata_utc_offset_unambiguous(monkeypatch, tz_los_angeles):
    monkeypatch.setattr(
        detect_created_mod,
        "_extract_metadata_json",
        lambda path: {"CreationDate": "2024:01:15 18:30:00+00:00"},
    )

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename="voice.m4a",
    )

    assert result == {
        "day": "20240115",
        "time": "103000",
        "confidence": "high",
        "source": detect_created_mod.DETERMINISTIC_SOURCE_METADATA_UTC,
        "utc": True,
    }


def test_metadata_ambiguous_voice_memo_falls_through(monkeypatch, tz_los_angeles):
    monkeypatch.setattr(
        detect_created_mod,
        "_extract_metadata_json",
        lambda path: {
            "CreationDate": "2024:01:15 10:30:00-08:00",
            "CreateDate": "2024:01:15 18:30:00",
        },
    )

    assert (
        detect_created_mod.resolve_created_deterministic(
            "/tmp/source",
            original_filename="voice.m4a",
        )
        is None
    )


def test_filename_metadata_conflict_falls_through(monkeypatch):
    monkeypatch.setattr(
        detect_created_mod,
        "_extract_metadata_json",
        lambda path: {"CreateDate": "2024:01:15 10:30:01"},
    )

    assert (
        detect_created_mod.resolve_created_deterministic(
            "/tmp/source",
            original_filename="2024-01-15_10_30_00.m4a",
        )
        is None
    )


def test_filename_metadata_agree_keeps_filename_source(monkeypatch):
    monkeypatch.setattr(
        detect_created_mod,
        "_extract_metadata_json",
        lambda path: {"CreateDate": "2024:01:15 10:30:00"},
    )

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename="2024-01-15_10_30_00.m4a",
    )

    assert result["source"] == detect_created_mod.DETERMINISTIC_SOURCE_FILENAME
    assert result["day"] == "20240115"
    assert result["time"] == "103000"


def test_metadata_failure_does_not_block_filename(monkeypatch):
    def fail_metadata(path):
        raise AssertionError("metadata failed")

    monkeypatch.setattr(detect_created_mod, "_extract_metadata_json", fail_metadata)

    result = detect_created_mod.resolve_created_deterministic(
        "/tmp/source",
        original_filename="2024-01-15_10_30_00.m4a",
    )

    assert result["source"] == detect_created_mod.DETERMINISTIC_SOURCE_FILENAME
    assert result["day"] == "20240115"
    assert result["time"] == "103000"


def test_extract_metadata_json_returns_empty_on_failure(monkeypatch):
    def fail_run(*args, **kwargs):
        raise FileNotFoundError("exiftool")

    monkeypatch.setattr(detect_created_mod.subprocess, "run", fail_run)

    assert detect_created_mod._extract_metadata_json("/tmp/source") == {}

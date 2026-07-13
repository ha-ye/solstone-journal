# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from solstone.think.importers.health_dedupe import (
    DEDUPE_DB_RELATIVE_PATH,
    HealthDedupeRecord,
    ensure_health_dedupe_db,
    get_health_dedupe_record,
    health_dedupe_db_path,
    upsert_health_dedupe_record,
    upsert_health_dedupe_records,
)
from solstone.think.importers.health_schema import (
    HealthRecordIdentity,
    health_record_dedupe_key,
    health_value_hash,
)


@contextmanager
def _temporary_umask(mask: int):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_dedupe_db_lives_under_imports(tmp_path: Path):
    db_path = ensure_health_dedupe_db(tmp_path)

    assert db_path == tmp_path / DEDUPE_DB_RELATIVE_PATH
    assert db_path == health_dedupe_db_path(tmp_path)
    assert db_path.exists()
    assert db_path.parent == tmp_path / "imports"
    assert not (tmp_path / "entities").exists()
    assert not (tmp_path / "facets").exists()
    assert not (tmp_path / "observations").exists()


def test_read_missing_dedupe_db_does_not_create_imports(tmp_path: Path):
    assert get_health_dedupe_record(tmp_path, "sha256:missing") is None
    assert not (tmp_path / "imports").exists()


def test_health_record_key_uses_source_id_when_available():
    first = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family="apple_health",
            record_type="HKQuantityTypeIdentifierHeartRate",
            source_record_id="abc123",
            start_time="2026-01-02T12:00:00-07:00",
            value=62,
        )
    )
    second = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family="apple_health",
            record_type="HKQuantityTypeIdentifierHeartRate",
            source_record_id="abc123",
            start_time="2026-01-02T12:05:00-07:00",
            value=99,
        )
    )

    assert first == second
    assert first.startswith("sha256:")


def test_health_record_key_keeps_source_families_separate():
    apple_key = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family="apple_health",
            record_type="heart_rate",
            start_time="2026-01-02T12:00:00-07:00",
            value=62,
            unit="count/min",
        )
    )
    oura_key = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family="oura",
            record_type="heart_rate",
            start_time="2026-01-02T12:00:00-07:00",
            value=62,
            unit="count/min",
        )
    )

    assert apple_key != oura_key


def test_upsert_health_dedupe_record_preserves_first_import(tmp_path: Path):
    value_hash = health_value_hash(value=105, unit="mg/dL")
    dedupe_key = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family="apple_health",
            record_type="HKQuantityTypeIdentifierBloodGlucose",
            start_time="2026-01-02T12:30:00-07:00",
            value=105,
            unit="mg/dL",
        )
    )

    inserted = upsert_health_dedupe_record(
        tmp_path,
        HealthDedupeRecord(
            dedupe_key=dedupe_key,
            source_family="apple_health",
            record_type="HKQuantityTypeIdentifierBloodGlucose",
            start_time="2026-01-02T12:30:00-07:00",
            value_hash=value_hash,
            first_import_id="20260102_123000",
            last_seen_import_id="20260102_123000",
            raw_ref="raw/export.xml#record-4",
        ),
    )
    updated = upsert_health_dedupe_record(
        tmp_path,
        HealthDedupeRecord(
            dedupe_key=dedupe_key,
            source_family="apple_health",
            record_type="HKQuantityTypeIdentifierBloodGlucose",
            start_time="2026-01-02T12:30:00-07:00",
            value_hash=value_hash,
            first_import_id="20260102_999999",
            last_seen_import_id="20260102_999999",
            normalized_ref="import.apple_health/20260102/123000",
        ),
    )
    row = get_health_dedupe_record(tmp_path, dedupe_key)

    assert inserted is True
    assert updated is False
    assert row is not None
    assert row["first_import_id"] == "20260102_123000"
    assert row["last_seen_import_id"] == "20260102_999999"
    assert row["raw_ref"] == "raw/export.xml#record-4"
    assert row["normalized_ref"] == "import.apple_health/20260102/123000"


def test_upsert_health_dedupe_records_batches_in_wal_mode(tmp_path: Path):
    original_key = "sha256:existing-glucose"
    inserted = upsert_health_dedupe_record(
        tmp_path,
        HealthDedupeRecord(
            dedupe_key=original_key,
            source_family="apple_health",
            record_type="HKQuantityTypeIdentifierBloodGlucose",
            start_time="2026-01-02T12:30:00-07:00",
            first_import_id="20260102_123000",
            last_seen_import_id="20260102_123000",
            raw_ref="raw/export.xml#record-4",
        ),
    )

    result = upsert_health_dedupe_records(
        tmp_path,
        [
            HealthDedupeRecord(
                dedupe_key=original_key,
                source_family="apple_health",
                record_type="HKQuantityTypeIdentifierBloodGlucose",
                start_time="2026-01-02T12:30:00-07:00",
                first_import_id="20260102_999999",
                last_seen_import_id="20260102_999999",
                normalized_ref="import.apple_health/20260102/123000",
            ),
            HealthDedupeRecord(
                dedupe_key="sha256:new-heart-rate",
                source_family="apple_health",
                source_record_id="heart-rate-1",
                record_type="HKQuantityTypeIdentifierHeartRate",
                start_time="2026-01-02T12:35:00-07:00",
                first_import_id="20260102_999999",
                last_seen_import_id="20260102_999999",
                normalized_ref="import.apple_health/20260102/123500",
            ),
        ],
    )

    existing_row = get_health_dedupe_record(tmp_path, original_key)
    new_row = get_health_dedupe_record(tmp_path, "sha256:new-heart-rate")

    assert inserted is True
    assert result.inserted == 1
    assert result.updated == 1
    assert existing_row is not None
    assert existing_row["first_import_id"] == "20260102_123000"
    assert existing_row["last_seen_import_id"] == "20260102_999999"
    assert existing_row["raw_ref"] == "raw/export.xml#record-4"
    assert existing_row["normalized_ref"] == "import.apple_health/20260102/123000"
    assert new_row is not None
    assert new_row["source_record_id"] == "heart-rate-1"
    with sqlite3.connect(health_dedupe_db_path(tmp_path)) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode == "wal"


def test_dedupe_db_and_wal_sidecars_are_private_while_connection_is_live(
    tmp_path: Path,
):
    with _temporary_umask(0o000):
        upsert_health_dedupe_records(tmp_path, _synthetic_dedupe_records(1))

    db_path = health_dedupe_db_path(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT OR REPLACE INTO health_dedupe (
                dedupe_key,
                source_family,
                record_type,
                start_time,
                first_import_id,
                last_seen_import_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sha256:sidecar-live-check",
                "apple_health",
                "HKQuantityTypeIdentifierStepCount",
                "2026-01-02T12:00:00-07:00",
                "synthetic-import",
                "synthetic-import",
                "2026-01-02T19:00:00Z",
                "2026-01-02T19:00:00Z",
            ),
        )
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            assert path.exists()
            assert _mode(path) == 0o600


def test_existing_broad_dedupe_db_is_repaired_on_open(tmp_path: Path):
    db_path = health_dedupe_db_path(tmp_path)
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    db_path.chmod(0o644)

    ensure_health_dedupe_db(tmp_path)

    assert _mode(db_path) == 0o600
    assert _mode(db_path.parent) == 0o700


def test_upsert_health_dedupe_records_handles_duplicate_keys_in_batch(tmp_path: Path):
    result = upsert_health_dedupe_records(
        tmp_path,
        [
            HealthDedupeRecord(
                dedupe_key="sha256:duplicate-key",
                source_family="apple_health",
                record_type="HKQuantityTypeIdentifierStepCount",
                start_time="2026-01-02T12:00:00-07:00",
                first_import_id="20260102_120000",
                last_seen_import_id="20260102_120000",
                raw_ref="raw/export.xml#record-1",
            ),
            HealthDedupeRecord(
                dedupe_key="sha256:duplicate-key",
                source_family="apple_health",
                record_type="HKQuantityTypeIdentifierStepCount",
                start_time="2026-01-02T12:00:00-07:00",
                first_import_id="20260102_999999",
                last_seen_import_id="20260102_999999",
                normalized_ref="import.apple_health/20260102/120000",
            ),
        ],
    )
    row = get_health_dedupe_record(tmp_path, "sha256:duplicate-key")

    assert result.inserted == 1
    assert result.updated == 1
    assert row is not None
    assert row["first_import_id"] == "20260102_120000"
    assert row["last_seen_import_id"] == "20260102_999999"
    assert row["raw_ref"] == "raw/export.xml#record-1"
    assert row["normalized_ref"] == "import.apple_health/20260102/120000"


def _synthetic_dedupe_records(count: int) -> list[HealthDedupeRecord]:
    return [
        HealthDedupeRecord(
            dedupe_key=f"sha256:synthetic-{index:05d}",
            source_family="apple_health",
            source_record_id=f"synthetic-{index:05d}",
            record_type="HKQuantityTypeIdentifierStepCount",
            start_time=f"2026-01-02T12:{index % 60:02d}:00-07:00",
            end_time=f"2026-01-02T12:{index % 60:02d}:30-07:00",
            value_hash=f"sha256:value-{index:05d}",
            first_import_id="synthetic-import",
            last_seen_import_id="synthetic-import",
            normalized_ref=f"import.apple_health/synthetic/{index:05d}",
            raw_ref=f"raw/export.xml#synthetic-{index:05d}",
        )
        for index in range(count)
    ]


def measure_batched_health_dedupe_upsert_rate(
    tmp_path: Path,
    count: int = 12_000,
) -> float:
    records = _synthetic_dedupe_records(count)
    started_at = time.perf_counter()
    result = upsert_health_dedupe_records(tmp_path, records)
    elapsed_seconds = time.perf_counter() - started_at

    assert result.inserted == count
    assert result.updated == 0
    return count / elapsed_seconds


@pytest.mark.performance
def test_batched_health_dedupe_upsert_benchmark(tmp_path: Path):
    upserts_per_second = measure_batched_health_dedupe_upsert_rate(tmp_path)

    # The original 10,000/sec floor was intended to be loose, but still
    # flaked on loaded hosts. This machine measured ~228,000/sec for the
    # 12k-record run; keep the floor far below the real rate so it only
    # trips on catastrophic batching loss, such as commit-per-record
    # behavior.
    assert upserts_per_second >= 2_000

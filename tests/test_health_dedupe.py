# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

from solstone.think.importers.health_dedupe import (
    DEDUPE_DB_RELATIVE_PATH,
    HealthDedupeRecord,
    ensure_health_dedupe_db,
    get_health_dedupe_record,
    health_dedupe_db_path,
    upsert_health_dedupe_record,
)
from solstone.think.importers.health_schema import (
    HealthRecordIdentity,
    health_record_dedupe_key,
    health_value_hash,
)


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

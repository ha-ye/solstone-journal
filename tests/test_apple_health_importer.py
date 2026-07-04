# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import json
import logging
from pathlib import Path

import pytest

from solstone.think.importers import apple_health
from solstone.think.importers.apple_health import (
    AppleHealthImporter,
)
from solstone.think.importers.health_dedupe import get_health_dedupe_record

FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic"
)
ZIP_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic.zip"
)
DTD_FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic_dtd"
)


def test_apple_health_not_registered_for_save_mode():
    file_importer = importlib.import_module("solstone.think.importers.file_importer")

    assert "apple_health" not in file_importer.FILE_IMPORTER_REGISTRY
    assert file_importer.get_file_importer("apple_health") is None


def test_detects_synthetic_export_directory():
    importer = AppleHealthImporter()

    assert importer.detect(FIXTURE_ROOT) is True
    assert importer.detect(FIXTURE_ROOT / "apple_health_export") is True
    assert importer.detect(Path(__file__)) is False


def test_preview_synthetic_export_directory():
    preview = AppleHealthImporter().preview(FIXTURE_ROOT)

    assert preview.date_range == ("20260101", "20260102")
    assert preview.item_count == 7
    assert preview.entity_count == 0
    assert "records=5" in preview.summary
    assert "workouts=1" in preview.summary
    assert "routes=1" in preview.summary
    assert "glucose=1" in preview.summary


def test_preview_filters_synthetic_export_by_inclusive_date_window():
    preview = AppleHealthImporter().preview(
        FIXTURE_ROOT,
        date_from="2026-01-02",
        date_to="2026-01-02",
    )

    assert preview.date_range == ("20260102", "20260102")
    assert preview.item_count == 4
    assert "records=3" in preview.summary
    assert "workouts=1" in preview.summary
    assert "routes=0" in preview.summary
    assert "glucose=1" in preview.summary


def test_preview_parses_synthetic_export_with_internal_dtd_subset():
    preview = AppleHealthImporter().preview(DTD_FIXTURE_ROOT)

    assert preview.date_range == ("20260410", "20260411")
    assert preview.item_count == 3
    assert "records=2" in preview.summary
    assert "workouts=1" in preview.summary
    assert "export_cda=present" in preview.summary
    assert "electrocardiograms=2" in preview.summary


def test_preview_reports_cda_and_ecg_files_by_name_only():
    preview = AppleHealthImporter().preview(DTD_FIXTURE_ROOT)

    assert "export_cda=present" in preview.summary
    assert "electrocardiograms=2" in preview.summary


def test_dry_run_process_returns_preview_without_files(tmp_path: Path):
    result = AppleHealthImporter().process(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260102_123000",
        dry_run=True,
    )

    assert result.entries_written == 0
    assert result.entities_seeded == 0
    assert result.files_created == []
    assert result.date_range == ("20260101", "20260102")
    assert "Dry run only" in result.summary
    assert not (tmp_path / "imports").exists()


def test_save_mode_writes_raw_source_normalized_rows_and_dedupe_to_journal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    live_journal = tmp_path / "live-journal"
    journal = tmp_path / "synthetic-journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(live_journal))

    result = AppleHealthImporter().process(
        FIXTURE_ROOT,
        journal,
        import_id="20260103_120000",
        dry_run=False,
        date_from="20260102",
        date_to="20260102",
    )

    raw_export = (
        journal
        / "imports"
        / "20260103_120000"
        / "raw"
        / "apple_health_export"
        / "export.xml"
    )
    normalized = (
        journal / "imports" / "20260103_120000" / "normalized" / "2026-01.jsonl"
    )
    rows = _read_jsonl(normalized)
    glucose_row = next(
        row
        for row in rows
        if row["record_type"] == "HKQuantityTypeIdentifierBloodGlucose"
    )
    dedupe_row = get_health_dedupe_record(journal, glucose_row["dedupe_key"])

    assert result.entries_written == 4
    assert result.entities_seeded == 0
    assert result.files_created == []
    assert result.segments is None
    assert result.date_range == ("20260102", "20260102")
    assert raw_export.read_text(encoding="utf-8").startswith("<?xml")
    assert {row["day"] for row in rows} == {"20260102"}
    assert {row["kind"] for row in rows} == {"record", "workout"}
    assert all(row["import_id"] == "20260103_120000" for row in rows)
    assert dedupe_row is not None
    assert dedupe_row["last_seen_import_id"] == "20260103_120000"
    assert dedupe_row["normalized_ref"] == glucose_row["normalized_ref"]
    assert dedupe_row["raw_ref"] == glucose_row["raw_ref"]
    assert not live_journal.exists()


def test_save_mode_writes_opt_in_day_summary_files_only_in_files_created(
    tmp_path: Path,
):
    result = AppleHealthImporter().process(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260103_120000",
        dry_run=False,
        date_from="2026-01-02",
        date_to="2026-01-02",
        with_day_summaries=True,
    )

    assert len(result.files_created) == 1
    summary_path = Path(result.files_created[0])
    assert summary_path == (
        tmp_path
        / "chronicle"
        / "20260102"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    assert result.segments == [("20260102", "000000_300")]

    summary = summary_path.read_text(encoding="utf-8")
    assert "HKQuantityTypeIdentifierBloodGlucose: 1" in summary
    assert "HKCategoryTypeIdentifierSleepAnalysis: 1" in summary
    assert (
        "Sleep window: 2026-01-02T22:30:00-07:00 to 2026-01-03T06:30:00-07:00"
    ) in summary
    assert "Workout names: Running" in summary
    assert "Glucose: count 1, min 105, max 105, mean 105" in summary
    assert (
        "Sources present: Synthetic Ring Mirror, Synthetic Stelo, Synthetic Watch"
        in summary
    )


def test_detects_and_previews_synthetic_zip_fixture():
    importer = AppleHealthImporter()

    assert ZIP_FIXTURE.exists()
    assert importer.detect(ZIP_FIXTURE) is True
    assert (
        importer.preview(ZIP_FIXTURE).summary == importer.preview(FIXTURE_ROOT).summary
    )


def test_preview_logs_byte_progress_for_large_xml_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    export_root = tmp_path / "apple_health_export"
    export_root.mkdir()
    records = "\n".join(
        '<Record type="HKQuantityTypeIdentifierStepCount" '
        'sourceName="Synthetic Watch" startDate="2026-05-01 08:00:00 -0700" '
        'endDate="2026-05-01 08:05:00 -0700" unit="count" value="1"/>'
        for _ in range(3)
    )
    (export_root / "export.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<!DOCTYPE HealthData>\n"
        f'<HealthData locale="en_US">{records}</HealthData>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(apple_health, "_BYTE_PROGRESS_LOG_INTERVAL", 128)
    caplog.set_level(logging.INFO, logger=apple_health.__name__)

    preview = AppleHealthImporter().preview(tmp_path)

    assert preview.item_count == 3
    assert any(
        "from Apple Health export.xml" in record.message for record in caplog.records
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

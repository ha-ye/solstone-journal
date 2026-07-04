# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
from pathlib import Path

import pytest

from solstone.think.importers.apple_health import (
    AppleHealthImporter,
)

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


def test_process_save_mode_is_blocked_before_writing(tmp_path: Path):
    importer = AppleHealthImporter()

    with pytest.raises(RuntimeError, match="privacy preflight"):
        importer.process(
            FIXTURE_ROOT,
            tmp_path,
            import_id="20260102_123000",
            dry_run=False,
        )

    assert not (tmp_path / "imports").exists()


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


def test_detects_and_previews_synthetic_zip_fixture():
    importer = AppleHealthImporter()

    assert ZIP_FIXTURE.exists()
    assert importer.detect(ZIP_FIXTURE) is True
    assert (
        importer.preview(ZIP_FIXTURE).summary == importer.preview(FIXTURE_ROOT).summary
    )

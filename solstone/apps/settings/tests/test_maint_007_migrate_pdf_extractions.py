# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import json
from pathlib import Path

import pytest

_mod = importlib.import_module(
    "solstone.apps.settings.maint.007_migrate_pdf_extractions"
)
migrate_pdf_extractions = _mod.migrate_pdf_extractions


@pytest.fixture(autouse=True)
def _set_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _segment(
    journal: Path,
    *,
    day: str = "20250101",
    stream: str = "import.document",
    segment: str = "120000_0",
) -> Path:
    path = journal / "chronicle" / day / stream / segment
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_jsonl(path: Path, header: dict, *entries: dict) -> None:
    lines = [json.dumps(header), *(json.dumps(entry) for entry in entries)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_migration_deletes_duplicate_document_and_image_jsonl(tmp_path):
    segment = _segment(tmp_path)
    (segment / "document_transcript.md").write_text(
        "already migrated", encoding="utf-8"
    )
    document_jsonl = segment / "document.jsonl"
    image_jsonl = segment / "image.jsonl"
    _write_jsonl(
        document_jsonl,
        {"raw": "original.pdf", "kind": "document"},
        {"start": "00:00:00", "text": "duplicate document"},
    )
    _write_jsonl(
        image_jsonl,
        {"raw": "image.png", "kind": "image"},
        {"start": "00:00:00", "text": "duplicate image"},
    )

    counts = migrate_pdf_extractions(tmp_path)

    assert counts == {
        "scanned": 2,
        "deleted_duplicates": 2,
        "converted_documents": 0,
        "skipped_unparseable": 0,
        "skipped_no_text": 0,
    }
    assert not document_jsonl.exists()
    assert not image_jsonl.exists()


def test_migration_converts_orphan_document_jsonl_in_any_stream(tmp_path):
    segment = _segment(tmp_path, stream="manual.docs")
    jsonl_path = segment / "document.jsonl"
    _write_jsonl(
        jsonl_path,
        {
            "raw": "original.pdf",
            "kind": "document",
            "page_count": 3,
            "extraction_method": "pypdf",
        },
        {"start": "00:00:00", "text": "First extracted paragraph."},
        {"start": "00:00:01", "text": "Second extracted paragraph."},
    )

    counts = migrate_pdf_extractions(tmp_path)

    md_path = segment / "document_transcript.md"
    md_text = md_path.read_text(encoding="utf-8")
    assert counts == {
        "scanned": 1,
        "deleted_duplicates": 0,
        "converted_documents": 1,
        "skipped_unparseable": 0,
        "skipped_no_text": 0,
    }
    assert not jsonl_path.exists()
    assert "# original" in md_text
    assert "**Type:** Document" in md_text
    assert "**Pages:** 3" in md_text
    assert "**Extraction method:** pypdf" in md_text
    assert "First extracted paragraph." in md_text
    assert "Second extracted paragraph." in md_text

    assert migrate_pdf_extractions(tmp_path) == {
        "scanned": 0,
        "deleted_duplicates": 0,
        "converted_documents": 0,
        "skipped_unparseable": 0,
        "skipped_no_text": 0,
    }


def test_migration_leaves_unconvertible_jsonl_in_place_and_reports(tmp_path):
    segment = _segment(tmp_path)
    malformed = segment / "malformed.jsonl"
    malformed.write_text("{not json\n", encoding="utf-8")
    (segment / "malformed.pdf").write_bytes(b"%PDF-1.4 synthetic")
    no_text = segment / "no_text.jsonl"
    _write_jsonl(
        no_text,
        {"raw": "empty.pdf", "kind": "document"},
        {"start": "00:00:00", "text": ""},
    )
    unrelated_screen = segment / "screen.jsonl"
    unrelated_screen.write_text('{}\n{"text": "screen output"}\n', encoding="utf-8")
    unrelated_empty = segment / "empty.jsonl"
    unrelated_empty.write_text("", encoding="utf-8")
    reported: list[str] = []

    counts = migrate_pdf_extractions(tmp_path, reported=reported)

    assert counts == {
        "scanned": 2,
        "deleted_duplicates": 0,
        "converted_documents": 0,
        "skipped_unparseable": 1,
        "skipped_no_text": 1,
    }
    assert malformed.exists()
    assert no_text.exists()
    assert unrelated_screen.exists()
    assert unrelated_empty.exists()
    assert reported == [
        "chronicle/20250101/import.document/120000_0/malformed.jsonl",
        "chronicle/20250101/import.document/120000_0/no_text.jsonl",
    ]

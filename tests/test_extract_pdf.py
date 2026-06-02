# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json

import pytest

from solstone.observe import extract_pdf


def _segment_pdf(tmp_path):
    segment_dir = tmp_path / "chronicle" / "20240101" / "default" / "123456_300"
    segment_dir.mkdir(parents=True)
    pdf_path = segment_dir / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")
    return pdf_path


def _meta(**overrides):
    meta = {
        "page_count": 2,
        "is_scanned": False,
        "extraction_method": "pypdf",
        "vision_error": None,
    }
    meta.update(overrides)
    return meta


def test_run_writes_document_jsonl_with_header_metadata(tmp_path, monkeypatch):
    pdf_path = _segment_pdf(tmp_path)
    monkeypatch.setenv("OBSERVER_NAME", "desk")
    monkeypatch.setenv(
        "SEGMENT_META", json.dumps({"stream": "default", "facet": "work"})
    )
    monkeypatch.setattr(
        extract_pdf,
        "extract_pdf_text",
        lambda path: ("Document text", _meta(page_count=4, extraction_method="vision")),
    )

    output_path = extract_pdf.run(pdf_path)

    assert output_path == pdf_path.with_suffix(".jsonl")
    lines = output_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    entry = json.loads(lines[1])
    assert header["raw"] == "report.pdf"
    assert header["kind"] == "document"
    assert header["observer"] == "desk"
    assert header["stream"] == "default"
    assert header["facet"] == "work"
    assert header["page_count"] == 4
    assert header["extraction_method"] == "vision"
    assert entry == {"start": "00:00:00", "text": "Document text"}


def test_run_skips_existing_output_unless_redo(tmp_path, monkeypatch):
    pdf_path = _segment_pdf(tmp_path)
    output_path = pdf_path.with_suffix(".jsonl")
    output_path.write_text("existing\n", encoding="utf-8")
    calls = []

    def fake_extract(path):
        calls.append(path)
        return "Replacement text", _meta()

    monkeypatch.setattr(extract_pdf, "extract_pdf_text", fake_extract)

    assert extract_pdf.run(pdf_path) is None
    assert output_path.read_text(encoding="utf-8") == "existing\n"
    assert calls == []

    assert extract_pdf.run(pdf_path, redo=True) == output_path
    assert calls == [pdf_path]
    assert "Replacement text" in output_path.read_text(encoding="utf-8")


def test_run_extract_failure_leaves_no_output(tmp_path, monkeypatch):
    pdf_path = _segment_pdf(tmp_path)

    def fail_extract(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract_pdf, "extract_pdf_text", fail_extract)

    with pytest.raises(RuntimeError, match="boom"):
        extract_pdf.run(pdf_path)

    assert not pdf_path.with_suffix(".jsonl").exists()


def test_run_vision_error_leaves_no_output(tmp_path, monkeypatch):
    pdf_path = _segment_pdf(tmp_path)
    monkeypatch.setattr(
        extract_pdf,
        "extract_pdf_text",
        lambda path: ("x", _meta(is_scanned=True, vision_error="vision failed")),
    )

    with pytest.raises(RuntimeError, match="scanned PDF, vision failed"):
        extract_pdf.run(pdf_path)

    assert not pdf_path.with_suffix(".jsonl").exists()

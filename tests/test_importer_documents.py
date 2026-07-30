# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from solstone.observe.pdf_worker import (
    PdfWorkerEncryptedError,
    PdfWorkerRenderIOError,
    PdfWorkerSuccess,
)
from solstone.think.importers.file_importer import FILE_IMPORTER_REGISTRY
from solstone.think.models import NoBrainConfiguredError
from solstone.think.responsiveness import NON_RESPONSIVE_OUTPUT_MESSAGE
from tests.pdf_worker_fixtures import (
    IMAGE_TEXT_SENTINEL,
    MIXED_TEXT_SENTINEL,
    TEXT_RICH_SENTINEL,
    write_encrypted_fixture_pair,
    write_image_only_fixture,
    write_importer_mixed_fixture,
    write_pdf,
    write_text_rich_fixture,
    write_text_with_image_rich_fixture,
)


def _mod():
    return importlib.import_module("solstone.think.importers.documents")


def _set_mtime(path: Path, when: dt.datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _fixed_mtime(path: Path) -> None:
    _set_mtime(path, dt.datetime(2026, 3, 4, 12, 0, 0).astimezone())


def _install_generate(monkeypatch, mod, outcomes):
    calls: list[dict] = []
    iterator = iter(outcomes)

    def fake_generate(*, contents, context, **kwargs):
        del kwargs
        calls.append({"contents": contents, "context": context})
        outcome = next(iterator)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(mod, "generate", fake_generate)
    return calls


def _import_pdf(mod, pdf: Path, journal: Path, **kwargs):
    return mod.importer.process(
        pdf,
        journal,
        import_id=kwargs.pop("import_id", "import-test"),
        **kwargs,
    )


def _segment_dir(journal: Path, result) -> Path:
    day, seg_key = result.segments[0]
    return journal / "chronicle" / day / "import.document" / seg_key


def _transcript(journal: Path, result) -> str:
    return (_segment_dir(journal, result) / "document_transcript.md").read_text(
        encoding="utf-8"
    )


def _assert_cited_rasters_exist(segment_dir: Path) -> None:
    transcript = (segment_dir / "document_transcript.md").read_text(encoding="utf-8")
    for line in transcript.splitlines():
        marker = "pages/page-"
        if marker not in line:
            continue
        rel = line[line.index(marker) :].split("]", 1)[0]
        assert (segment_dir / rel).is_file()


def _assert_no_bare_single_h1(transcript: str) -> None:
    for index, line in enumerate(transcript.splitlines()):
        if line.startswith("# "):
            assert index == 0


def _snapshot_tree(path: Path) -> dict[str, bytes]:
    return {
        str(child.relative_to(path)): child.read_bytes()
        for child in sorted(path.rglob("*"))
        if child.is_file()
    }


def _payload(pdf: Path, *, pages: list[dict], warnings=(), metadata=None) -> dict:
    return {
        "schema": "sol-pdf/1",
        "engine": "pdfium fixture / pypdfium2 fixture",
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "page_count": len(pages),
        "encrypted": False,
        "warnings": list(warnings),
        "render": None,
        "metadata": metadata
        or {
            "title": None,
            "author": None,
            "creation_date": None,
            "mod_date": None,
            "producer": None,
        },
        "pages": pages,
    }


def test_detect_pdf_file(tmp_path):
    mod = _mod()
    pdf = tmp_path / "file.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert mod.importer.detect(pdf) is True


def test_detect_non_pdf(tmp_path):
    mod = _mod()
    txt = tmp_path / "file.txt"
    txt.write_text("hello", encoding="utf-8")
    assert mod.importer.detect(txt) is False


def test_detect_directory_with_pdfs(tmp_path):
    mod = _mod()
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    assert mod.importer.detect(tmp_path) is True


def test_detect_empty_directory(tmp_path):
    mod = _mod()
    assert mod.importer.detect(tmp_path) is False


def test_pure_text_layer_pdf_imports_verbatim_with_zero_model_calls(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_text_rich_fixture(tmp_path / "contract.pdf")
    _fixed_mtime(pdf)
    calls = _install_generate(
        monkeypatch, mod, [AssertionError("unexpected model call")]
    )

    result = _import_pdf(mod, pdf, tmp_path)

    segment_dir = _segment_dir(tmp_path, result)
    transcript = _transcript(tmp_path, result)
    assert result.errors == []
    assert result.hard_failures == ()
    assert result.entries_written == 1
    assert result.entities_seeded == 0
    assert result.files_created == [str(segment_dir / "document_transcript.md")]
    assert (segment_dir / "original.pdf").read_bytes() == pdf.read_bytes()
    assert calls == []
    assert "## Page 1" in transcript
    assert "## Page 2" in transcript
    assert TEXT_RICH_SENTINEL in transcript
    assert mod.MARKER_MODEL_EXTRACTED.split("{NNNN}", 1)[0] not in transcript
    assert (
        "**Extraction:" in transcript
        and "2 text-layer, 0 model-extracted, 0 unavailable of 2 pages; "
        "0 image-described; 0 model calls"
        in transcript
    )


def test_scanned_pdf_uses_reading_prompt_once_per_page_and_installs_rasters(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_image_only_fixture(tmp_path / "scan.pdf")
    _fixed_mtime(pdf)
    calls = _install_generate(
        monkeypatch,
        mod,
        ["# Extracted page 1\n\nBody 1", "Extracted page 2"],
    )

    result = _import_pdf(mod, pdf, tmp_path)

    segment_dir = _segment_dir(tmp_path, result)
    transcript = _transcript(tmp_path, result)
    assert [call["context"] for call in calls] == [
        "import.document.vision",
        "import.document.vision",
    ]
    assert all("# [Document Title or Type]" in call["contents"][0] for call in calls)
    assert mod._marker(mod.MARKER_MODEL_EXTRACTED, index=1) in transcript
    assert mod._marker(mod.MARKER_MODEL_EXTRACTED, index=2) in transcript
    assert "> # Extracted page 1" in transcript
    assert ">\n> Body 1" in transcript
    assert (segment_dir / "pages" / "page-0001.png").is_file()
    assert (segment_dir / "pages" / "page-0002.png").is_file()
    assert (
        "0 text-layer, 2 model-extracted, 0 unavailable of 2 pages; "
        "0 image-described; 2 model calls" in transcript
    )
    _assert_no_bare_single_h1(transcript)
    _assert_cited_rasters_exist(segment_dir)


def test_mixed_pdf_keeps_text_extracts_scanned_and_describes_text_image_page(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_importer_mixed_fixture(tmp_path / "mixed.pdf")
    _fixed_mtime(pdf)
    calls = _install_generate(
        monkeypatch,
        mod,
        ["Scanned page text", "Description of embedded chart"],
    )

    result = _import_pdf(mod, pdf, tmp_path)

    segment_dir = _segment_dir(tmp_path, result)
    transcript = _transcript(tmp_path, result)
    assert [call["context"] for call in calls] == [
        "import.document.vision",
        "import.document.describe",
    ]
    assert MIXED_TEXT_SENTINEL in transcript
    assert IMAGE_TEXT_SENTINEL in transcript
    assert mod._marker(mod.MARKER_MODEL_EXTRACTED, index=2) in transcript
    assert mod._marker(mod.MARKER_IMAGE_DESCRIPTION, index=3) in transcript
    assert "> Scanned page text" in transcript
    assert "> Description of embedded chart" in transcript
    assert (
        "2 text-layer, 1 model-extracted, 0 unavailable of 3 pages; "
        "1 image-described; 2 model calls" in transcript
    )
    _assert_cited_rasters_exist(segment_dir)


def test_text_page_with_large_image_gets_one_description_only(tmp_path, monkeypatch):
    mod = _mod()
    pdf = write_text_with_image_rich_fixture(tmp_path / "image-text.pdf")
    _fixed_mtime(pdf)
    calls = _install_generate(monkeypatch, mod, ["Image description"])

    result = _import_pdf(mod, pdf, tmp_path)

    transcript = _transcript(tmp_path, result)
    assert [call["context"] for call in calls] == ["import.document.describe"]
    assert mod._marker(mod.MARKER_IMAGE_DESCRIPTION, index=1) in transcript
    assert "page-0002.png" not in transcript
    assert "2 text-layer, 0 model-extracted, 0 unavailable of 2 pages" in transcript
    assert "1 image-described; 1 model calls" in transcript


def test_marker_bytes_are_exact():
    mod = _mod()

    assert mod.MARKER_MODEL_EXTRACTED == (
        "> [model-extracted from page image — may contain errors; "
        "original: pages/page-{NNNN}.png]"
    )
    assert mod.MARKER_IMAGE_DESCRIPTION == (
        "> [image description — model-generated; original: pages/page-{NNNN}.png]"
    )
    assert mod.MARKER_PAGE_TEXT_UNAVAILABLE_WITH_RASTER == (
        "> [page text unavailable — {reason}; "
        "page image preserved at pages/page-{NNNN}.png]"
    )
    assert mod.MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_WITH_RASTER == (
        "> [image description unavailable — {reason}; "
        "page image preserved at pages/page-{NNNN}.png]"
    )
    assert mod.MARKER_PAGE_TEXT_UNAVAILABLE_NO_RASTER == (
        "> [page text unavailable — {reason}; no page image could be produced]"
    )
    assert mod.MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_NO_RASTER == (
        "> [image description unavailable — {reason}; no page image could be produced]"
    )
    assert mod._marker(mod.MARKER_MODEL_EXTRACTED, index=2) == (
        "> [model-extracted from page image — may contain errors; "
        "original: pages/page-0002.png]"
    )
    assert mod._marker(mod.MARKER_IMAGE_DESCRIPTION, index=12) == (
        "> [image description — model-generated; original: pages/page-0012.png]"
    )
    assert (
        mod._marker(
            mod.MARKER_PAGE_TEXT_UNAVAILABLE_WITH_RASTER,
            index=3,
            reason="boom",
        )
        == "> [page text unavailable — boom; page image preserved at pages/page-0003.png]"
    )
    assert mod._marker(
        mod.MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_WITH_RASTER,
        index=4,
        reason="boom",
    ) == (
        "> [image description unavailable — boom; "
        "page image preserved at pages/page-0004.png]"
    )
    assert (
        mod._marker(
            mod.MARKER_PAGE_TEXT_UNAVAILABLE_NO_RASTER,
            reason="boom",
        )
        == "> [page text unavailable — boom; no page image could be produced]"
    )
    assert (
        mod._marker(
            mod.MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_NO_RASTER,
            reason="boom",
        )
        == "> [image description unavailable — boom; no page image could be produced]"
    )


def test_vision_extraction_failure_marks_one_scanned_page_unavailable(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_image_only_fixture(tmp_path / "scan.pdf")
    _fixed_mtime(pdf)
    calls = _install_generate(
        monkeypatch,
        mod,
        [RuntimeError("vision failed"), "Second page model text"],
    )

    result = _import_pdf(mod, pdf, tmp_path)

    segment_dir = _segment_dir(tmp_path, result)
    transcript = _transcript(tmp_path, result)
    assert len(calls) == 2
    assert result.hard_failures == ()
    assert (
        mod._marker(
            mod.MARKER_PAGE_TEXT_UNAVAILABLE_WITH_RASTER,
            index=1,
            reason="vision failed",
        )
        in transcript
    )
    assert mod._marker(mod.MARKER_MODEL_EXTRACTED, index=2) in transcript
    assert (segment_dir / "pages" / "page-0001.png").is_file()
    assert (segment_dir / "pages" / "page-0002.png").is_file()


def test_non_responsive_document_description_uses_unavailable_marker_without_raw_refusal(
    tmp_path, monkeypatch
):
    import solstone.think.providers as providers_package
    from solstone.think import models

    mod = _mod()
    pdf = write_image_only_fixture(tmp_path / "scan.pdf")
    _fixed_mtime(pdf)
    provider_module = SimpleNamespace(
        run_generate=MagicMock(
            return_value={
                "text": "I cannot describe this screen.",
                "model": "provider-model",
                "finish_reason": "stop",
            }
        )
    )
    monkeypatch.setattr(
        models,
        "resolve_provider",
        lambda _interface: ("fake", "provider-model"),
    )
    monkeypatch.setattr(
        providers_package,
        "get_provider_module",
        lambda _provider: provider_module,
    )

    result = _import_pdf(mod, pdf, tmp_path)

    transcript = _transcript(tmp_path, result)
    assert "I cannot describe this screen." not in transcript
    assert NON_RESPONSIVE_OUTPUT_MESSAGE in transcript
    assert "2 model-extracted, 0 unavailable" not in transcript
    assert "0 model-extracted, 2 unavailable" in transcript


def test_description_failure_keeps_full_text_and_uses_description_marker(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_text_with_image_rich_fixture(tmp_path / "image-text.pdf")
    _fixed_mtime(pdf)
    _install_generate(monkeypatch, mod, [NoBrainConfiguredError()])

    result = _import_pdf(mod, pdf, tmp_path)

    transcript = _transcript(tmp_path, result)
    assert IMAGE_TEXT_SENTINEL in transcript
    assert (
        mod._marker(
            mod.MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_WITH_RASTER,
            index=1,
            reason=mod.REASON_NO_BRAIN_CONFIGURED,
        )
        in transcript
    )
    assert "page text unavailable" not in transcript
    assert result.hard_failures == ()


def test_model_call_cap_marks_overflow_pages_unavailable(tmp_path, monkeypatch):
    mod = _mod()
    pdf = write_image_only_fixture(tmp_path / "scan.pdf")
    _fixed_mtime(pdf)
    monkeypatch.setattr(mod, "MODEL_CALLS_MAX_PER_DOCUMENT", 1)
    calls = _install_generate(monkeypatch, mod, ["Only first page"])

    result = _import_pdf(mod, pdf, tmp_path)

    transcript = _transcript(tmp_path, result)
    assert len(calls) == 1
    assert mod._marker(mod.MARKER_MODEL_EXTRACTED, index=1) in transcript
    assert (
        mod._marker(
            mod.MARKER_PAGE_TEXT_UNAVAILABLE_WITH_RASTER,
            index=2,
            reason=mod.REASON_MODEL_CALL_LIMIT,
        )
        in transcript
    )
    assert "1 model calls" in transcript


def test_encrypted_fixture_is_hard_failure_without_segment(tmp_path):
    mod = _mod()
    clear = write_text_rich_fixture(tmp_path / "clear.pdf")
    user_pdf = tmp_path / "encrypted.pdf"
    owner_pdf = tmp_path / "owner.pdf"
    write_encrypted_fixture_pair(clear, user_pdf, owner_pdf)

    result = _import_pdf(mod, user_pdf, tmp_path)

    assert result.entries_written == 0
    assert result.hard_failures == ("encrypted.pdf: password-protected PDF",)
    assert result.errors == ["encrypted.pdf: password-protected PDF"]
    assert not (tmp_path / "chronicle").exists()


def test_corrupt_pdf_batched_with_good_pdf_imports_good_and_returns_hard_failure(
    tmp_path, monkeypatch
):
    mod = _mod()
    good = write_text_rich_fixture(tmp_path / "good.pdf")
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\nnot a valid xref\n%%EOF\n")
    _fixed_mtime(good)
    calls = _install_generate(
        monkeypatch, mod, [AssertionError("unexpected model call")]
    )

    result = _import_pdf(mod, tmp_path, tmp_path)

    assert calls == []
    assert result.entries_written == 1
    assert result.segments
    assert "good" in _transcript(tmp_path, result)
    assert len(result.hard_failures) == 1
    assert "corrupt.pdf" in result.hard_failures[0]
    assert "corrupt" in result.hard_failures[0]


def test_render_io_failure_is_hard_failure_before_segment_creation(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    render_root = tmp_path / "render-root"
    payload = _payload(
        pdf,
        pages=[
            {
                "index": 1,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "chars": 0,
                "image_area_fraction": 1.0,
                "rendered": None,
                "error": None,
                "text": "",
            }
        ],
    )

    class FakeTemporaryDirectory:
        def __enter__(self):
            render_root.mkdir()
            return str(render_root)

        def __exit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            shutil.rmtree(render_root, ignore_errors=True)

    def fake_worker(command, pdf_path, **kwargs):
        del command, pdf_path
        if kwargs.get("render_pages"):
            raise PdfWorkerRenderIOError(
                "synthetic render I/O failure",
                returncode=5,
                stdout="",
                stderr="",
                payload={
                    "schema": "sol-pdf/1",
                    "error": "render-io",
                    "detail": "synthetic render I/O failure",
                },
            )
        return PdfWorkerSuccess(payload=payload, warnings=(), stderr="")

    monkeypatch.setattr(mod.tempfile, "TemporaryDirectory", FakeTemporaryDirectory)
    monkeypatch.setattr(mod, "run_pdf_worker", fake_worker)

    result = _import_pdf(mod, pdf, tmp_path)

    assert result.entries_written == 0
    assert len(result.hard_failures) == 1
    assert "scan.pdf" in result.hard_failures[0]
    assert "I/O" in result.hard_failures[0]
    assert not (tmp_path / "chronicle").exists()
    assert not render_root.exists()


def test_worker_warnings_land_in_header_and_import_result(tmp_path, monkeypatch):
    mod = _mod()
    pdf = tmp_path / "warning.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    warning = (
        "page 1: page render failed: synthetic render failure after text extraction"
    )
    first = _payload(
        pdf,
        pages=[
            {
                "index": 1,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "chars": 80,
                "image_area_fraction": 0.25,
                "rendered": None,
                "error": None,
                "text": "Healthy text layer survives even when render later fails.",
            }
        ],
    )
    second = _payload(
        pdf,
        pages=[
            {
                **first["pages"][0],
                "chars": 0,
                "image_area_fraction": 0.0,
                "error": warning,
                "text": "",
            }
        ],
        warnings=[warning],
    )
    calls = []

    def fake_worker(command, pdf_path, **kwargs):
        calls.append((command, Path(pdf_path).name, kwargs))
        return PdfWorkerSuccess(
            payload=second if kwargs.get("render_pages") else first,
            warnings=tuple(second["warnings"] if kwargs.get("render_pages") else ()),
            stderr="",
        )

    monkeypatch.setattr(mod, "run_pdf_worker", fake_worker)

    result = _import_pdf(mod, pdf, tmp_path)

    transcript = _transcript(tmp_path, result)
    assert len(calls) == 2
    assert "**Worker warnings:**" in transcript
    assert f"- {warning}" in transcript
    assert result.errors == [f"warning.pdf: {warning}"]
    assert (
        mod._marker(
            mod.MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_NO_RASTER,
            reason=warning,
        )
        in transcript
    )


def test_timestamp_metadata_offset_and_year_2999_fallback(tmp_path, monkeypatch):
    mod = _mod()
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    offset_pdf = write_pdf(
        tmp_path / "offset.pdf",
        [
            {
                "text": (
                    "Offset metadata document has enough text to avoid model "
                    "calls while proving the local day conversion."
                )
            }
        ],
        {"ModDate": "D:20260304003000+02'00'"},
    )
    future_pdf = write_pdf(
        tmp_path / "future.pdf",
        [
            {
                "text": (
                    "Future metadata document falls back to the ordinary file "
                    "mtime because year 2999 is outside the sanity window."
                )
            }
        ],
        {"ModDate": "D:29990101000000+00'00'"},
    )
    _set_mtime(future_pdf, dt.datetime(2026, 5, 6, 12, 0, 0).astimezone())

    try:
        offset_result = _import_pdf(mod, offset_pdf, tmp_path)
        future_result = _import_pdf(mod, future_pdf, tmp_path)

        assert offset_result.segments[0][0] == "20260303"
        assert "**Date:** 2026-03-03 (pdf-metadata)" in _transcript(
            tmp_path, offset_result
        )
        assert future_result.segments[0][0] == "20260506"
        assert "**Date:** 2026-05-06 (file-mtime)" in _transcript(
            tmp_path, future_result
        )
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_segment_identity_force_controls_same_content_regeneration(
    tmp_path, monkeypatch
):
    mod = _mod()
    first = write_importer_mixed_fixture(tmp_path / "first.pdf")
    second = write_pdf(
        tmp_path / "second.pdf",
        [
            {
                "text": (
                    "Second different document deliberately shares the exact "
                    "same metadata timestamp as the first document."
                )
            }
        ],
    )
    _fixed_mtime(first)
    _fixed_mtime(second)

    _install_generate(monkeypatch, mod, ["Initial scanned text", "Initial description"])
    first_result = _import_pdf(mod, first, tmp_path)
    first_segment = _segment_dir(tmp_path, first_result)
    initial_snapshot = _snapshot_tree(first_segment)
    manifest_path = tmp_path / "imports" / "import-test" / "content_manifest.jsonl"
    initial_manifest = manifest_path.read_bytes()

    def fail_generate(**_kwargs):
        raise AssertionError("same-content skip must not call generate")

    monkeypatch.setattr(mod, "generate", fail_generate)
    real_worker = mod.run_pdf_worker
    worker_calls = []

    def spy_worker(command, pdf_path, **kwargs):
        worker_calls.append((command, Path(pdf_path).name, dict(kwargs)))
        return real_worker(command, pdf_path, **kwargs)

    monkeypatch.setattr(mod, "run_pdf_worker", spy_worker)
    skipped_result = _import_pdf(mod, first, tmp_path)

    assert worker_calls == [("extract", "first.pdf", {})]
    assert skipped_result.entries_written == 0
    assert skipped_result.segments == []
    assert skipped_result.hard_failures == ()
    assert skipped_result.errors == [
        "first.pdf: skipped (already imported; use --force to regenerate)"
    ]
    assert _snapshot_tree(first_segment) == initial_snapshot
    assert manifest_path.read_bytes() == initial_manifest

    monkeypatch.setattr(mod, "run_pdf_worker", real_worker)
    _install_generate(monkeypatch, mod, ["Forced scanned text", "Forced description"])
    same_result = _import_pdf(mod, first, tmp_path, force=True)

    assert same_result.segments == first_result.segments
    assert same_result.entries_written == 1
    assert "Forced scanned text" in _transcript(tmp_path, same_result)
    assert "Forced description" in _transcript(tmp_path, same_result)
    assert (first_segment / "original.pdf").read_bytes() == first.read_bytes()
    _assert_cited_rasters_exist(first_segment)

    forced_snapshot = _snapshot_tree(first_segment)
    second_result = _import_pdf(mod, second, tmp_path, force=True)

    assert second_result.segments != first_result.segments
    assert _snapshot_tree(first_segment) == forced_snapshot
    assert (_segment_dir(tmp_path, second_result) / "original.pdf").read_bytes() == (
        second.read_bytes()
    )


def test_write_order_original_exists_without_transcript_when_transcript_write_fails(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_text_rich_fixture(tmp_path / "contract.pdf")
    _fixed_mtime(pdf)

    def fail_write_text(*_args, **_kwargs):
        raise RuntimeError("stop after original")

    monkeypatch.setattr(mod, "write_text", fail_write_text)
    result = _import_pdf(mod, pdf, tmp_path)

    assert result.segments == []
    [segment_dir] = list((tmp_path / "chronicle").glob("*/import.document/*"))
    assert (segment_dir / "original.pdf").is_file()
    assert not (segment_dir / "document_transcript.md").exists()


def test_write_order_rasters_exist_without_transcript_when_final_write_fails(
    tmp_path, monkeypatch
):
    mod = _mod()
    pdf = write_image_only_fixture(tmp_path / "scan.pdf")
    _fixed_mtime(pdf)
    _install_generate(monkeypatch, mod, ["Page one", "Page two"])

    def fail_write_text(*_args, **_kwargs):
        raise RuntimeError("stop after rasters")

    monkeypatch.setattr(mod, "write_text", fail_write_text)
    result = _import_pdf(mod, pdf, tmp_path)

    assert result.segments == []
    [segment_dir] = list((tmp_path / "chronicle").glob("*/import.document/*"))
    assert (segment_dir / "original.pdf").is_file()
    assert (segment_dir / "pages" / "page-0001.png").is_file()
    assert (segment_dir / "pages" / "page-0002.png").is_file()
    assert not (segment_dir / "document_transcript.md").exists()


def test_preview_uses_inspect_only_and_aggregates_worker_failures(
    tmp_path, monkeypatch
):
    mod = _mod()
    readable = tmp_path / "readable.pdf"
    encrypted = tmp_path / "encrypted.pdf"
    readable.write_bytes(b"%PDF-1.4 readable")
    encrypted.write_bytes(b"%PDF-1.4 encrypted")
    payload = _payload(
        readable,
        pages=[
            {
                "index": 1,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "chars": 0,
                "image_area_fraction": 0.0,
                "rendered": None,
                "error": None,
            }
        ],
        metadata={"mod_date": "2026-03-04T12:00:00+00:00"},
    )
    calls = []

    def fake_worker(command, pdf_path, **kwargs):
        calls.append((command, Path(pdf_path).name, kwargs))
        if Path(pdf_path).name == "encrypted.pdf":
            raise PdfWorkerEncryptedError(
                "encrypted",
                returncode=3,
                stdout="",
                stderr="",
                payload={"schema": "sol-pdf/1", "error": "encrypted"},
            )
        return PdfWorkerSuccess(payload=payload, warnings=(), stderr="")

    monkeypatch.setattr(mod, "run_pdf_worker", fake_worker)

    preview = mod.importer.preview(tmp_path)

    assert [call[0] for call in calls] == ["inspect", "inspect"]
    assert all("render_pages" not in call[2] for call in calls)
    assert preview.item_count == 2
    assert preview.entity_count == 0
    assert preview.summary.startswith("2 PDF documents, 1 total pages")
    assert "encrypted.pdf: password-protected PDF" in preview.summary
    assert not (tmp_path / "chronicle").exists()
    assert not (tmp_path / "imports").exists()


def test_document_importer_no_longer_seeds_entities():
    mod = _mod()
    assert not hasattr(mod, "_extract_entities")
    assert not hasattr(mod, "seed_entities")


def test_registry_entry():
    assert FILE_IMPORTER_REGISTRY["document"] == "solstone.think.importers.documents"


def test_manifest_meta_is_new_shape(tmp_path):
    mod = _mod()
    pdf = write_text_rich_fixture(tmp_path / "contract.pdf")
    _fixed_mtime(pdf)

    _import_pdf(mod, pdf, tmp_path)

    manifest = tmp_path / "imports" / "import-test" / "content_manifest.jsonl"
    entry = json.loads(manifest.read_text(encoding="utf-8"))
    assert entry["meta"] == {
        "page_count": 2,
        "engine": entry["meta"]["engine"],
        "timestamp_source": "file-mtime",
        "text_layer_pages": 2,
        "model_extracted_pages": 0,
        "unavailable_pages": 0,
        "image_described_pages": 0,
        "model_calls": 0,
        "warnings": [],
    }
    assert "extraction_method" not in entry["meta"]

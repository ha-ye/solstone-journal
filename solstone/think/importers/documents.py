# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""PDF document importer backed by the isolated PDFium worker."""

from __future__ import annotations

import datetime as dt
import functools
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from solstone.observe import pdf_worker
from solstone.observe.pdf_worker import (
    PdfWorkerCorruptError,
    PdfWorkerEncryptedError,
    PdfWorkerEngineError,
    PdfWorkerError,
    PdfWorkerRenderIOError,
    PdfWorkerTimeoutError,
    run_pdf_worker,
)
from solstone.think.features import require_extra
from solstone.think.importers.file_importer import ImportPreview, ImportResult
from solstone.think.importers.shared import (
    PRIVATE_IMPORT_FILE_MODE,
    hash_source,
    install_source_file,
    write_content_manifest,
)
from solstone.think.journal_io import write_text
from solstone.think.models import NoBrainConfiguredError, generate
from solstone.think.prompts import load_prompt

logger = logging.getLogger(__name__)

PAGE_TEXT_MIN_CHARS = 50
PAGE_IMAGE_DESCRIBE_MIN = 0.10
MODEL_CALLS_MAX_PER_DOCUMENT = 50

REASON_MODEL_CALL_LIMIT = "model-call limit reached"
REASON_EMPTY_MODEL_RESPONSE = "empty model response"
REASON_NO_BRAIN_CONFIGURED = "no brain configured"

MARKER_MODEL_EXTRACTED = (
    "> [model-extracted from page image — may contain errors; "
    "original: pages/page-{NNNN}.png]"
)
MARKER_IMAGE_DESCRIPTION = (
    "> [image description — model-generated; original: pages/page-{NNNN}.png]"
)
MARKER_PAGE_TEXT_UNAVAILABLE_WITH_RASTER = (
    "> [page text unavailable — {reason}; "
    "page image preserved at pages/page-{NNNN}.png]"
)
MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_WITH_RASTER = (
    "> [image description unavailable — {reason}; "
    "page image preserved at pages/page-{NNNN}.png]"
)
MARKER_PAGE_TEXT_UNAVAILABLE_NO_RASTER = (
    "> [page text unavailable — {reason}; no page image could be produced]"
)
MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_NO_RASTER = (
    "> [image description unavailable — {reason}; no page image could be produced]"
)

_DOCUMENT_STREAM = "import.document"
_TRANSCRIPT_FILENAME = "document_transcript.md"
_ORIGINAL_FILENAME = "original.pdf"
_DESCRIBE_PROMPT = (
    "Describe this image in detail. Include any visible text, people, objects, "
    "setting, and notable context. Return a concise natural-language description."
)


@dataclass(frozen=True)
class _TimestampChoice:
    timestamp: float
    source: str


@dataclass(frozen=True)
class _PreparedDocument:
    payload: dict[str, Any]
    transcript: str
    rasters: dict[int, Path]
    warnings: tuple[str, ...]
    timestamp_source: str
    text_layer_pages: int
    model_extracted_pages: int
    unavailable_pages: int
    image_described_pages: int
    model_calls: int


@dataclass(frozen=True)
class _WorkerOutputs:
    payload: dict[str, Any]
    rasters: dict[int, Path]
    render_errors: dict[int, str]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _SegmentClaim:
    day: str
    seg_key: str
    timestamp: float
    already_imported: bool = False


@dataclass
class _RenderStats:
    text_layer_pages: int = 0
    model_extracted_pages: int = 0
    unavailable_pages: int = 0
    image_described_pages: int = 0
    model_calls: int = 0


@dataclass(frozen=True)
class _ModelOutcome:
    text: str | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.text is not None


def _find_pdfs(path: Path) -> list[Path]:
    """Return matching PDF files for a file or directory path."""

    if path.is_file() and path.suffix.lower() == ".pdf":
        return [path]
    if path.is_dir():
        return sorted(
            child
            for child in path.iterdir()
            if child.is_file() and child.suffix.lower() == ".pdf"
        )
    return []


def _now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _collapse_line(value: Any) -> str:
    return " ".join(str(value).split())


def _page_name(index: int) -> str:
    return f"page-{index:04d}.png"


def _marker(
    template: str, *, index: int | None = None, reason: str | None = None
) -> str:
    rendered = template
    if index is not None:
        rendered = rendered.replace("{NNNN}", f"{index:04d}")
    if reason is not None:
        rendered = rendered.replace("{reason}", _collapse_line(reason))
    return rendered


def _blockquote_lines(text: str) -> list[str]:
    return [">" if line == "" else f"> {line}" for line in text.splitlines()]


def _model_block(marker: str, text: str) -> str:
    return "\n".join([marker, *_blockquote_lines(text)])


@functools.lru_cache(maxsize=1)
def _reading_prompt() -> str:
    categories_dir = Path(pdf_worker.__file__).resolve().parent / "categories"
    return load_prompt("reading", base_dir=categories_dir).text


def _image_for_model(path: Path) -> Any:
    from PIL import Image

    from solstone.observe.utils import resize_for_vlm

    with Image.open(path) as img:
        img.load()
        return resize_for_vlm(img).copy()


def _generate_for_page(
    *,
    prompt: str,
    raster_path: Path,
    context: str,
    stats: _RenderStats,
) -> _ModelOutcome:
    if stats.model_calls >= MODEL_CALLS_MAX_PER_DOCUMENT:
        return _ModelOutcome(text=None, reason=REASON_MODEL_CALL_LIMIT)

    stats.model_calls += 1
    try:
        response = generate(
            contents=[prompt, _image_for_model(raster_path)],
            context=context,
        )
    except NoBrainConfiguredError:
        return _ModelOutcome(text=None, reason=REASON_NO_BRAIN_CONFIGURED)
    except Exception as exc:
        return _ModelOutcome(
            text=None, reason=_collapse_line(str(exc) or type(exc).__name__)
        )

    text = response.strip()
    if not text:
        return _ModelOutcome(text=None, reason=REASON_EMPTY_MODEL_RESPONSE)
    return _ModelOutcome(text=text, reason=None)


def _merge_warnings(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for warning in group:
            collapsed = _collapse_line(warning)
            if collapsed and collapsed not in seen:
                seen.add(collapsed)
                merged.append(collapsed)
    return tuple(merged)


def _render_set(payload: dict[str, Any]) -> set[int]:
    pages: set[int] = set()
    for page in payload.get("pages", []):
        if page.get("error") is not None:
            continue
        index = int(page["index"])
        chars = int(page.get("chars") or 0)
        image_area_fraction = float(page.get("image_area_fraction") or 0.0)
        if (
            chars < PAGE_TEXT_MIN_CHARS
            or image_area_fraction >= PAGE_IMAGE_DESCRIBE_MIN
        ):
            pages.add(index)
    return pages


def _rendered_pages(
    payload: dict[str, Any],
    render_dir: Path | None,
) -> tuple[dict[int, Path], dict[int, str]]:
    rasters: dict[int, Path] = {}
    render_errors: dict[int, str] = {}
    if render_dir is None:
        return rasters, render_errors

    for page in payload.get("pages", []):
        index = int(page["index"])
        error = page.get("error")
        if error is not None:
            render_errors[index] = _collapse_line(error)
            continue
        rendered = page.get("rendered")
        if not rendered:
            continue
        raster_path = render_dir / str(rendered)
        if raster_path.exists():
            rasters[index] = raster_path
        else:
            render_errors[index] = f"page {index}: rendered page image missing"
    return rasters, render_errors


def _parse_pdf_metadata_date(value: Any, *, now: dt.datetime) -> float | None:
    if not value:
        return None
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    local = parsed.astimezone()
    if _timestamp_in_window(local.timestamp(), now=now):
        return local.timestamp()
    return None


def _timestamp_in_window(timestamp: float, *, now: dt.datetime) -> bool:
    lower = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    upper = now.astimezone(dt.timezone.utc) + dt.timedelta(days=1)
    candidate = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    return lower <= candidate <= upper


def _choose_timestamp(payload: dict[str, Any], pdf_path: Path) -> _TimestampChoice:
    now = _now_local()
    metadata = payload.get("metadata") or {}
    for key in ("mod_date", "creation_date"):
        timestamp = _parse_pdf_metadata_date(metadata.get(key), now=now)
        if timestamp is not None:
            return _TimestampChoice(timestamp=timestamp, source="pdf-metadata")

    try:
        mtime = pdf_path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None and _timestamp_in_window(mtime, now=now):
        return _TimestampChoice(timestamp=mtime, source="file-mtime")

    return _TimestampChoice(timestamp=now.timestamp(), source="import-time")


def _segment_matches_sha(segment_dir: Path, sha256: str) -> bool:
    original_path = segment_dir / _ORIGINAL_FILENAME
    return original_path.is_file() and hash_source(original_path) == sha256


def _claim_segment(
    journal_root: Path,
    *,
    timestamp: float,
    sha256: str,
    used_keys: set[tuple[str, str]],
    force: bool,
) -> _SegmentClaim:
    ts = timestamp
    while True:
        local_dt = dt.datetime.fromtimestamp(ts).astimezone()
        day = local_dt.strftime("%Y%m%d")
        seg_key = f"{local_dt.strftime('%H%M%S')}_0"
        segment_dir = journal_root / "chronicle" / day / _DOCUMENT_STREAM / seg_key
        candidate = (day, seg_key)
        if candidate in used_keys:
            ts += 1
            continue
        if not segment_dir.exists():
            used_keys.add(candidate)
            return _SegmentClaim(day=day, seg_key=seg_key, timestamp=ts)
        if _segment_matches_sha(segment_dir, sha256):
            used_keys.add(candidate)
            return _SegmentClaim(
                day=day,
                seg_key=seg_key,
                timestamp=ts,
                already_imported=not force,
            )
        ts += 1


def _page_failure_reason(
    *,
    page: dict[str, Any],
    render_errors: dict[int, str],
) -> str:
    index = int(page["index"])
    return _collapse_line(
        page.get("error")
        or render_errors.get(index)
        or f"page {index}: page image missing"
    )


def _append_text_layer(
    section: list[str],
    text: str,
) -> None:
    section.append(text)
    if not text.endswith("\n"):
        section.append("\n")


def _render_page_section(
    page: dict[str, Any],
    *,
    rasters: dict[int, Path],
    render_errors: dict[int, str],
    stats: _RenderStats,
) -> str:
    index = int(page["index"])
    chars = int(page.get("chars") or 0)
    image_area_fraction = float(page.get("image_area_fraction") or 0.0)
    raster_path = rasters.get(index)
    page_error = page.get("error")
    section: list[str] = [f"## Page {index}\n\n"]

    if page_error is None and chars >= PAGE_TEXT_MIN_CHARS:
        stats.text_layer_pages += 1
        _append_text_layer(section, str(page.get("text") or ""))
        if image_area_fraction >= PAGE_IMAGE_DESCRIBE_MIN:
            if raster_path is not None:
                outcome = _generate_for_page(
                    prompt=_DESCRIBE_PROMPT,
                    raster_path=raster_path,
                    context="import.document.describe",
                    stats=stats,
                )
                if outcome.ok:
                    stats.image_described_pages += 1
                    section.append("\n")
                    section.append(
                        _model_block(
                            _marker(MARKER_IMAGE_DESCRIPTION, index=index),
                            outcome.text or "",
                        )
                    )
                    section.append("\n")
                else:
                    section.append("\n")
                    section.append(
                        _marker(
                            MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_WITH_RASTER,
                            index=index,
                            reason=outcome.reason or REASON_EMPTY_MODEL_RESPONSE,
                        )
                    )
                    section.append("\n")
            else:
                section.append("\n")
                section.append(
                    _marker(
                        MARKER_IMAGE_DESCRIPTION_UNAVAILABLE_NO_RASTER,
                        reason=_page_failure_reason(
                            page=page,
                            render_errors=render_errors,
                        ),
                    )
                )
                section.append("\n")
        return "".join(section).rstrip("\n")

    if page_error is None and chars < PAGE_TEXT_MIN_CHARS and raster_path is not None:
        outcome = _generate_for_page(
            prompt=_reading_prompt(),
            raster_path=raster_path,
            context="import.document.vision",
            stats=stats,
        )
        if outcome.ok:
            stats.model_extracted_pages += 1
            section.append(
                _model_block(
                    _marker(MARKER_MODEL_EXTRACTED, index=index),
                    outcome.text or "",
                )
            )
            section.append("\n")
        else:
            stats.unavailable_pages += 1
            section.append(
                _marker(
                    MARKER_PAGE_TEXT_UNAVAILABLE_WITH_RASTER,
                    index=index,
                    reason=outcome.reason or REASON_EMPTY_MODEL_RESPONSE,
                )
            )
            section.append("\n")
        return "".join(section).rstrip("\n")

    stats.unavailable_pages += 1
    section.append(
        _marker(
            MARKER_PAGE_TEXT_UNAVAILABLE_NO_RASTER,
            reason=_page_failure_reason(page=page, render_errors=render_errors),
        )
    )
    section.append("\n")
    return "".join(section).rstrip("\n")


def _render_header(
    *,
    title: str,
    payload: dict[str, Any],
    date: str,
    timestamp_source: str,
    stats: _RenderStats,
    warnings: tuple[str, ...],
) -> str:
    page_count = int(payload.get("page_count") or 0)
    lines = [
        f"# {title}",
        "",
        "**Type:** Document",
        f"**Pages:** {page_count}",
        f"**Date:** {date} ({timestamp_source})",
        (
            f"**Extraction:** {payload.get('engine', 'unknown')} — "
            f"{stats.text_layer_pages} text-layer, "
            f"{stats.model_extracted_pages} model-extracted, "
            f"{stats.unavailable_pages} unavailable of {page_count} pages; "
            f"{stats.image_described_pages} image-described; "
            f"{stats.model_calls} model calls"
        ),
    ]
    if warnings:
        lines.extend(["", "**Worker warnings:**"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", "---"])
    return "\n".join(lines)


def _render_transcript(
    *,
    title: str,
    payload: dict[str, Any],
    rasters: dict[int, Path],
    render_errors: dict[int, str],
    timestamp_choice: _TimestampChoice,
    segment_timestamp: float,
    warnings: tuple[str, ...],
) -> tuple[str, _RenderStats]:
    stats = _RenderStats()
    pages = sorted(payload.get("pages", []), key=lambda page: int(page["index"]))
    sections = [
        _render_page_section(
            page,
            rasters=rasters,
            render_errors=render_errors,
            stats=stats,
        )
        for page in pages
    ]
    header = _render_header(
        title=title,
        payload=payload,
        date=dt.datetime.fromtimestamp(segment_timestamp).strftime("%Y-%m-%d"),
        timestamp_source=timestamp_choice.source,
        stats=stats,
        warnings=warnings,
    )
    body = "\n\n".join(sections)
    return f"{header}\n\n{body}\n", stats


def _collect_worker_outputs(
    first: dict[str, Any], pdf_path: Path, *, render_dir: Path
) -> _WorkerOutputs:
    render_pages = _render_set(first)
    second: dict[str, Any] | None = None
    if render_pages:
        second = run_pdf_worker(
            "extract",
            pdf_path,
            render_pages=sorted(render_pages),
            render_dir=render_dir,
        ).payload

    warnings = _merge_warnings(
        first.get("warnings", []),
        second.get("warnings", []) if second is not None else [],
    )
    rasters, render_errors = _rendered_pages(second or first, render_dir)
    return _WorkerOutputs(
        payload=first,
        rasters=rasters,
        render_errors=render_errors,
        warnings=warnings,
    )


def _prepare_document(
    outputs: _WorkerOutputs,
    *,
    pdf_path: Path,
    timestamp_choice: _TimestampChoice,
    segment_timestamp: float,
) -> _PreparedDocument:
    transcript, stats = _render_transcript(
        title=pdf_path.stem,
        payload=outputs.payload,
        rasters=outputs.rasters,
        render_errors=outputs.render_errors,
        timestamp_choice=timestamp_choice,
        segment_timestamp=segment_timestamp,
        warnings=outputs.warnings,
    )
    return _PreparedDocument(
        payload=outputs.payload,
        transcript=transcript,
        rasters=outputs.rasters,
        warnings=outputs.warnings,
        timestamp_source=timestamp_choice.source,
        text_layer_pages=stats.text_layer_pages,
        model_extracted_pages=stats.model_extracted_pages,
        unavailable_pages=stats.unavailable_pages,
        image_described_pages=stats.image_described_pages,
        model_calls=stats.model_calls,
    )


def _install_artifacts(
    *,
    pdf_path: Path,
    segment_dir: Path,
    prepared: _PreparedDocument,
) -> Path:
    original_path = segment_dir / _ORIGINAL_FILENAME
    install_source_file(pdf_path, original_path)

    pages_dir = segment_dir / "pages"
    for index, raster_path in sorted(prepared.rasters.items()):
        install_source_file(raster_path, pages_dir / _page_name(index))

    transcript_path = segment_dir / _TRANSCRIPT_FILENAME
    write_text(transcript_path, prepared.transcript, mode=PRIVATE_IMPORT_FILE_MODE)
    return transcript_path


def _worker_error_message(pdf_path: Path, exc: PdfWorkerError) -> str:
    name = pdf_path.name
    detail = ""
    if exc.payload:
        detail = _collapse_line(exc.payload.get("detail") or "")
    if not detail:
        detail = _collapse_line(str(exc))

    if isinstance(exc, PdfWorkerEncryptedError):
        return f"{name}: password-protected PDF"
    if isinstance(exc, PdfWorkerCorruptError):
        return f"{name}: corrupt PDF ({detail})"
    if isinstance(exc, PdfWorkerRenderIOError):
        return f"{name}: PDF render I/O failed ({detail})"
    if isinstance(exc, PdfWorkerTimeoutError):
        return f"{name}: PDF worker timed out after {exc.timeout_seconds:g}s"
    if isinstance(exc, PdfWorkerEngineError):
        return f"{name}: PDF worker failed ({detail})"
    return f"{name}: PDF worker failed ({detail})"


def _manifest_meta(prepared: _PreparedDocument) -> dict[str, Any]:
    return {
        "page_count": prepared.payload.get("page_count"),
        "engine": prepared.payload.get("engine"),
        "timestamp_source": prepared.timestamp_source,
        "text_layer_pages": prepared.text_layer_pages,
        "model_extracted_pages": prepared.model_extracted_pages,
        "unavailable_pages": prepared.unavailable_pages,
        "image_described_pages": prepared.image_described_pages,
        "model_calls": prepared.model_calls,
        "warnings": list(prepared.warnings),
    }


class DocumentImporter:
    name = "document"
    display_name = "Documents"
    file_patterns = ["*.pdf"]
    description = "Import PDF documents with worker-backed text and raster extraction"

    def detect(self, path: Path) -> bool:
        return bool(_find_pdfs(path))

    def preview(self, path: Path) -> ImportPreview:
        require_extra("pdf-import")
        pdfs = _find_pdfs(path)
        if not pdfs:
            return ImportPreview(
                date_range=("", ""),
                item_count=0,
                entity_count=0,
                summary="No PDF documents found",
            )

        timestamps: list[float] = []
        total_pages = 0
        failures: list[str] = []
        for pdf_path in pdfs:
            try:
                payload = run_pdf_worker("inspect", pdf_path).payload
            except PdfWorkerError as exc:
                failures.append(_worker_error_message(pdf_path, exc))
                continue
            timestamps.append(_choose_timestamp(payload, pdf_path).timestamp)
            total_pages += int(payload.get("page_count") or 0)

        dates = sorted(
            dt.datetime.fromtimestamp(ts).strftime("%Y%m%d") for ts in timestamps
        )
        date_range = (dates[0], dates[-1]) if dates else ("", "")
        summary = f"{len(pdfs)} PDF documents, {total_pages} total pages"
        if failures:
            summary += f"; {len(failures)} unreadable ({'; '.join(failures)})"
        return ImportPreview(
            date_range=date_range,
            item_count=len(pdfs),
            entity_count=0,
            summary=summary,
        )

    def process(
        self,
        path: Path,
        journal_root: Path,
        *,
        facet: str | None = None,
        import_id: str | None = None,
        progress_callback: Callable | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> ImportResult:
        require_extra("pdf-import")
        pdfs = _find_pdfs(path)
        import_id = import_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        if not pdfs:
            return ImportResult(
                entries_written=0,
                entities_seeded=0,
                files_created=[],
                errors=[],
                summary="No PDF documents found to import",
            )

        journal_root = Path(journal_root)
        created_files: list[str] = []
        errors: list[str] = []
        hard_failures: list[str] = []
        segments: list[tuple[str, str]] = []
        manifest_entries: list[dict[str, Any]] = []
        timestamps: list[float] = []
        used_keys: set[tuple[str, str]] = set()

        for index, pdf_path in enumerate(pdfs):
            with tempfile.TemporaryDirectory() as render_root:
                try:
                    first = run_pdf_worker("extract", pdf_path).payload
                    timestamp_choice = _choose_timestamp(first, pdf_path)
                    claim = _claim_segment(
                        journal_root,
                        timestamp=timestamp_choice.timestamp,
                        sha256=str(first.get("sha256") or ""),
                        used_keys=used_keys,
                        force=force,
                    )
                    if claim.already_imported:
                        errors.append(
                            f"{pdf_path.name}: skipped (already imported; use --force to regenerate)"
                        )
                        continue

                    # Imported documents use at most two extract calls: pass 1 above
                    # is authoritative text/metadata, pass 2 here renders only the
                    # selected pages. Pure text and already-imported skips cost one.
                    outputs = _collect_worker_outputs(
                        first,
                        pdf_path=pdf_path,
                        render_dir=Path(render_root),
                    )
                    prepared = _prepare_document(
                        outputs,
                        pdf_path=pdf_path,
                        timestamp_choice=timestamp_choice,
                        segment_timestamp=claim.timestamp,
                    )
                    errors.extend(
                        f"{pdf_path.name}: {warning}" for warning in prepared.warnings
                    )
                    timestamps.append(claim.timestamp)

                    segment_dir = (
                        journal_root
                        / "chronicle"
                        / claim.day
                        / _DOCUMENT_STREAM
                        / claim.seg_key
                    )
                    md_path = segment_dir / _TRANSCRIPT_FILENAME
                    if not dry_run:
                        md_path = _install_artifacts(
                            pdf_path=pdf_path,
                            segment_dir=segment_dir,
                            prepared=prepared,
                        )
                        created_files.append(str(md_path))

                    segments.append((claim.day, claim.seg_key))
                    manifest_entries.append(
                        {
                            "id": f"document-{index}",
                            "title": pdf_path.stem,
                            "date": claim.day,
                            "type": "document",
                            "preview": prepared.transcript[:200],
                            "meta": _manifest_meta(prepared),
                            "segments": [{"day": claim.day, "key": claim.seg_key}],
                        }
                    )
                except PdfWorkerError as exc:
                    message = _worker_error_message(pdf_path, exc)
                    errors.append(message)
                    hard_failures.append(message)
                except Exception as exc:
                    message = f"{pdf_path.name}: document import failed ({_collapse_line(str(exc) or type(exc).__name__)})"
                    errors.append(message)

            if progress_callback:
                earliest = None
                latest = None
                if timestamps:
                    earliest = dt.datetime.fromtimestamp(min(timestamps)).strftime(
                        "%Y%m%d"
                    )
                    latest = dt.datetime.fromtimestamp(max(timestamps)).strftime(
                        "%Y%m%d"
                    )
                progress_callback(
                    index + 1,
                    len(pdfs),
                    earliest_date=earliest,
                    latest_date=latest,
                    entities_found=0,
                )

        if not dry_run and manifest_entries:
            write_content_manifest(
                import_id, manifest_entries, journal_root=journal_root
            )

        if timestamps:
            earliest = dt.datetime.fromtimestamp(min(timestamps)).strftime("%Y%m%d")
            latest = dt.datetime.fromtimestamp(max(timestamps)).strftime("%Y%m%d")
            date_range: tuple[str, str] | None = (earliest, latest)
        else:
            date_range = None

        return ImportResult(
            entries_written=len(segments),
            entities_seeded=0,
            files_created=created_files,
            errors=errors,
            hard_failures=tuple(hard_failures),
            summary=(
                f"Imported {len(segments)} PDF documents across "
                f"{len({day for day, _ in segments})} days into {len(segments)} segments"
            ),
            segments=segments,
            date_range=date_range,
        )


importer = DocumentImporter()

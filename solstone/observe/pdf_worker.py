# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Subprocess PDF extraction worker for the frozen ``sol-pdf/1`` contract.

The worker is deliberately journal-agnostic: one PDF in, one JSON document to
stdout, optional page PNGs to a caller-supplied directory. It uses PDFium via
pypdfium2 for metadata, text, page-object inspection, and rendering.

``image_area_fraction`` is a simple approximation: the sum of image page-object
bounding-box areas divided by page area, clamped to [0.0, 1.0]. Overlapping
images double-count by design; the worker avoids polygon-union complexity.

PDFium repairs some damaged files without surfacing a Python-level signal. In
particular, prep observed that removing only the trailing ``startxref`` block or
``%%EOF`` marker opens cleanly with the same page count, no warnings, and
``FPDF_GetLastError() == 0``. Deeper truncation, such as cutting the xref table
or file body, exits as corrupt.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

SCHEMA = "sol-pdf/1"

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_ENCRYPTED = 3
EXIT_CORRUPT = 4
EXIT_RENDER_IO = 5

ENV_RLIMIT_AS_MB = "SOLSTONE_PDF_WORKER_RLIMIT_AS_MB"
ENV_RLIMIT_CPU_SECONDS = "SOLSTONE_PDF_WORKER_RLIMIT_CPU_SECONDS"
DEFAULT_RLIMIT_AS_MB = 2048
DEFAULT_RLIMIT_CPU_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 90

logger = logging.getLogger(__name__)

_PDF_DATE_RE = re.compile(
    r"^D:"
    r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"(?:(?P<z>Z)|(?P<sign>[+-])(?P<tzhour>\d{2})'?(?P<tzminute>\d{2})'?)?$"
)


@dataclasses.dataclass(frozen=True)
class PdfWorkerSuccess:
    payload: dict[str, Any]
    warnings: tuple[str, ...]
    stderr: str


class PdfWorkerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None,
        stdout: str,
        stderr: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.payload = payload


class PdfWorkerEncryptedError(PdfWorkerError):
    pass


class PdfWorkerCorruptError(PdfWorkerError):
    pass


class PdfWorkerRenderIOError(PdfWorkerError):
    pass


class PdfWorkerEngineError(PdfWorkerError):
    pass


class PdfWorkerTimeoutError(PdfWorkerError):
    def __init__(self, message: str, *, timeout_seconds: float) -> None:
        super().__init__(message, returncode=None, stdout="", stderr="", payload=None)
        self.timeout_seconds = timeout_seconds


class _UsageError(RuntimeError):
    pass


class _ContractExit(RuntimeError):
    def __init__(self, exit_code: int, payload: dict[str, Any], message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


@dataclasses.dataclass(frozen=True)
class _RenderRequest:
    requested: bool
    render_dir: Path | None
    dpi: int
    below_chars: int | None
    above_image_fraction: float | None
    pages: frozenset[int]


def run_pdf_worker(
    command: str,
    pdf_path: str | Path,
    *,
    password: str | None = None,
    render_below_chars: int | None = None,
    render_above_image_fraction: float | None = None,
    render_pages: Sequence[int] | None = None,
    render_dir: str | Path | None = None,
    dpi: int = 150,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    python_executable: str | None = None,
) -> PdfWorkerSuccess:
    argv = [
        python_executable or sys.executable,
        "-m",
        "solstone.observe.pdf_worker",
        command,
        str(pdf_path),
    ]
    if password is not None:
        argv.extend(["--password", password])
    if command == "extract":
        if render_below_chars is not None:
            argv.extend(["--render-below-chars", str(render_below_chars)])
        if render_above_image_fraction is not None:
            argv.extend(
                ["--render-above-image-fraction", str(render_above_image_fraction)]
            )
        if render_pages is not None:
            argv.extend(
                ["--render-pages", ",".join(str(page) for page in render_pages)]
            )
        if render_dir is not None:
            argv.extend(["--render-dir", str(render_dir)])
        argv.extend(["--dpi", str(dpi)])

    child_env = os.environ.copy()
    if env:
        child_env.update(env)

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfWorkerTimeoutError(
            f"PDF worker timed out after {timeout_seconds:g}s",
            timeout_seconds=timeout_seconds,
        ) from exc

    payload = _parse_worker_stdout(completed.stdout)
    if completed.returncode == EXIT_OK and payload is not None:
        warnings = payload.get("warnings", [])
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            raise PdfWorkerEngineError(
                "PDF worker returned invalid warnings",
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                payload=payload,
            )
        return PdfWorkerSuccess(
            payload=payload,
            warnings=tuple(warnings),
            stderr=completed.stderr,
        )

    if completed.returncode == EXIT_ENCRYPTED and payload is not None:
        raise PdfWorkerEncryptedError(
            "PDF is encrypted or uses an unsupported security handler",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            payload=payload,
        )
    if completed.returncode == EXIT_CORRUPT and payload is not None:
        raise PdfWorkerCorruptError(
            "PDF is corrupt or not a PDF",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            payload=payload,
        )
    if completed.returncode == EXIT_RENDER_IO and payload is not None:
        raise PdfWorkerRenderIOError(
            "PDF render output failed",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            payload=payload,
        )

    raise PdfWorkerEngineError(
        f"PDF worker failed with return code {completed.returncode}",
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        payload=payload,
    )


def _parse_worker_stdout(stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Extract PDF metadata/text with PDFium")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_ArgumentParser,
    )

    inspect_parser = subparsers.add_parser("inspect", help="Inspect a PDF")
    inspect_parser.add_argument("pdf_path")
    inspect_parser.add_argument("--password")

    extract_parser = subparsers.add_parser("extract", help="Extract a PDF")
    extract_parser.add_argument("pdf_path")
    extract_parser.add_argument("--password")
    extract_parser.add_argument("--render-below-chars", type=int)
    extract_parser.add_argument("--render-above-image-fraction", type=float)
    extract_parser.add_argument("--render-pages")
    extract_parser.add_argument("--render-dir")
    extract_parser.add_argument("--dpi", type=int, default=150)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        pdf_path = Path(args.pdf_path)
        if not pdf_path.is_file():
            raise _UsageError(f"PDF not found: {pdf_path}")

        if args.command == "inspect":
            payload = _extract_document(
                pdf_path,
                password=args.password,
                include_text=False,
                render_request=_RenderRequest(
                    False, None, 150, None, None, frozenset()
                ),
            )
        else:
            render_request = _parse_render_request(args)
            payload = _extract_document(
                pdf_path,
                password=args.password,
                include_text=True,
                render_request=render_request,
            )
        _write_json_stdout(payload)
        return EXIT_OK
    except _UsageError as exc:
        logger.error("PDF worker usage error: %s", exc)
        _write_json_stdout({"schema": SCHEMA, "error": "usage", "detail": str(exc)})
        return EXIT_USAGE
    except _ContractExit as exc:
        logger.error("PDF worker failed: %s", exc)
        _write_json_stdout(exc.payload)
        return exc.exit_code
    except Exception as exc:
        logger.exception("PDF worker internal error")
        _write_json_stdout({"schema": SCHEMA, "error": "internal", "detail": str(exc)})
        return EXIT_INTERNAL


def _parse_render_request(args: argparse.Namespace) -> _RenderRequest:
    render_pages = _parse_render_pages(args.render_pages)
    requested = (
        args.render_below_chars is not None
        or args.render_above_image_fraction is not None
        or render_pages is not None
    )
    if args.dpi <= 0:
        raise _UsageError("--dpi must be positive")
    if args.render_below_chars is not None and args.render_below_chars < 0:
        raise _UsageError("--render-below-chars must be non-negative")
    if (
        args.render_above_image_fraction is not None
        and args.render_above_image_fraction < 0
    ):
        raise _UsageError("--render-above-image-fraction must be non-negative")
    if requested and not args.render_dir:
        raise _UsageError("--render-dir is required when render selectors are used")

    return _RenderRequest(
        requested=requested,
        render_dir=Path(args.render_dir).resolve() if args.render_dir else None,
        dpi=args.dpi,
        below_chars=args.render_below_chars,
        above_image_fraction=args.render_above_image_fraction,
        pages=frozenset(render_pages or ()),
    )


def _parse_render_pages(raw_pages: str | None) -> set[int] | None:
    if raw_pages is None:
        return None
    if not raw_pages.strip():
        raise _UsageError("--render-pages must contain at least one page")
    pages: set[int] = set()
    for raw_page in raw_pages.split(","):
        raw_page = raw_page.strip()
        if not raw_page:
            raise _UsageError("--render-pages contains an empty page number")
        try:
            page = int(raw_page)
        except ValueError as exc:
            raise _UsageError(f"invalid page number: {raw_page}") from exc
        if page < 1:
            raise _UsageError("--render-pages uses 1-based positive page numbers")
        pages.add(page)
    return pages


def _extract_document(
    pdf_path: Path,
    *,
    password: str | None,
    include_text: bool,
    render_request: _RenderRequest,
) -> dict[str, Any]:
    pdfium, raw, pdfium_version = _load_pdfium()
    doc = _open_document(pdfium, raw, pdf_path, password)
    page_count = len(doc)
    warnings: list[str] = []
    pages = [
        _extract_page(
            doc, raw, page_index, include_text=include_text, warnings=warnings
        )
        for page_index in range(page_count)
    ]

    render_payload = None
    if render_request.requested:
        if render_request.pages and max(render_request.pages) > page_count:
            raise _UsageError("--render-pages contains a page beyond the document")
        assert render_request.render_dir is not None
        render_payload = {
            "dpi": render_request.dpi,
            "dir": str(render_request.render_dir),
        }
        _render_selected_pages(pdfium, doc, raw, pages, render_request, warnings)

    return {
        "schema": SCHEMA,
        "engine": (
            f"pdfium {pdfium_version.PDFIUM_INFO} / "
            f"pypdfium2 {importlib.metadata.version('pypdfium2')}"
        ),
        "sha256": _sha256(pdf_path),
        "page_count": page_count,
        "encrypted": raw.FPDF_GetSecurityHandlerRevision(doc.raw) != -1,
        "warnings": warnings,
        "render": render_payload,
        "metadata": _metadata_payload(doc),
        "pages": pages,
    }


def _load_pdfium() -> tuple[Any, Any, Any]:
    import pypdfium2 as pdfium
    import pypdfium2.version as pdfium_version
    from pypdfium2 import raw

    return pdfium, raw, pdfium_version


def _open_document(pdfium: Any, raw: Any, pdf_path: Path, password: str | None) -> Any:
    try:
        return pdfium.PdfDocument(str(pdf_path), password=password)
    except Exception as exc:
        err_code = getattr(exc, "err_code", None)
        if err_code in {raw.FPDF_ERR_PASSWORD, raw.FPDF_ERR_SECURITY}:
            raise _ContractExit(
                EXIT_ENCRYPTED,
                {"schema": SCHEMA, "error": "encrypted"},
                str(exc),
            ) from exc
        if err_code in {raw.FPDF_ERR_FILE, raw.FPDF_ERR_FORMAT}:
            raise _ContractExit(
                EXIT_CORRUPT,
                {"schema": SCHEMA, "error": "corrupt", "detail": str(exc)},
                str(exc),
            ) from exc
        raise


def _extract_page(
    doc: Any,
    raw: Any,
    page_index: int,
    *,
    include_text: bool,
    warnings: list[str],
) -> dict[str, Any]:
    page_number = page_index + 1
    entry: dict[str, Any] = {
        "index": page_number,
        "chars": 0,
        "width_pt": 0.0,
        "height_pt": 0.0,
        "image_area_fraction": 0.0,
        "rendered": None,
        "error": None,
    }
    if include_text:
        entry["text"] = ""

    try:
        page = doc.get_page(page_index)
        width_pt, height_pt = page.get_size()
        entry["width_pt"] = float(width_pt)
        entry["height_pt"] = float(height_pt)

        text = _page_text(page)
        image_area_fraction = _image_area_fraction(page, raw, width_pt, height_pt)
    except Exception as exc:
        _record_page_error(entry, warnings, page_number, "page extraction", exc)
        return entry

    entry["chars"] = sum(1 for char in text if not char.isspace())
    entry["image_area_fraction"] = image_area_fraction
    if include_text:
        entry["text"] = text
    return entry


def _page_text(page: Any) -> str:
    textpage = page.get_textpage()
    return textpage.get_text_range()


def _image_area_fraction(
    page: Any, raw: Any, width_pt: float, height_pt: float
) -> float:
    page_area = width_pt * height_pt
    if page_area <= 0:
        return 0.0
    image_area = 0.0
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]):
        left, bottom, right, top = obj.get_bounds()
        image_area += max(0.0, right - left) * max(0.0, top - bottom)
    return max(0.0, min(1.0, image_area / page_area))


def _render_selected_pages(
    pdfium: Any,
    doc: Any,
    raw: Any,
    pages: list[dict[str, Any]],
    render_request: _RenderRequest,
    warnings: list[str],
) -> None:
    assert render_request.render_dir is not None
    try:
        render_request.render_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _ContractExit(
            EXIT_RENDER_IO,
            {"schema": SCHEMA, "error": "render-io", "detail": str(exc)},
            str(exc),
        ) from exc

    selected = _selected_pages(pages, render_request)
    for page_number in sorted(selected):
        page = pages[page_number - 1]
        if page["error"] is not None:
            continue
        rendered_name = f"page-{page_number:04d}.png"
        rendered_path = render_request.render_dir / rendered_name
        try:
            _render_page_png(
                pdfium,
                doc,
                raw,
                page_number - 1,
                page["width_pt"],
                page["height_pt"],
                render_request.dpi,
                rendered_path,
            )
        except OSError as exc:
            raise _ContractExit(
                EXIT_RENDER_IO,
                {"schema": SCHEMA, "error": "render-io", "detail": str(exc)},
                str(exc),
            ) from exc
        except Exception as exc:
            _record_page_error(page, warnings, page_number, "page render", exc)
            continue
        page["rendered"] = rendered_name


def _selected_pages(
    pages: list[dict[str, Any]], render_request: _RenderRequest
) -> set[int]:
    selected = set(render_request.pages)
    if render_request.below_chars is not None:
        selected.update(
            page["index"]
            for page in pages
            if page["error"] is None and page["chars"] < render_request.below_chars
        )
    if render_request.above_image_fraction is not None:
        selected.update(
            page["index"]
            for page in pages
            if page["error"] is None
            and page["image_area_fraction"] >= render_request.above_image_fraction
        )
    return selected


def _render_page_png(
    pdfium: Any,
    doc: Any,
    raw: Any,
    page_index: int,
    width_pt: float,
    height_pt: float,
    dpi: int,
    output_path: Path,
) -> None:
    page = doc.get_page(page_index)
    width_px = round(width_pt * dpi / 72)
    height_px = round(height_pt * dpi / 72)
    # Do not replace this with page.render(scale=dpi/72): pypdfium2 v5 ceils
    # float products there, producing 1275x1651 for 612x792pt at 150 dpi.
    native_bitmap = pdfium.PdfBitmap.new_native(
        width_px,
        height_px,
        format=raw.FPDFBitmap_BGR,
    )
    native_bitmap.fill_rect((255, 255, 255, 255), 0, 0, width_px, height_px)
    raw.FPDF_RenderPageBitmap(
        native_bitmap,
        page,
        0,
        0,
        width_px,
        height_px,
        0,
        0,
    )
    native_bitmap.to_pil().save(output_path)


def _record_page_error(
    entry: dict[str, Any],
    warnings: list[str],
    page_number: int,
    stage: str,
    exc: Exception,
) -> None:
    message = f"page {page_number}: {stage} failed: {exc}"
    entry["error"] = message
    entry["chars"] = 0
    entry["image_area_fraction"] = 0.0
    entry["rendered"] = None
    if "text" in entry:
        entry["text"] = ""
    warnings.append(message)


def _metadata_payload(doc: Any) -> dict[str, str | None]:
    metadata = doc.get_metadata_dict()
    return {
        "title": _optional_text(metadata.get("Title")),
        "author": _optional_text(metadata.get("Author")),
        "creation_date": _parse_pdf_date(metadata.get("CreationDate")),
        "mod_date": _parse_pdf_date(metadata.get("ModDate")),
        "producer": _optional_text(metadata.get("Producer")),
    }


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or value == "":
        return None
    return value


def _parse_pdf_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _PDF_DATE_RE.match(value)
    if not match:
        return None
    parts = match.groupdict()
    try:
        dt.datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts["hour"]),
            int(parts["minute"]),
            int(parts["second"]),
        )
    except ValueError:
        return None
    prefix = (
        f"{parts['year']}-{parts['month']}-{parts['day']}"
        f"T{parts['hour']}:{parts['minute']}:{parts['second']}"
    )
    if parts["z"]:
        return prefix + "Z"
    if not parts["sign"]:
        return None
    return prefix + f"{parts['sign']}{parts['tzhour']}:{parts['tzminute']}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_stdout(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _apply_rlimits_from_env() -> None:
    as_mb = _positive_env_int(ENV_RLIMIT_AS_MB, DEFAULT_RLIMIT_AS_MB)
    cpu_seconds = _positive_env_int(
        ENV_RLIMIT_CPU_SECONDS,
        DEFAULT_RLIMIT_CPU_SECONDS,
    )
    if as_mb is not None:
        _set_limit(resource.RLIMIT_AS, as_mb * 1024 * 1024)
    if cpu_seconds is not None:
        _set_limit(resource.RLIMIT_CPU, cpu_seconds)


def _positive_env_int(name: str, default: int) -> int | None:
    raw_value = os.getenv(name)
    value = default if raw_value is None else int(raw_value)
    if value <= 0:
        return None
    return value


def _set_limit(kind: int, limit: int) -> None:
    _soft, hard = resource.getrlimit(kind)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(kind, (limit, limit))


def _entrypoint() -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s"
    )
    try:
        _apply_rlimits_from_env()
    except Exception as exc:
        logger.exception("PDF worker failed to apply resource limits")
        _write_json_stdout({"schema": SCHEMA, "error": "internal", "detail": str(exc)})
        raise SystemExit(EXIT_INTERNAL) from exc
    raise SystemExit(main())


if __name__ == "__main__":
    _entrypoint()

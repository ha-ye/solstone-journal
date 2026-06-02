# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Segment PDF text extraction handler."""

import argparse
import json
import logging
import os
from pathlib import Path

from solstone.observe.utils import get_segment_key
from solstone.think.importers.documents import extract_pdf_text
from solstone.think.utils import require_solstone, setup_cli

logger = logging.getLogger(__name__)


def _build_header(raw_name: str, kind: str) -> dict:
    header = {"raw": raw_name, "kind": kind}

    observer = os.getenv("OBSERVER_NAME")
    if observer:
        header["observer"] = observer

    segment_meta_str = os.getenv("SEGMENT_META")
    if segment_meta_str:
        try:
            segment_meta = json.loads(segment_meta_str)
            for key, value in segment_meta.items():
                header[key] = value
        except json.JSONDecodeError:
            logger.warning("Invalid SEGMENT_META JSON: %s", segment_meta_str[:100])

    return header


def run(pdf_path: Path, *, redo: bool = False) -> Path | None:
    output_path = pdf_path.with_suffix(".jsonl")
    if output_path.exists() and not redo:
        logger.info("Already processed: %s", pdf_path)
        return None

    text, meta = extract_pdf_text(pdf_path)
    if meta["vision_error"]:
        raise RuntimeError(
            f"PDF text extraction failed for {pdf_path.name}: scanned PDF, vision failed ({meta['vision_error']})"
        )

    header = _build_header(pdf_path.name, "document")
    header["page_count"] = meta["page_count"]
    header["extraction_method"] = meta["extraction_method"]
    entry = {"start": "00:00:00", "text": text}
    output_path.write_text(
        json.dumps(header) + "\n" + json.dumps(entry) + "\n", encoding="utf-8"
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract text from a segment PDF")
    parser.add_argument("pdf_path", help="Path to PDF in a segment directory")
    parser.add_argument("--redo", action="store_true", help="Reprocess existing output")
    args = setup_cli(parser)
    require_solstone()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        parser.error(f"PDF not found: {pdf_path}")
    if get_segment_key(pdf_path) is None:
        parser.error(
            "PDF must be in a segment directory (HHMMSS_LEN/), "
            f"but parent is: {pdf_path.parent.name}"
        )

    try:
        run(pdf_path, redo=args.redo)
    except Exception as exc:
        logger.error("Failed to process %s: %s", pdf_path, exc, exc_info=True)
        raise


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Migrate legacy segment PDF extraction JSONL into document transcripts.

Delete-safety rule: never remove a legacy JSONL until any replacement markdown
has been written, verified readable, and verified non-empty. Duplicate
document/image JSONL files that already sit beside a transcript markdown are
deleted because the transcript is already the durable readable form.

Re-drain consequence: historical PDF days will fingerprint differently once
because raw PDF originals now contribute size markers. This migration must not
rebaseline stored catchup fingerprints; catchup owns that state transition.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from solstone.think.media import PDF_EXTENSIONS
from solstone.think.utils import get_journal, setup_cli


def _display_path(journal_root: Path, path: Path) -> str:
    try:
        return path.relative_to(journal_root).as_posix()
    except ValueError:
        return str(path)


def _read_header(path: Path) -> dict[str, Any] | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        header = json.loads(first_line)
    except (IndexError, OSError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) else None


def _same_stem_pdf_exists(path: Path) -> bool:
    return any(path.with_suffix(suffix).is_file() for suffix in PDF_EXTENSIONS)


def _read_document_text(path: Path) -> str | None:
    parts: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines[1:]:
        if not raw_line.strip():
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts).strip()


def _render_document_markdown(title: str, text: str, header: dict[str, Any]) -> str:
    lines = [f"# {title}", "", "**Type:** Document"]
    if header.get("page_count") is not None:
        lines.append(f"**Pages:** {header['page_count']}")
    if header.get("extraction_method"):
        lines.append(f"**Extraction method:** {header['extraction_method']}")
    lines.extend(["", "---", "", text.strip()])
    return "\n".join(lines).rstrip() + "\n"


def _write_verified(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    if not path.is_file():
        raise RuntimeError(f"failed to write {path}")
    written = path.read_text(encoding="utf-8")
    if not written.strip():
        raise RuntimeError(f"wrote empty transcript {path}")


def migrate_pdf_extractions(
    journal_root: Path, *, reported: list[str] | None = None
) -> dict[str, int]:
    """Migrate legacy PDF extraction JSONL files."""
    counts = {
        "scanned": 0,
        "deleted_duplicates": 0,
        "converted_documents": 0,
        "skipped_unparseable": 0,
        "skipped_no_text": 0,
    }
    chronicle = journal_root / "chronicle"
    for jsonl_path in sorted(chronicle.glob("*/*/*/*.jsonl")):
        header = _read_header(jsonl_path)
        if header is None:
            if not _same_stem_pdf_exists(jsonl_path):
                continue
            counts["scanned"] += 1
            counts["skipped_unparseable"] += 1
            if reported is not None:
                reported.append(_display_path(journal_root, jsonl_path))
            continue

        kind = header.get("kind")
        if kind not in {"document", "image"}:
            continue

        counts["scanned"] += 1

        if any(jsonl_path.parent.glob("*_transcript.md")):
            jsonl_path.unlink()
            counts["deleted_duplicates"] += 1
            continue

        if kind != "document":
            continue

        text = _read_document_text(jsonl_path)
        if text is None:
            counts["skipped_unparseable"] += 1
            if reported is not None:
                reported.append(_display_path(journal_root, jsonl_path))
            continue
        if not text:
            counts["skipped_no_text"] += 1
            if reported is not None:
                reported.append(_display_path(journal_root, jsonl_path))
            continue

        raw_name = header.get("raw")
        title = Path(raw_name).stem if isinstance(raw_name, str) else jsonl_path.stem
        md_path = jsonl_path.parent / "document_transcript.md"
        _write_verified(md_path, _render_document_markdown(title, text, header))
        jsonl_path.unlink()
        counts["converted_documents"] += 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    setup_cli(parser)
    journal_root = Path(get_journal())
    reported: list[str] = []
    counts = migrate_pdf_extractions(journal_root, reported=reported)
    print(f"Scanned {counts['scanned']} JSONL file(s)")
    print(f"Deleted {counts['deleted_duplicates']} duplicate extraction JSONL file(s)")
    print(f"Converted {counts['converted_documents']} document JSONL file(s)")
    print(f"Skipped {counts['skipped_unparseable']} unparseable JSONL file(s)")
    print(f"Skipped {counts['skipped_no_text']} document JSONL file(s) with no text")
    for path in reported:
        print(f"Left in place: {path}")


if __name__ == "__main__":
    main()

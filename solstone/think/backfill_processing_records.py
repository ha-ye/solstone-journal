# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backfill empty processing records onto header-only native analysis outputs."""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from solstone.observe.processing_record import (
    HANDLER_DESCRIBE,
    HANDLER_TRANSCRIBE,
    REASON_NO_DECODABLE_AUDIO,
    REASON_NO_DECODABLE_FRAMES,
    STATE_EMPTY,
    build_processing_record,
)
from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.media import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from solstone.think.utils import DATE_RE, day_dirs, iter_segments, setup_cli

logger = logging.getLogger(__name__)

SCREEN_GLOBS = ("screen.jsonl", "*_screen.jsonl")
AUDIO_GLOBS = ("audio.jsonl", "*_audio.jsonl")


class Outcome(StrEnum):
    STAMP_EMPTY = "stamp_empty"
    SKIP_HAS_RECORD = "skip_has_record"
    SKIP_CHUNK_BEARING = "skip_chunk_bearing"
    SKIP_MARKER = "skip_marker"
    SKIP_INELIGIBLE = "skip_ineligible"
    SKIP_UNREADABLE = "skip_unreadable"


@dataclass(frozen=True)
class ModalitySpec:
    modality: str
    globs: tuple[str, ...]
    chunk_key: str
    media_exts: frozenset[str]
    handler: str
    reason: str


SCREEN_SPEC = ModalitySpec(
    modality="screen",
    globs=SCREEN_GLOBS,
    chunk_key="timestamp",
    media_exts=VIDEO_EXTENSIONS,
    handler=HANDLER_DESCRIBE,
    reason=REASON_NO_DECODABLE_FRAMES,
)
AUDIO_SPEC = ModalitySpec(
    modality="audio",
    globs=AUDIO_GLOBS,
    chunk_key="start",
    media_exts=AUDIO_EXTENSIONS,
    handler=HANDLER_TRANSCRIBE,
    reason=REASON_NO_DECODABLE_AUDIO,
)
SPECS = (SCREEN_SPEC, AUDIO_SPEC)


def _match_spec(jsonl_path: Path) -> ModalitySpec | None:
    name = jsonl_path.name
    for spec in SPECS:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in spec.globs):
            return spec
    return None


def _find_media_sibling(
    seg_path: Path, jsonl_path: Path, spec: ModalitySpec
) -> Path | None:
    try:
        paths = sorted(seg_path.iterdir())
    except OSError as exc:
        logger.warning("Could not list segment directory %s: %s", seg_path, exc)
        return None

    for path in paths:
        if (
            path.is_file()
            and path.stem == jsonl_path.stem
            and path.suffix.lower() in spec.media_exts
        ):
            return path
    return None


def _first_json_dict(text: str, jsonl_path: Path) -> dict | None:
    for line in text.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            return parsed
        logger.warning("First JSONL row is not an object: %s", jsonl_path)
        return None
    logger.warning("JSONL has no nonblank header row: %s", jsonl_path)
    return None


def _has_chunk_row(text: str, chunk_key: str) -> bool:
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and chunk_key in parsed:
            return True
    return False


def _has_marker(seg_path: Path, modality: str) -> bool:
    return (seg_path / f".analyzing_{modality}").exists() or (
        seg_path / f".analyze_failed_{modality}"
    ).exists()


def classify_output(
    stream_name: str, seg_path: Path, jsonl_path: Path
) -> tuple[Outcome, ModalitySpec | None]:
    spec = _match_spec(jsonl_path)
    if spec is None:
        return Outcome.SKIP_INELIGIBLE, None

    if stream_name.startswith("import."):
        return Outcome.SKIP_INELIGIBLE, spec

    if _find_media_sibling(seg_path, jsonl_path, spec) is None:
        return Outcome.SKIP_INELIGIBLE, spec

    try:
        text = jsonl_path.read_text(encoding="utf-8")
        header = _first_json_dict(text, jsonl_path)
    except OSError as exc:
        logger.warning("Could not read %s: %s", jsonl_path, exc)
        return Outcome.SKIP_UNREADABLE, spec
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Could not parse header row in %s: %s", jsonl_path, exc)
        return Outcome.SKIP_UNREADABLE, spec

    if header is None:
        return Outcome.SKIP_UNREADABLE, spec

    if isinstance(header.get("_solstone_processing"), dict):
        return Outcome.SKIP_HAS_RECORD, spec

    if _has_chunk_row(text, spec.chunk_key):
        return Outcome.SKIP_CHUNK_BEARING, spec

    if _has_marker(seg_path, spec.modality):
        return Outcome.SKIP_MARKER, spec

    return Outcome.STAMP_EMPTY, spec


def stamp_empty_record(seg_path: Path, jsonl_path: Path, spec: ModalitySpec) -> None:
    media_sibling = _find_media_sibling(seg_path, jsonl_path, spec)
    try:
        input_size = media_sibling.stat().st_size if media_sibling is not None else 0
    except OSError:
        input_size = 0

    record = build_processing_record(
        state=STATE_EMPTY,
        reason_code=spec.reason,
        handler=spec.handler,
        input_size=input_size,
        source="backfill",
    )

    original_text = jsonl_path.read_text(encoding="utf-8")
    lines = original_text.splitlines(keepends=True)
    header_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if header_index is None:
        raise ValueError(f"JSONL has no nonblank header row: {jsonl_path}")

    header = json.loads(lines[header_index])
    if not isinstance(header, dict):
        raise ValueError(f"First JSONL row is not an object: {jsonl_path}")
    header["_solstone_processing"] = record
    lines[header_index] = json.dumps(header) + "\n"

    atomic_replace(jsonl_path, "".join(lines))


def _zero_counts() -> dict[Outcome, int]:
    return {outcome: 0 for outcome in Outcome}


def run_backfill(day: str | None, *, commit: bool) -> dict[Outcome, int]:
    if day is not None:
        if not DATE_RE.fullmatch(day):
            raise ValueError("expected day in YYYYMMDD format")
        days = [day]
    else:
        days = sorted(day_dirs())

    counts = _zero_counts()
    for current_day in days:
        day_counts = _zero_counts()
        for stream_name, _seg_key, seg_path in iter_segments(current_day):
            for jsonl_path in sorted(seg_path.glob("*.jsonl")):
                try:
                    outcome, spec = classify_output(stream_name, seg_path, jsonl_path)
                except Exception:
                    logger.exception("Could not classify %s", jsonl_path)
                    outcome = Outcome.SKIP_UNREADABLE
                    spec = None

                counts[outcome] += 1
                day_counts[outcome] += 1
                if commit and outcome is Outcome.STAMP_EMPTY:
                    if spec is None:
                        raise RuntimeError(
                            f"Eligible output has no modality spec: {jsonl_path}"
                        )
                    stamp_empty_record(seg_path, jsonl_path, spec)

        logger.info(
            "Backfill processing-records day=%s total=%s stamp_empty=%s",
            current_day,
            sum(day_counts.values()),
            day_counts[Outcome.STAMP_EMPTY],
        )

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill empty processing records onto stuck header-only native "
            "describe/transcribe outputs"
        )
    )
    parser.add_argument("--day", help="Day in YYYYMMDD format; defaults to all days")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--commit",
        action="store_true",
        help="Rewrite eligible JSONL headers with empty processing records",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview eligible outputs without writing changes",
    )

    args = setup_cli(parser)
    try:
        counts = run_backfill(args.day, commit=bool(args.commit))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    print("COMMITTED" if args.commit else "DRY RUN (no changes written)")
    total = 0
    for outcome in Outcome:
        count = counts[outcome]
        total += count
        print(f"{outcome.value}: {count}")
    print(f"total: {total}")


if __name__ == "__main__":
    main()

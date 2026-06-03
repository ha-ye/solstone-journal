# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Delete an allowed source stream from the observer-owned journal surface."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from solstone.apps.observer.utils import prune_history_by_stream
from solstone.think.facets import get_facets
from solstone.think.indexer.journal import prune_chunks_by_stream
from solstone.think.streams import delete_stream_state
from solstone.think.utils import day_dirs, get_journal, iter_segments

logger = logging.getLogger(__name__)

SHARE_STREAM = "import.share"
LOCATION_STREAM = "location"
DELETABLE_SOURCE_STREAMS = {SHARE_STREAM, LOCATION_STREAM}

_SEGMENT_NOT_REMOVED_REASON = (
    "This segment could not be removed from disk. Try again after checking file "
    "permissions."
)
_INDEX_NOT_REMOVED_REASON = (
    "The search index could not be updated. The imported files may be gone, but "
    "search results may still mention them until this is repaired."
)
_STREAM_STATE_NOT_REMOVED_REASON = (
    "The stream state file could not be removed from disk. Try again after "
    "checking file permissions."
)
_HISTORY_NOT_REMOVED_REASON = (
    "Observer history could not be updated. The imported files may be gone, but "
    "this source may still appear there until this is repaired."
)


def _day_display(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _classify_segment_files(seg_path: Path, stream: str) -> tuple[int, int]:
    originals = 0
    derived = 0
    for file_path in seg_path.iterdir():
        if not file_path.is_file():
            continue
        if file_path.name in {"item.json", "stream.json"}:
            continue
        if stream == LOCATION_STREAM and file_path.suffix in {".jsonl", ".npz"}:
            originals += 1
        elif file_path.suffix in {".jsonl", ".npz"}:
            derived += 1
        else:
            originals += 1
    return originals, derived


def _not_confirmed_entries(
    journal: str,
    days_with_segments: dict[str, list[Path]],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    facets = get_facets()
    for day in sorted(days_with_segments):
        day_fmt = _day_display(day)
        for facet_name, meta in facets.items():
            facet_dir = Path(
                meta.get("path") or (Path(journal) / "facets" / facet_name)
            )
            checks = [
                (
                    facet_dir / "entities" / f"{day}.jsonl",
                    "people and topics",
                    "This was merged into this day's people and topics; can't remove just this source's part.",
                ),
                (
                    facet_dir / "logs" / f"{day}.jsonl",
                    "activity summary",
                    "This was merged into this day's activity summary; can't remove just this source's part.",
                ),
                (
                    facet_dir / "news" / f"{day}.md",
                    "news",
                    "This was merged into this day's news; can't remove just this source's part.",
                ),
            ]
            for path, kind, reason in checks:
                if path.exists():
                    entries.append(
                        {
                            "what": f"{facet_name} {day_fmt}: {kind}",
                            "plain_reason": reason,
                        }
                    )
    return entries


def delete_source_stream(stream: str) -> dict:
    """Delete everything attributed to an allowed source stream."""
    if stream not in DELETABLE_SOURCE_STREAMS:
        raise ValueError(f"Cannot delete unsupported source stream: {stream!r}")

    journal = str(Path(get_journal()).resolve())

    days_with_segments: dict[str, list[Path]] = {}
    for day in day_dirs():
        segs = [
            seg_path
            for seg_stream, _segment, seg_path in iter_segments(day)
            if seg_stream == stream
        ]
        if segs:
            days_with_segments[day] = segs

    originals = 0
    segments = 0
    in_segment_derived = 0
    index_chunks = 0
    stream_identity = 0
    history_rows = 0
    not_removed: list[dict[str, str]] = []

    for day in sorted(days_with_segments):
        day_fmt = _day_display(day)
        segs = days_with_segments[day]
        for seg_path in segs:
            try:
                segment_originals, segment_derived = _classify_segment_files(
                    seg_path,
                    stream,
                )
                shutil.rmtree(seg_path)
            except OSError as exc:
                logger.warning(
                    "Failed to remove %s segment %s: %s",
                    stream,
                    seg_path,
                    exc,
                )
                not_removed.append(
                    {
                        "what": f"{stream} {day_fmt} {seg_path.name}: segment",
                        "plain_reason": _SEGMENT_NOT_REMOVED_REASON,
                    }
                )
                continue
            originals += segment_originals
            in_segment_derived += segment_derived
            segments += 1

        stream_dir = segs[0].parent
        try:
            if stream_dir.exists() and not any(stream_dir.iterdir()):
                stream_dir.rmdir()
        except OSError:
            pass

    try:
        index_result = prune_chunks_by_stream(stream)
        index_chunks = index_result["chunks"]
    except Exception as exc:
        logger.warning("Failed to prune %s search index: %s", stream, exc)
        not_removed.append(
            {
                "what": "search index",
                "plain_reason": _INDEX_NOT_REMOVED_REASON,
            }
        )

    try:
        stream_identity = 1 if delete_stream_state(stream) else 0
    except OSError as exc:
        logger.warning("Failed to remove %s stream state: %s", stream, exc)
        not_removed.append(
            {
                "what": f"{stream} stream state",
                "plain_reason": _STREAM_STATE_NOT_REMOVED_REASON,
            }
        )

    try:
        history_rows = prune_history_by_stream(stream)
    except Exception as exc:
        logger.warning("Failed to prune %s observer history: %s", stream, exc)
        not_removed.append(
            {
                "what": "observer history",
                "plain_reason": _HISTORY_NOT_REMOVED_REASON,
            }
        )

    not_confirmed = _not_confirmed_entries(journal, days_with_segments)
    removed = {
        "originals": originals,
        "segments": segments,
        "in_segment_derived": in_segment_derived,
        "index_chunks": index_chunks,
        "stream_identity": stream_identity,
        "history_rows": history_rows,
    }
    # The location source's owner-facing delete receipt counts distinct days
    # ("removed ... across {N} days"); surface the day count the op already
    # computed. import.share's receipt shape is left unchanged.
    if stream == LOCATION_STREAM:
        removed["days"] = len(days_with_segments)
    receipt = {
        "target": {
            "stream": stream,
            "journal": journal,
        },
        "removed": removed,
        "not_confirmed": not_confirmed,
        "not_removed": not_removed,
        "backup_hosted": "not confirmed",
    }
    logger.info(
        "Deleted %s source: originals=%s segments=%s derived=%s "
        "index_chunks=%s stream_identity=%s history_rows=%s not_confirmed=%s "
        "not_removed=%s",
        stream,
        originals,
        segments,
        in_segment_derived,
        index_chunks,
        stream_identity,
        history_rows,
        len(not_confirmed),
        len(not_removed),
    )
    return receipt

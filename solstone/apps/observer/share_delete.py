# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Delete an allowed source stream from the observer-owned journal surface."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path

from solstone.apps.observer.source_discovery import (
    LOCATION_ORIGINAL,
    find_location_sources,
)
from solstone.apps.observer.utils import has_history_for_stream, prune_history_by_stream
from solstone.think.facets import get_facets
from solstone.think.indexer.journal import prune_chunks_by_stream
from solstone.think.streams import delete_stream_state
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)

LOCATION_STREAM = "location"
DELETABLE_SOURCE_STREAMS = {LOCATION_STREAM}

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
_ORIGINAL_NOT_REMOVED_REASON = (
    "This source file could not be removed from disk. Try again after checking file "
    "permissions."
)


def _day_display(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _not_confirmed_entries(
    journal: str,
    days: Iterable[str],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    facets = get_facets()
    for day in sorted(days):
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

    originals = 0
    segments = 0
    mixed_segments = 0
    in_segment_derived = 0
    index_chunks = 0
    stream_identity = 0
    history_rows = 0
    not_removed: list[dict[str, str]] = []
    days: set[str] = set()
    location_only_parents: set[Path] = set()
    mobile_streams_touched: set[str] = set()

    for src in find_location_sources():
        day_fmt = _day_display(src.day)
        if src.is_mixed:
            try:
                (src.path / LOCATION_ORIGINAL).unlink()
            except OSError as exc:
                logger.warning(
                    "Failed to remove location original in %s: %s",
                    src.path,
                    exc,
                )
                not_removed.append(
                    {
                        "what": f"{src.stream} {day_fmt} {src.segment}: location data",
                        "plain_reason": _ORIGINAL_NOT_REMOVED_REASON,
                    }
                )
                continue
            originals += 1
            mixed_segments += 1
            days.add(src.day)
            if src.stream != LOCATION_STREAM:
                mobile_streams_touched.add(src.stream)
        else:
            try:
                shutil.rmtree(src.path)
            except OSError as exc:
                logger.warning(
                    "Failed to remove %s segment %s: %s",
                    src.stream,
                    src.path,
                    exc,
                )
                not_removed.append(
                    {
                        "what": f"{src.stream} {day_fmt} {src.segment}: segment",
                        "plain_reason": _SEGMENT_NOT_REMOVED_REASON,
                    }
                )
                continue
            originals += 1
            segments += 1
            days.add(src.day)
            if src.stream == LOCATION_STREAM:
                location_only_parents.add(src.path.parent)
            else:
                mobile_streams_touched.add(src.stream)

    for parent in location_only_parents:
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
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

    not_confirmed = _not_confirmed_entries(journal, days)
    for mobile_stream in sorted(mobile_streams_touched):
        if has_history_for_stream(mobile_stream):
            not_confirmed.append(
                {
                    "what": f"{mobile_stream}: import history",
                    "plain_reason": "This source was imported together with others in one record; its history entry can't be removed on its own.",
                }
            )

    removed = {
        "originals": originals,
        "segments": segments,
        "mixed_segments": mixed_segments,
        "in_segment_derived": in_segment_derived,
        "index_chunks": index_chunks,
        "stream_identity": stream_identity,
        "history_rows": history_rows,
    }
    # The location source's owner-facing delete receipt counts distinct days
    # ("removed ... across {N} days"); surface the day count the op already
    # computed.
    if stream == LOCATION_STREAM:
        removed["days"] = len(days)
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
        "Deleted %s source: originals=%s segments=%s mixed_segments=%s derived=%s "
        "index_chunks=%s stream_identity=%s history_rows=%s not_confirmed=%s "
        "not_removed=%s",
        stream,
        originals,
        segments,
        mixed_segments,
        in_segment_derived,
        index_chunks,
        stream_identity,
        history_rows,
        len(not_confirmed),
        len(not_removed),
    )
    return receipt

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Delete the iOS Share Sheet source from the observer-owned journal surface."""

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


def _classify_segment_files(seg_path: Path) -> tuple[int, int]:
    originals = 0
    derived = 0
    for file_path in seg_path.iterdir():
        if not file_path.is_file():
            continue
        if file_path.name in {"item.json", "stream.json"}:
            continue
        if file_path.suffix in {".jsonl", ".npz"}:
            derived += 1
        else:
            originals += 1
    return originals, derived


def _not_confirmed_entries(
    journal: str,
    days_with_share: dict[str, list[Path]],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    facets = get_facets()
    for day in sorted(days_with_share):
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


def delete_share_source() -> dict:
    """Delete everything attributed to the import.share source."""
    journal = str(Path(get_journal()).resolve())

    days_with_share: dict[str, list[Path]] = {}
    for day in day_dirs():
        segs = [
            seg_path
            for stream, _segment, seg_path in iter_segments(day)
            if stream == SHARE_STREAM
        ]
        if segs:
            days_with_share[day] = segs

    originals = 0
    segments = 0
    in_segment_derived = 0
    index_chunks = 0
    stream_identity = 0
    history_rows = 0
    not_removed: list[dict[str, str]] = []

    for day in sorted(days_with_share):
        day_fmt = _day_display(day)
        segs = days_with_share[day]
        for seg_path in segs:
            try:
                segment_originals, segment_derived = _classify_segment_files(seg_path)
                shutil.rmtree(seg_path)
            except OSError as exc:
                logger.warning(
                    "Failed to remove import.share segment %s: %s",
                    seg_path,
                    exc,
                )
                not_removed.append(
                    {
                        "what": f"import.share {day_fmt} {seg_path.name}: segment",
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
        index_result = prune_chunks_by_stream(SHARE_STREAM)
        index_chunks = index_result["chunks"]
    except Exception as exc:
        logger.warning("Failed to prune import.share search index: %s", exc)
        not_removed.append(
            {
                "what": "search index",
                "plain_reason": _INDEX_NOT_REMOVED_REASON,
            }
        )

    try:
        stream_identity = 1 if delete_stream_state(SHARE_STREAM) else 0
    except OSError as exc:
        logger.warning("Failed to remove import.share stream state: %s", exc)
        not_removed.append(
            {
                "what": "import.share stream state",
                "plain_reason": _STREAM_STATE_NOT_REMOVED_REASON,
            }
        )

    try:
        history_rows = prune_history_by_stream(SHARE_STREAM)
    except Exception as exc:
        logger.warning("Failed to prune import.share observer history: %s", exc)
        not_removed.append(
            {
                "what": "observer history",
                "plain_reason": _HISTORY_NOT_REMOVED_REASON,
            }
        )

    not_confirmed = _not_confirmed_entries(journal, days_with_share)
    receipt = {
        "target": {
            "stream": SHARE_STREAM,
            "journal": journal,
        },
        "removed": {
            "originals": originals,
            "segments": segments,
            "in_segment_derived": in_segment_derived,
            "index_chunks": index_chunks,
            "stream_identity": stream_identity,
            "history_rows": history_rows,
        },
        "not_confirmed": not_confirmed,
        "not_removed": not_removed,
        "backup_hosted": "not confirmed",
    }
    logger.info(
        "Deleted import.share source: originals=%s segments=%s derived=%s "
        "index_chunks=%s stream_identity=%s history_rows=%s not_confirmed=%s "
        "not_removed=%s",
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

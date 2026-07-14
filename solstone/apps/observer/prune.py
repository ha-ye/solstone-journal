# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Observer duplicate segment pruning."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solstone.apps.observer.utils import (
    ContentIdentityFile,
    ContentIdentityIssue,
    append_history_record,
    content_identity_from_segment,
    content_identity_key,
    get_hist_dir,
    is_structural_derived_file,
    list_observers,
    load_history,
    observer_filename_prefix,
)
from solstone.think.indexer.journal import delete_segment_index_rows
from solstone.think.segment_files import (
    INGEST_MANIFEST_NAME,
    RESERVED_SEGMENT_FILENAMES,
)
from solstone.think.streams import (
    get_stream_state,
    read_segment_stream,
    repair_stream_state_tail,
    touch_stream_health_marker,
    write_segment_stream,
)
from solstone.think.utils import day_dirs, get_journal, iter_segments, now_ms

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Refusal:
    subject: str
    gate: str
    file: str | None
    resolution: str


@dataclass
class SegmentAnalysis:
    day: str
    stream: str
    segment: str
    path: Path
    marker: dict[str, Any] | None
    identity: dict[str, ContentIdentityFile]
    identity_issue: ContentIdentityIssue | None
    marker_error: str | None
    unknown_files: list[str]

    @property
    def label(self) -> str:
        return f"{self.day}/{self.stream}/{self.segment}"

    @property
    def identity_key(self) -> tuple:
        return content_identity_key(self.identity)


@dataclass
class PruneCandidate:
    analysis: SegmentAnalysis
    last_physical_copy: bool


@dataclass
class PruneGroup:
    day: str
    stream: str
    start: str
    canonical: SegmentAnalysis
    candidates: list[PruneCandidate]


@dataclass
class PruneResult:
    execute: bool
    groups: list[PruneGroup] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    deleted: list[PruneCandidate] = field(default_factory=list)
    index_errors: list[str] = field(default_factory=list)
    crash_repaired: int = 0
    chain_repaired: int = 0

    @property
    def last_physical_copy_count(self) -> int:
        items = (
            self.deleted
            if self.execute
            else [candidate for group in self.groups for candidate in group.candidates]
        )
        return sum(1 for candidate in items if candidate.last_physical_copy)


def resolve_prune_days(
    *, day: str | None, day_range: str | None, all_days: bool
) -> list[str]:
    """Resolve the mutually-exclusive prune day selector."""
    selected = [value is not None and value is not False for value in (day, day_range)]
    if sum(selected) + int(all_days) != 1:
        raise ValueError("choose exactly one of --day, --day-range, or --all")
    if day:
        _validate_day(day)
        return [day]
    if day_range:
        if ".." not in day_range:
            raise ValueError("--day-range must use A..B")
        start_text, end_text = day_range.split("..", 1)
        _validate_day(start_text)
        _validate_day(end_text)
        start = datetime.strptime(start_text, "%Y%m%d")
        end = datetime.strptime(end_text, "%Y%m%d")
        if end < start:
            raise ValueError("--day-range end must be on or after start")
        days = []
        current = start
        while current <= end:
            days.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return days
    return sorted(day_dirs().keys())


def run_prune(
    *,
    days: list[str],
    stream: str | None = None,
    execute: bool = False,
) -> PruneResult:
    """Plan or execute observer duplicate pruning."""
    if execute:
        recovery_refusals, repaired = repair_crash_leftovers(days, stream=stream)
        result = _plan(days, stream=stream)
        result.execute = True
        result.refusals = [*recovery_refusals, *result.refusals]
        result.crash_repaired = repaired
        if recovery_refusals:
            return result
        _execute_plan(result)
        return result
    return _plan(days, stream=stream)


def format_result(result: PruneResult) -> str:
    """Format prune output for the operator CLI."""
    candidates = [
        candidate for group in result.groups for candidate in group.candidates
    ]
    lines = [
        ("observer prune execute" if result.execute else "observer prune dry-run"),
        f"groups: {len(result.groups)}",
        f"candidates: {len(candidates)}",
        f"deleted: {len(result.deleted)}",
        f"chain-repaired: {result.chain_repaired}",
        f"last-physical-copy: {result.last_physical_copy_count}",
        f"refusals: {len(result.refusals)}",
    ]
    if result.crash_repaired:
        lines.append(f"crash-repaired: {result.crash_repaired}")
    for group in result.groups:
        lines.append(
            "group "
            f"{group.day}/{group.stream}/{group.start}_*: "
            f"canonical={group.canonical.segment} "
            f"candidates={len(group.candidates)}"
        )
        for candidate in group.candidates:
            if candidate.last_physical_copy:
                prefix = "deleted" if candidate in result.deleted else "would-delete"
                lines.append(
                    "  "
                    f"{prefix}: {candidate.analysis.segment} "
                    f"duplicate_of={group.canonical.segment} "
                    "[last-physical-copy]"
                )
    if result.index_errors:
        lines.append("index errors:")
        lines.extend(f"  {error}" for error in result.index_errors)
    if result.refusals:
        lines.append("refusals:")
        for refusal in result.refusals:
            file_text = refusal.file or "(none)"
            lines.append(
                "  "
                f"refused={refusal.subject} "
                f"gate={refusal.gate} "
                f"file={file_text} "
                f"resolution={refusal.resolution}"
            )
    return "\n".join(lines) + "\n"


def repair_crash_leftovers(
    days: list[str], *, stream: str | None = None
) -> tuple[list[Refusal], int]:
    """Repair dangling prev pointers already justified by pruned history rows."""
    refusals: list[Refusal] = []
    repaired = 0
    for stream_name in _selected_streams(days, stream):
        stream_refusals, count = repair_stream_chain(stream_name, {}, dry_run=False)
        refusals.extend(stream_refusals)
        repaired += count
    return refusals, repaired


def repair_stream_chain(
    stream: str,
    deleted_markers: dict[tuple[str, str], dict[str, Any]],
    *,
    dry_run: bool = False,
) -> tuple[list[Refusal], int]:
    """Repair survivor prev pointers that point through pruned/missing segments."""
    pruned = _pruned_records_by_stream(stream)
    segments = _stream_segments(stream)
    existing = set(segments)
    refusals: list[Refusal] = []
    repaired = 0
    for key, seg_path in sorted(segments.items()):
        marker = read_segment_stream(seg_path)
        if not marker:
            continue
        prev_key = _marker_prev_key(marker)
        if prev_key is None or prev_key in existing:
            continue
        target, refusal = _nearest_surviving_ancestor(
            prev_key,
            stream,
            existing,
            deleted_markers,
            pruned,
        )
        if refusal is not None:
            refusals.append(refusal)
            continue
        if dry_run:
            repaired += 1
            continue
        write_segment_stream(
            seg_path,
            marker["stream"],
            target[0] if target else None,
            target[1] if target else None,
            marker["seq"],
        )
        repaired += 1
    return refusals, repaired


def _plan(days: list[str], *, stream: str | None) -> PruneResult:
    result = PruneResult(execute=False)
    for analyses in _same_start_sets(days, stream=stream):
        duplicate_groups, singleton_mismatches = _duplicate_groups(analyses)
        identity_errors = [analysis for analysis in analyses if analysis.identity_issue]
        if not duplicate_groups:
            if identity_errors and any(
                analysis.identity_issue is None for analysis in analyses
            ):
                for analysis in identity_errors:
                    result.refusals.append(_identity_refusal(analysis))
            continue
        if identity_errors:
            for analysis in identity_errors:
                result.refusals.append(_identity_refusal(analysis))
            continue
        mismatch_canonical = _mismatch_comparison_canonical(duplicate_groups)
        for mismatch in singleton_mismatches:
            result.refusals.append(
                Refusal(
                    mismatch.label,
                    "content-identity",
                    _first_identity_difference(
                        mismatch_canonical.identity, mismatch.identity
                    )
                    or "content identity",
                    "compared to canonical "
                    f"{mismatch_canonical.segment}; leave it in place; only "
                    "byte-identical same-start duplicates are pruned",
                )
            )
        for group_analyses in duplicate_groups:
            canonical = min(group_analyses, key=lambda item: item.segment)
            if canonical.marker_error:
                result.refusals.append(
                    Refusal(
                        canonical.label,
                        "chain-identity",
                        "stream.json",
                        canonical.marker_error,
                    )
                )
                continue
            owner_refusal = _observer_prefix_for_stream(canonical.stream)
            if isinstance(owner_refusal, Refusal):
                result.refusals.append(owner_refusal)
                continue
            safe_candidates: list[PruneCandidate] = []
            for analysis in sorted(group_analyses, key=lambda item: item.segment):
                if analysis is canonical:
                    continue
                if analysis.marker_error:
                    result.refusals.append(
                        Refusal(
                            analysis.label,
                            "chain-identity",
                            "stream.json",
                            analysis.marker_error,
                        )
                    )
                    continue
                if analysis.unknown_files:
                    result.refusals.append(
                        Refusal(
                            analysis.label,
                            "derived-output",
                            analysis.unknown_files[0],
                            "remove the file or add a valid ingest manifest proving it is content",
                        )
                    )
                    continue
                safe_candidates.append(
                    PruneCandidate(
                        analysis,
                        _is_last_physical_copy(canonical.identity, analysis.path),
                    )
                )
            if safe_candidates:
                result.groups.append(
                    PruneGroup(
                        canonical.day,
                        canonical.stream,
                        canonical.segment.split("_", 1)[0],
                        canonical,
                        safe_candidates,
                    )
                )
    return result


def _identity_refusal(analysis: SegmentAnalysis) -> Refusal:
    issue = analysis.identity_issue
    if issue is None:
        return Refusal(
            analysis.label,
            "canonical-heldness",
            INGEST_MANIFEST_NAME,
            "restore the canonical content file or terminal proof before pruning",
        )
    return Refusal(
        analysis.label,
        "canonical-heldness",
        issue.file,
        issue.resolution,
    )


def _execute_plan(result: PruneResult) -> None:
    affected_streams: set[str] = set()
    affected_days: set[str] = set()
    deleted_markers_by_stream: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for group in result.groups:
        prefix = _observer_prefix_for_stream(group.stream)
        if isinstance(prefix, Refusal):
            result.refusals.append(prefix)
            continue
        deleted_markers = deleted_markers_by_stream.setdefault(group.stream, {})
        for candidate in group.candidates:
            analysis = candidate.analysis
            marker = analysis.marker
            if marker is None:
                result.refusals.append(
                    Refusal(
                        analysis.label,
                        "chain-identity",
                        "stream.json",
                        "restore a readable stream.json marker before pruning",
                    )
                )
                break
            _append_pruned_once(
                prefix,
                analysis.day,
                analysis.stream,
                analysis.segment,
                group.canonical.segment,
            )
            try:
                shutil.rmtree(analysis.path)
            except OSError as exc:
                result.refusals.append(
                    Refusal(
                        analysis.label,
                        "delete",
                        analysis.segment,
                        "delete failed after the pruned history record was written: "
                        f"{exc}; fix the filesystem error and rerun prune",
                    )
                )
                break
            deleted_markers[(analysis.day, analysis.segment)] = marker
            result.deleted.append(candidate)
            affected_streams.add(analysis.stream)
            affected_days.add(analysis.day)
            rel = f"{analysis.day}/{analysis.stream}/{analysis.segment}"
            deleted = delete_segment_index_rows(get_journal(), rel)
            if deleted.get("error"):
                result.index_errors.append(f"{rel}: {deleted['error']}")
    for stream in sorted(affected_streams):
        refusals, repaired = repair_stream_chain(
            stream, deleted_markers_by_stream.get(stream, {}), dry_run=False
        )
        result.refusals.extend(refusals)
        result.chain_repaired += repaired
        if refusals:
            continue
        _repair_stream_state(stream)
    for day in sorted(affected_days):
        touch_stream_health_marker(day)


def _same_start_sets(
    days: list[str], *, stream: str | None
) -> list[list[SegmentAnalysis]]:
    sets: dict[tuple[str, str, str], list[SegmentAnalysis]] = {}
    for day in days:
        for stream_name, segment, path in iter_segments(day):
            if stream and stream_name != stream:
                continue
            if "_" not in segment:
                continue
            start = segment.split("_", 1)[0]
            sets.setdefault((day, stream_name, start), []).append(
                _analyze_segment(day, stream_name, segment, path)
            )
    return [items for items in sets.values() if len(items) > 1]


def _duplicate_groups(
    analyses: list[SegmentAnalysis],
) -> tuple[list[list[SegmentAnalysis]], list[SegmentAnalysis]]:
    by_key: dict[tuple, list[SegmentAnalysis]] = {}
    for analysis in analyses:
        if analysis.identity_issue:
            continue
        by_key.setdefault(analysis.identity_key, []).append(analysis)
    duplicate_groups = [items for items in by_key.values() if len(items) > 1]
    duplicate_keys = {
        content_identity_key(items[0].identity) for items in duplicate_groups
    }
    singleton_mismatches = [
        items[0]
        for key, items in by_key.items()
        if key not in duplicate_keys and duplicate_groups
    ]
    return duplicate_groups, singleton_mismatches


def _mismatch_comparison_canonical(
    duplicate_groups: list[list[SegmentAnalysis]],
) -> SegmentAnalysis:
    group = min(
        duplicate_groups,
        key=lambda items: (-len(items), min(item.segment for item in items)),
    )
    return min(group, key=lambda item: item.segment)


def _analyze_segment(
    day: str, stream: str, segment: str, path: Path
) -> SegmentAnalysis:
    identity, identity_issue = content_identity_from_segment(path)
    marker = read_segment_stream(path)
    marker_error = None
    if marker is None:
        marker_error = "restore a readable stream.json marker before pruning"
    elif marker.get("stream") != stream:
        marker_error = "rewrite stream.json so its stream matches the segment directory before pruning"
    unknown_files = []
    if not identity_issue:
        unknown_files = _unknown_files(path, identity)
    return SegmentAnalysis(
        day,
        stream,
        segment,
        path,
        marker,
        identity,
        identity_issue,
        marker_error,
        unknown_files,
    )


def _unknown_files(
    segment_dir: Path, identity: dict[str, ContentIdentityFile]
) -> list[str]:
    unknown = []
    for path in sorted(segment_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(segment_dir).as_posix()
        if rel in RESERVED_SEGMENT_FILENAMES:
            continue
        if rel in identity:
            continue
        if is_structural_derived_file(rel, identity):
            continue
        unknown.append(rel)
    return unknown


def _is_last_physical_copy(
    canonical_identity: dict[str, ContentIdentityFile], candidate_dir: Path
) -> bool:
    for item in canonical_identity.values():
        if not item.is_terminal_proof_only:
            continue
        if (candidate_dir / item.name).is_file():
            return True
    return False


def _observer_prefix_for_stream(stream: str) -> str | Refusal:
    locked = [
        observer
        for observer in list_observers()
        if not observer.get("revoked", False) and observer.get("stream") == stream
    ]
    if len(locked) == 1:
        return observer_filename_prefix(locked[0])
    if len(locked) > 1:
        return Refusal(
            stream,
            "observer-attribution",
            "apps/observer/observers",
            "multiple active observers own this locked stream; reconcile observers first",
        )
    prefixes = set()
    for observer in list_observers():
        if observer.get("revoked", False):
            continue
        prefix = observer_filename_prefix(observer)
        hist_dir = get_hist_dir(prefix, ensure_exists=False)
        if not hist_dir.exists():
            continue
        for hist_path in hist_dir.glob("*.jsonl"):
            for record in load_history(prefix, hist_path.stem):
                if record.get("stream") == stream:
                    prefixes.add(prefix)
                    break
    if len(prefixes) == 1:
        return next(iter(prefixes))
    if not prefixes:
        return Refusal(
            stream,
            "observer-attribution",
            "apps/observer/observers",
            "no observer owns or references this stream; create an unambiguous observer history first",
        )
    return Refusal(
        stream,
        "observer-attribution",
        "apps/observer/observers/*/hist",
        "multiple observer histories reference this stream; reconcile ownership first",
    )


def _append_pruned_once(
    prefix: str, day: str, stream: str, segment: str, duplicate_of: str
) -> None:
    records = load_history(prefix, day)
    latest = None
    for record in records:
        if record.get("stream") == stream and record.get("segment") == segment:
            latest = record
    if latest and latest.get("type") == "pruned":
        return
    append_history_record(
        prefix,
        day,
        {
            "type": "pruned",
            "ts": now_ms(),
            "segment": segment,
            "stream": stream,
            "duplicate_of": duplicate_of,
        },
    )


def _selected_streams(days: list[str], stream: str | None) -> set[str]:
    streams = set()
    for day in days:
        for stream_name, _segment, _path in iter_segments(day):
            if stream is None or stream_name == stream:
                streams.add(stream_name)
    if stream is not None:
        streams.add(stream)
    return streams


def _stream_segments(stream: str) -> dict[tuple[str, str], Path]:
    segments = {}
    for day in day_dirs().keys():
        for stream_name, segment, path in iter_segments(day):
            if stream_name == stream:
                segments[(day, segment)] = path
    return segments


def _marker_prev_key(marker: dict[str, Any]) -> tuple[str, str] | None:
    prev_day = marker.get("prev_day")
    prev_segment = marker.get("prev_segment")
    if isinstance(prev_day, str) and isinstance(prev_segment, str):
        return prev_day, prev_segment
    return None


def _nearest_surviving_ancestor(
    start: tuple[str, str],
    stream: str,
    existing: set[tuple[str, str]],
    deleted_markers: dict[tuple[str, str], dict[str, Any]],
    pruned: dict[tuple[str, str], dict[str, Any]],
) -> tuple[tuple[str, str] | None, Refusal | None]:
    current: tuple[str, str] | None = start
    seen: set[tuple[str, str]] = set()
    while current is not None:
        if current in existing:
            return current, None
        if current in seen:
            return None, Refusal(
                f"{current[0]}/{stream}/{current[1]}",
                "chain-repair",
                "stream.json",
                "break the predecessor cycle before pruning",
            )
        seen.add(current)
        marker = deleted_markers.get(current)
        if marker:
            current = _marker_prev_key(marker)
            continue
        record = pruned.get(current)
        if record:
            duplicate_of = record.get("duplicate_of")
            if isinstance(duplicate_of, str) and duplicate_of:
                current = (current[0], duplicate_of)
                continue
            current = None
            continue
        return None, Refusal(
            f"{current[0]}/{stream}/{current[1]}",
            "chain-repair",
            "stream.json",
            "restore the missing predecessor or append a valid pruned history record",
        )
    return None, None


def _pruned_records_by_stream(stream: str) -> dict[tuple[str, str], dict[str, Any]]:
    records_by_segment: dict[tuple[str, str], dict[str, Any]] = {}
    for observer in list_observers():
        prefix = observer_filename_prefix(observer)
        hist_dir = get_hist_dir(prefix, ensure_exists=False)
        if not hist_dir.exists():
            continue
        for hist_path in hist_dir.glob("*.jsonl"):
            day = hist_path.stem
            for record in load_history(prefix, day):
                if (
                    record.get("type") == "pruned"
                    and record.get("stream") == stream
                    and isinstance(record.get("segment"), str)
                ):
                    records_by_segment[(day, record["segment"])] = record
    return records_by_segment


def _repair_stream_state(stream: str) -> None:
    state = get_stream_state(stream)
    current_tail: tuple[str, str] | None = None
    if state:
        last_day = state.get("last_day")
        last_segment = state.get("last_segment")
        if isinstance(last_day, str) and isinstance(last_segment, str):
            current_tail = (last_day, last_segment)
    segments = _stream_segments(stream)
    if current_tail in segments:
        return
    max_seq = 0
    tail: tuple[str, str] | None = None
    for key, path in segments.items():
        marker = read_segment_stream(path)
        if not marker:
            continue
        seq = marker.get("seq", 0)
        if isinstance(seq, bool) or not isinstance(seq, int):
            continue
        if seq >= max_seq:
            max_seq = seq
            tail = key
    repair_stream_state_tail(
        stream,
        tail[0] if tail else None,
        tail[1] if tail else None,
        max_seq=max_seq,
    )


def _first_identity_difference(
    canonical: dict[str, ContentIdentityFile],
    candidate: dict[str, ContentIdentityFile],
) -> str | None:
    canonical_names = set(canonical)
    candidate_names = set(candidate)
    extra = sorted(candidate_names - canonical_names)
    if extra:
        return extra[0]
    missing = sorted(canonical_names - candidate_names)
    if missing:
        return missing[0]
    for name in sorted(canonical_names):
        left = canonical[name]
        right = candidate[name]
        if left.sha256 != right.sha256 or left.size != right.size:
            return name
    return None


def _validate_day(day: str) -> None:
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError:
        raise ValueError(f"invalid day: {day}") from None


def result_exit_code(result: PruneResult) -> int:
    """Return the observer prune CLI exit code for a result."""
    return 2 if result.refusals else 0

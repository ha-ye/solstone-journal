# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Media offload pass for verified raw journal media."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from solstone.think.backup.engine import (
    ArchiveCheckResult,
    BackupResult,
    check_archive_snapshot_files,
    request_verification_now,
    run_archive_backup,
)
from solstone.think.backup.hosted import load_hosted_binding
from solstone.think.backup.state import (
    get_backup_config,
    get_destination,
    get_keys,
    record_offload_result,
)
from solstone.think.offload_ledger import OffloadFile, append_offload_event
from solstone.think.offload_measurement import (
    device_free_bytes,
    measure_raw_media_usage,
)
from solstone.think.pruning_audit import AuditOutcome, write_prune_audit
from solstone.think.retention import get_raw_media_files, resolve_segment_gate
from solstone.think.utils import day_dirs, get_journal, iter_segments

logger = logging.getLogger(__name__)

OFFLOAD_STALL_REASONS = frozenset(
    {
        "backup_not_ready",
        "backup_failing",
        "verification_missing",
        "verification_overdue",
        "verification_failed",
        "locked",
        "archive_failed",
        "confirm_failed",
        "confirm_tool_failed",
        "unexpected_error",
    }
)
VERIFICATION_INTEGRITY_REASONS = {"integrity_failed", "auth_failed", "repo_missing"}
VERIFICATION_MAX_AGE_SECONDS = 14 * 86400
OFFLOAD_MAX_RUNTIME = "7h"


@dataclass(frozen=True)
class OffloadSegmentDetail:
    day: str
    stream: str
    segment: str
    files: int
    bytes: int


@dataclass(frozen=True)
class OffloadResult:
    status: str
    reason: str | None
    files_offloaded: int
    bytes_offloaded: int
    ran_out_of_media: bool
    dry_run: bool = False
    details: tuple[OffloadSegmentDetail, ...] = ()


@dataclass
class _Counters:
    files_offloaded: int = 0
    bytes_offloaded: int = 0
    details: list[OffloadSegmentDetail] = field(default_factory=list)


@dataclass(frozen=True)
class _PreparedRawFile:
    path: Path
    ledger_file: OffloadFile
    audit_file: dict[str, Any]


def run_offload(dry_run: bool = False) -> OffloadResult:
    counters = _Counters()
    # This catches Python exceptions, not scheduler SIGKILL at max_runtime; a
    # kill can leave the prior ok visible until the next daily run.
    try:
        return _run_offload(dry_run=dry_run, counters=counters)
    except Exception:
        logger.exception("media offload failed unexpectedly")
        return _stalled_result(
            "unexpected_error",
            dry_run=dry_run,
            counters=counters,
            ran_out_of_media=False,
        )


def _run_offload(*, dry_run: bool, counters: _Counters) -> OffloadResult:
    config = get_backup_config()
    offload_config = config["offload"]
    if offload_config.get("enabled") is not True:
        return OffloadResult(
            status="skipped",
            reason=None,
            files_offloaded=0,
            bytes_offloaded=0,
            ran_out_of_media=False,
            dry_run=dry_run,
        )

    precondition_reason = _precondition_stall_reason(config)
    if precondition_reason is not None:
        if (
            precondition_reason in {"verification_missing", "verification_overdue"}
            and not dry_run
        ):
            request_verification_now()
        return _stalled_result(
            precondition_reason,
            dry_run=dry_run,
            counters=counters,
            ran_out_of_media=False,
        )

    usage = measure_raw_media_usage()
    start_raw_bytes = usage.total_bytes
    start_free_bytes = device_free_bytes()
    budget_bytes = offload_config.get("budget_bytes")
    floor_bytes = offload_config.get("floor_bytes")

    if _bounds_satisfied(
        start_raw_bytes=start_raw_bytes,
        start_free_bytes=start_free_bytes,
        freed_bytes=0,
        budget_bytes=budget_bytes,
        floor_bytes=floor_bytes,
    ):
        return _ok_result(
            dry_run=dry_run,
            counters=counters,
            ran_out_of_media=False,
        )

    journal_path = Path(get_journal())
    for day in sorted(day_dirs().keys()):
        for stream, segment, segment_path in iter_segments(day):
            raw_files = sorted(
                get_raw_media_files(segment_path), key=lambda path: path.name
            )
            if not raw_files:
                continue

            gate = resolve_segment_gate(segment_path)
            if gate.verdict in {"failed", "incomplete"}:
                continue
            if gate.verdict != "eligible":
                raise RuntimeError(f"unexpected retention gate verdict: {gate.verdict}")

            if dry_run:
                segment_files, segment_bytes = _stat_raw_files(raw_files)
                if segment_files == 0:
                    continue
                counters.details.append(
                    OffloadSegmentDetail(
                        day=day,
                        stream=stream,
                        segment=segment,
                        files=segment_files,
                        bytes=segment_bytes,
                    )
                )
            else:
                stall = _offload_segment(
                    journal_path=journal_path,
                    day=day,
                    stream=stream,
                    segment=segment,
                    raw_files=raw_files,
                    completion_files=gate.completion_files,
                    counters=counters,
                )
                if stall is not None:
                    return stall

            if _bounds_satisfied(
                start_raw_bytes=start_raw_bytes,
                start_free_bytes=start_free_bytes,
                freed_bytes=_effective_freed_bytes(counters, dry_run=dry_run),
                budget_bytes=budget_bytes,
                floor_bytes=floor_bytes,
            ):
                return _ok_result(
                    dry_run=dry_run,
                    counters=counters,
                    ran_out_of_media=False,
                )

    return _ok_result(
        dry_run=dry_run,
        counters=counters,
        ran_out_of_media=True,
    )


def _precondition_stall_reason(config: dict[str, Any]) -> str | None:
    if config.get("enabled") is not True:
        return "backup_not_ready"
    if get_keys() is None:
        return "backup_not_ready"
    if config.get("mode") == "operated":
        if load_hosted_binding() is None:
            return "backup_not_ready"
    elif get_destination() is None:
        return "backup_not_ready"

    # Exit-3 whole-journal backups can leave a partial snapshot id; offload
    # requires the latest full backup to be ok, so this self-clearing stall is honest.
    last_backup = config["last_backup"]
    backup_status = last_backup.get("status")
    snapshot_id = last_backup.get("snapshot_id")
    if backup_status is None:
        return "backup_not_ready"
    if backup_status != "ok":
        return "backup_failing"
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return "backup_not_ready"

    last_verification = config["last_verification"]
    verification_status = last_verification.get("status")
    verification_reason = last_verification.get("reason")
    if (
        verification_status == "error"
        and verification_reason in VERIFICATION_INTEGRITY_REASONS
    ):
        return "verification_failed"

    last_ok_time = last_verification.get("last_ok_time")
    if type(last_ok_time) is not int:
        return "verification_missing"
    if int(time.time()) - last_ok_time > VERIFICATION_MAX_AGE_SECONDS:
        return "verification_overdue"
    return None


def _offload_segment(
    *,
    journal_path: Path,
    day: str,
    stream: str,
    segment: str,
    raw_files: list[Path],
    completion_files: list[Path],
    counters: _Counters,
) -> OffloadResult | None:
    prepared = _prepare_raw_files(raw_files)
    if not prepared:
        return None

    archive = run_archive_backup([file.path for file in prepared])
    stall_reason = _archive_stall_reason(archive)
    if stall_reason is not None:
        return _stalled_result(
            stall_reason,
            dry_run=False,
            counters=counters,
            ran_out_of_media=False,
        )

    snapshot_id = archive.snapshot_id
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return _stalled_result(
            "archive_failed",
            dry_run=False,
            counters=counters,
            ran_out_of_media=False,
        )

    confirm = check_archive_snapshot_files(
        snapshot_id,
        {file.path: file.ledger_file.bytes for file in prepared},
    )
    stall_reason = _confirm_stall_reason(confirm)
    if stall_reason is not None:
        return _stalled_result(
            stall_reason,
            dry_run=False,
            counters=counters,
            ran_out_of_media=False,
        )

    append_offload_event(
        day=day,
        stream=stream,
        segment=segment,
        snapshot_id=snapshot_id,
        files=[file.ledger_file for file in prepared],
    )

    segment_bytes = 0
    segment_files = 0
    for file in prepared:
        file.path.unlink()
        counters.files_offloaded += 1
        counters.bytes_offloaded += file.ledger_file.bytes
        segment_files += 1
        segment_bytes += file.ledger_file.bytes

    counters.details.append(
        OffloadSegmentDetail(
            day=day,
            stream=stream,
            segment=segment,
            files=segment_files,
            bytes=segment_bytes,
        )
    )
    audit_files = [file.audit_file for file in prepared]
    audit = _write_segment_offload_audit(
        journal_path,
        day,
        stream,
        segment,
        audit_files,
        segment_bytes,
        _processed_at(completion_files),
    )
    if audit.partial_error:
        logger.warning(
            "media offload audit partially failed day=%s stream=%s segment=%s",
            day,
            stream,
            segment,
        )
    return None


def _archive_stall_reason(result: BackupResult) -> str | None:
    if result.status == "ok":
        return None
    if result.status == "skipped":
        return "backup_not_ready"
    if result.error_reason == "locked":
        return "locked"
    return "archive_failed"


def _confirm_stall_reason(result: ArchiveCheckResult) -> str | None:
    if result.status == "skipped":
        return "backup_not_ready"
    if result.status == "error":
        if result.error_reason == "locked":
            return "locked"
        return "confirm_tool_failed"
    if result.status != "ok":
        raise RuntimeError(f"unexpected archive check status: {result.status}")
    if result.verdicts is None:
        return "confirm_tool_failed"
    if any(not verdict.confirmed for verdict in result.verdicts):
        return "confirm_failed"
    return None


def _prepare_raw_files(raw_files: list[Path]) -> list[_PreparedRawFile]:
    prepared: list[_PreparedRawFile] = []
    for f in raw_files:
        size = f.stat().st_size
        digest = hashlib.sha256()
        with open(f, "rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        hex_digest = digest.hexdigest()
        prepared.append(
            _PreparedRawFile(
                path=f,
                ledger_file=OffloadFile(name=f.name, bytes=size, sha256=hex_digest),
                audit_file={"name": f.name, "bytes": size, "hash": hex_digest},
            )
        )
    return prepared


def _stat_raw_files(raw_files: list[Path]) -> tuple[int, int]:
    files = 0
    bytes_total = 0
    for path in raw_files:
        try:
            bytes_total += path.stat().st_size
        except FileNotFoundError:
            continue
        files += 1
    return files, bytes_total


def _bounds_satisfied(
    *,
    start_raw_bytes: int,
    start_free_bytes: int,
    freed_bytes: int,
    budget_bytes: Any,
    floor_bytes: Any,
) -> bool:
    if type(budget_bytes) is int and start_raw_bytes - freed_bytes > budget_bytes:
        return False
    if type(floor_bytes) is int and start_free_bytes + freed_bytes < floor_bytes:
        return False
    return True


def _effective_freed_bytes(counters: _Counters, *, dry_run: bool) -> int:
    if dry_run:
        return sum(detail.bytes for detail in counters.details)
    return counters.bytes_offloaded


def _stalled_result(
    reason: str,
    *,
    dry_run: bool,
    counters: _Counters,
    ran_out_of_media: bool,
) -> OffloadResult:
    if reason not in OFFLOAD_STALL_REASONS:
        raise AssertionError(f"unknown media offload stall reason: {reason}")
    result = OffloadResult(
        status="stalled",
        reason=reason,
        files_offloaded=counters.files_offloaded,
        bytes_offloaded=counters.bytes_offloaded,
        ran_out_of_media=ran_out_of_media,
        dry_run=dry_run,
        details=tuple(counters.details),
    )
    if not dry_run:
        record_offload_result(
            status="stalled",
            time=int(time.time()),
            reason=reason,
            files_offloaded=counters.files_offloaded,
            bytes_offloaded=counters.bytes_offloaded,
            ran_out_of_media=ran_out_of_media,
        )
    return result


def _ok_result(
    *,
    dry_run: bool,
    counters: _Counters,
    ran_out_of_media: bool,
) -> OffloadResult:
    result = OffloadResult(
        status="ok",
        reason=None,
        files_offloaded=counters.files_offloaded,
        bytes_offloaded=counters.bytes_offloaded,
        ran_out_of_media=ran_out_of_media,
        dry_run=dry_run,
        details=tuple(counters.details),
    )
    if not dry_run:
        record_offload_result(
            status="ok",
            time=int(time.time()),
            reason=None,
            files_offloaded=counters.files_offloaded,
            bytes_offloaded=counters.bytes_offloaded,
            ran_out_of_media=ran_out_of_media,
        )
    return result


def _processed_at(completion_files: list[Path]) -> str | None:
    mtimes: list[float] = []
    for path in completion_files:
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes)).isoformat()


def _write_segment_offload_audit(
    journal_path: Path,
    day: str,
    stream: str,
    segment: str,
    files: list[dict[str, Any]],
    bytes_freed: int,
    processed_at: str | None,
) -> AuditOutcome:
    run_record = {
        "timestamp": datetime.now().isoformat(),
        "kind": "raw_media_offload",
        "dry_run": False,
        "days": None,
        "day": day,
        "stream": stream,
        "segment": segment,
        "files": files,
        "bytes_freed": bytes_freed,
        "processed_at": processed_at,
    }
    message = (
        f"raw-media offload: pruned {len(files)} raw media file(s) "
        f"({bytes_freed} bytes) from segment {stream}/{segment}"
    )
    return write_prune_audit(
        journal_path,
        kind="raw_media_offload",
        run_record=run_record,
        per_day_messages={day: message},
    )


__all__ = [
    "OFFLOAD_MAX_RUNTIME",
    "OFFLOAD_STALL_REASONS",
    "OffloadResult",
    "OffloadSegmentDetail",
    "VERIFICATION_INTEGRITY_REASONS",
    "VERIFICATION_MAX_AGE_SECONDS",
    "run_offload",
]

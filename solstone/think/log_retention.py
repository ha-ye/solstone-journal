# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Journal-wide operational log and cache retention pruning."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from solstone.think.journal_io import atomic_replace
from solstone.think.pruning_audit import write_prune_audit
from solstone.think.retention import _human_bytes
from solstone.think.utils import get_config, get_journal

PRUNE_LOGS_MAX_RUNTIME = "30m"

DELETE_FAILED_HINT = (
    "check file ownership/permissions; the service user may not own this file"
)

CLASS_NAMES = (
    "chronicle_health_logs",
    "talent_run_logs",
    "talent_day_index",
    "cogitate_history_cache",
    "tokens",
    "awareness_logs",
    "config_actions",
    "facet_logs",
    "observer_history",
)


@dataclass
class LogRetentionConfig:
    """Operational log/cache retention config."""

    enabled: bool = True
    days: int = 30


@dataclass
class PruneResult:
    """Result of a journal log/cache prune run."""

    enabled: bool
    dry_run: bool
    days: int
    cutoff_day: str
    by_class: dict[str, dict]
    by_day: dict[str, dict]
    files_deleted: int
    dirs_deleted: int
    bytes_freed: int
    errors: list[dict]
    audit_written: bool
    partial_error: bool
    root_task_log: dict = field(default_factory=lambda: _empty_root_task_log_stats())

    def to_record(self) -> dict:
        """Return the global audit record for this prune result."""
        return {
            "timestamp": datetime.now().isoformat(),
            "kind": "journal_logs",
            "dry_run": self.dry_run,
            "enabled": self.enabled,
            "days": self.days,
            "cutoff_day": self.cutoff_day,
            "by_class": self.by_class,
            "by_day": self.by_day,
            "totals": {
                "files_deleted": self.files_deleted,
                "dirs_deleted": self.dirs_deleted,
                "bytes_freed": self.bytes_freed,
                "skipped": sum(
                    stats.get("skipped", 0) for stats in self.by_class.values()
                ),
            },
            "root_task_log": self.root_task_log,
            "errors": self.errors,
        }

    def day_messages(self) -> dict[str, str]:
        """Return one concise task-log line per affected day."""
        if (
            not self.enabled
            or self.dry_run
            or (self.files_deleted + self.dirs_deleted) == 0
        ):
            return {}

        messages: dict[str, str] = {}
        for day, stats in sorted(self.by_day.items()):
            files = int(stats.get("files_deleted", 0))
            dirs = int(stats.get("dirs_deleted", 0))
            parts = []
            if files:
                parts.append(f"{files} operational-log file(s)")
            if dirs:
                parts.append(f"{dirs} cache dir(s)")
            if not parts:
                continue
            size = _human_bytes(int(stats.get("bytes_freed", 0)))
            messages[day] = (
                f"log-retention: pruned {' and '.join(parts)} ({size}) for this day"
            )
        return messages


def load_log_retention_config() -> LogRetentionConfig:
    """Load journal log/cache retention config with per-field defaults."""
    config = get_config()
    retention = config.get("retention") or {}
    journal_logs = retention.get("journal_logs") or {}

    enabled = journal_logs.get("enabled", True)
    days = journal_logs.get("days", 30)
    if not isinstance(enabled, bool):
        raise ValueError("retention.journal_logs.enabled must be a boolean")
    if isinstance(days, bool):
        raise ValueError("retention.journal_logs.days must be a positive integer")
    try:
        parsed_days = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "retention.journal_logs.days must be a positive integer"
        ) from exc
    if parsed_days < 1:
        raise ValueError("retention.journal_logs.days must be a positive integer")
    return LogRetentionConfig(enabled=enabled, days=parsed_days)


def prune(
    *,
    days: int | None = None,
    dry_run: bool = False,
    config: LogRetentionConfig | None = None,
) -> PruneResult:
    """Prune eligible operational logs and caches from the journal."""
    effective_config = config or load_log_retention_config()
    effective_days = _effective_days(
        days if days is not None else effective_config.days
    )
    today = datetime.now().date()
    cutoff = today - timedelta(days=effective_days)
    result = _empty_result(
        enabled=effective_config.enabled,
        dry_run=dry_run,
        days=effective_days,
        cutoff_day=_format_day(cutoff),
    )

    if not effective_config.enabled:
        return result

    journal_path = Path(get_journal())
    _scan_chronicle_health_logs(journal_path, cutoff, result, dry_run=dry_run)
    _scan_talent_run_logs(journal_path, cutoff, result, dry_run=dry_run)
    _scan_talent_day_index(journal_path, cutoff, result, dry_run=dry_run)
    _scan_cogitate_history_cache(journal_path, cutoff, result, dry_run=dry_run)
    _scan_dated_files(
        journal_path,
        cutoff,
        result,
        dry_run=dry_run,
        class_name="tokens",
        base=journal_path / "tokens",
        pattern="*.jsonl",
    )
    _scan_dated_files(
        journal_path,
        cutoff,
        result,
        dry_run=dry_run,
        class_name="awareness_logs",
        base=journal_path / "awareness",
        pattern="*.jsonl",
    )
    _scan_dated_files(
        journal_path,
        cutoff,
        result,
        dry_run=dry_run,
        class_name="config_actions",
        base=journal_path / "config" / "actions",
        pattern="*.jsonl",
    )
    _scan_facet_logs(journal_path, cutoff, result, dry_run=dry_run)
    _scan_observer_history(journal_path, cutoff, result, dry_run=dry_run)
    _compact_root_task_log(journal_path, cutoff, result, dry_run=dry_run)

    if not dry_run and _has_audit_work(result):
        outcome = write_prune_audit(
            journal_path,
            kind="journal_logs",
            run_record=result.to_record(),
            per_day_messages=result.day_messages(),
        )
        for day, error in outcome.per_day_failures.items():
            _add_error(
                result,
                "journal_logs",
                path=journal_path / "chronicle" / day / "task_log.txt",
                day=day,
                reason="task_log_append_failed",
                message=f"failed to append pruning task log for {day}: {error}",
                hint=None,
            )
        if outcome.global_record_error is not None:
            _add_error(
                result,
                "journal_logs",
                path=journal_path / "health" / "pruning-runs",
                day=None,
                reason="global_record_failed",
                message=f"failed to append pruning run record: {outcome.global_record_error}",
                hint=None,
            )
        result.audit_written = outcome.global_record_written
        result.partial_error = outcome.partial_error or result.partial_error

    return result


def _effective_days(days: int) -> int:
    if isinstance(days, bool):
        raise ValueError("days must be a positive integer")
    try:
        parsed = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError("days must be a positive integer") from exc
    if parsed < 1:
        raise ValueError("days must be a positive integer")
    return parsed


def _empty_result(
    *,
    enabled: bool,
    dry_run: bool,
    days: int,
    cutoff_day: str,
) -> PruneResult:
    return PruneResult(
        enabled=enabled,
        dry_run=dry_run,
        days=days,
        cutoff_day=cutoff_day,
        by_class={name: _empty_class_stats() for name in CLASS_NAMES},
        by_day={},
        files_deleted=0,
        dirs_deleted=0,
        bytes_freed=0,
        errors=[],
        audit_written=False,
        partial_error=False,
        root_task_log=_empty_root_task_log_stats(),
    )


def _empty_class_stats() -> dict:
    return {
        "files_deleted": 0,
        "bytes_freed": 0,
        "dirs_deleted": 0,
        "skipped": 0,
        "errors": [],
    }


def _empty_root_task_log_stats() -> dict:
    return {
        "exists": False,
        "lines_total": 0,
        "lines_kept": 0,
        "lines_removed": 0,
        "unparseable_lines_kept": 0,
        "bytes_freed": 0,
        "rewritten": False,
        "errors": [],
    }


def _has_root_task_log_work(result: PruneResult) -> bool:
    return int(result.root_task_log.get("lines_removed", 0)) > 0 and bool(
        result.root_task_log.get("rewritten", False)
    )


def _has_audit_work(result: PruneResult) -> bool:
    return (result.files_deleted + result.dirs_deleted) > 0 or _has_root_task_log_work(
        result
    )


def _scan_chronicle_health_logs(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    chronicle = journal_path / "chronicle"
    if not chronicle.is_dir():
        return
    for day_dir in chronicle.iterdir():
        if not day_dir.is_dir() or day_dir.is_symlink():
            continue
        health_dir = day_dir / "health"
        if not health_dir.is_dir():
            continue
        targets = [*health_dir.glob("*.log"), *health_dir.glob("*.jsonl")]
        for target in targets:
            day = _parse_yyyymmdd(
                day_dir.name,
                class_name="chronicle_health_logs",
                path=target,
                result=result,
            )
            if day is not None and day < cutoff:
                _delete_target(
                    journal_path,
                    result,
                    "chronicle_health_logs",
                    target,
                    _format_day(day),
                    target_kind="file",
                    dry_run=dry_run,
                )


def _scan_talent_run_logs(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    talents = journal_path / "talents"
    if not talents.is_dir():
        return
    for talent_dir in talents.iterdir():
        if not talent_dir.is_dir() or talent_dir.is_symlink():
            continue
        for target in talent_dir.glob("*.jsonl"):
            if target.name.endswith("_active.jsonl"):
                _mark_skipped(result, "talent_run_logs")
                continue
            day = _parse_epoch_ms(target.stem, path=target, result=result)
            if day is not None and day < cutoff:
                _delete_target(
                    journal_path,
                    result,
                    "talent_run_logs",
                    target,
                    _format_day(day),
                    target_kind="file",
                    dry_run=dry_run,
                )


def _scan_talent_day_index(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    talents = journal_path / "talents"
    if not talents.is_dir():
        return
    for target in talents.glob("*.jsonl"):
        day = _parse_yyyymmdd(
            target.stem,
            class_name="talent_day_index",
            path=target,
            result=result,
        )
        if day is not None and day < cutoff:
            _delete_target(
                journal_path,
                result,
                "talent_day_index",
                target,
                _format_day(day),
                target_kind="file",
                dry_run=dry_run,
            )


def _scan_cogitate_history_cache(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    cache_root = journal_path / ".cache" / "cogitate-history"
    if not cache_root.is_dir():
        return
    for target in cache_root.iterdir():
        if not target.is_symlink() and not target.is_dir():
            continue
        try:
            mtime_day = date.fromtimestamp(target.lstat().st_mtime)
        except FileNotFoundError:
            _record_skip_error(
                journal_path,
                result,
                "cogitate_history_cache",
                target,
                None,
                "missing_file_race",
                "cache session disappeared before pruning could inspect it",
                None,
            )
            continue
        except OSError as exc:
            _record_skip_error(
                journal_path,
                result,
                "cogitate_history_cache",
                target,
                None,
                "delete_failed",
                f"failed to inspect cache session before pruning: {exc}",
                DELETE_FAILED_HINT,
            )
            continue
        if mtime_day < cutoff:
            _delete_target(
                journal_path,
                result,
                "cogitate_history_cache",
                target,
                _format_day(mtime_day),
                target_kind="dir",
                dry_run=dry_run,
            )


def _scan_dated_files(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
    class_name: str,
    base: Path,
    pattern: str,
) -> None:
    if not base.is_dir():
        return
    for target in base.glob(pattern):
        day = _parse_yyyymmdd(
            target.stem,
            class_name=class_name,
            path=target,
            result=result,
        )
        if day is not None and day < cutoff:
            _delete_target(
                journal_path,
                result,
                class_name,
                target,
                _format_day(day),
                target_kind="file",
                dry_run=dry_run,
            )


def _scan_facet_logs(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    facets = journal_path / "facets"
    if not facets.is_dir():
        return
    for facet_dir in facets.iterdir():
        if not facet_dir.is_dir() or facet_dir.is_symlink():
            continue
        _scan_dated_files(
            journal_path,
            cutoff,
            result,
            dry_run=dry_run,
            class_name="facet_logs",
            base=facet_dir / "logs",
            pattern="*.jsonl",
        )


def _scan_observer_history(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    observers = journal_path / "apps" / "observer" / "observers"
    if not observers.is_dir():
        return
    for observer_dir in observers.iterdir():
        if not observer_dir.is_dir() or observer_dir.is_symlink():
            continue
        _scan_dated_files(
            journal_path,
            cutoff,
            result,
            dry_run=dry_run,
            class_name="observer_history",
            base=observer_dir / "hist",
            pattern="*.jsonl",
        )


def _compact_root_task_log(
    journal_path: Path,
    cutoff: date,
    result: PruneResult,
    *,
    dry_run: bool,
) -> None:
    path = journal_path / "task_log.txt"
    stats = result.root_task_log
    stats["exists"] = path.exists()
    if not stats["exists"]:
        return

    try:
        original = path.read_bytes()
    except OSError as exc:
        _add_root_task_log_error(
            result,
            path,
            "root_task_log_read_failed",
            f"failed to read root task log before compaction: {exc}",
            DELETE_FAILED_HINT,
            journal_path=journal_path,
        )
        return

    lines = original.splitlines(keepends=True)
    kept: list[bytes] = []
    unparseable = 0

    for line in lines:
        entry_day = _parse_root_task_log_day(line)
        if entry_day is None:
            unparseable += 1
            kept.append(line)
            continue
        if entry_day < cutoff:
            continue
        kept.append(line)

    rewritten = b"".join(kept)
    stats.update(
        {
            "lines_total": len(lines),
            "lines_kept": len(kept),
            "lines_removed": len(lines) - len(kept),
            "unparseable_lines_kept": unparseable,
            "bytes_freed": len(original) - len(rewritten),
        }
    )
    result.bytes_freed += int(stats["bytes_freed"])

    if dry_run or stats["lines_removed"] == 0:
        return

    try:
        _atomic_replace_file(path, rewritten)
    except OSError as exc:
        result.bytes_freed -= int(stats["bytes_freed"])
        stats["bytes_freed"] = 0
        _add_root_task_log_error(
            result,
            path,
            "root_task_log_rewrite_failed",
            f"failed to rewrite root task log during compaction: {exc}",
            DELETE_FAILED_HINT,
            journal_path=journal_path,
        )
        return

    stats["rewritten"] = True


def _parse_root_task_log_day(line: bytes) -> date | None:
    epoch_raw, separator, _rest = line.partition(b"\t")
    if not separator or not epoch_raw:
        return None
    try:
        epoch = int(epoch_raw)
    except ValueError:
        return None
    try:
        return datetime.fromtimestamp(epoch).date()
    except (OSError, OverflowError, ValueError):
        return None


def _atomic_replace_file(path: Path, data: bytes) -> None:
    original_mode = path.stat().st_mode & 0o777
    atomic_replace(path, data, mode=original_mode)


def _add_root_task_log_error(
    result: PruneResult,
    path: Path,
    reason: str,
    message: str,
    hint: str | None,
    *,
    journal_path: Path,
) -> None:
    result.root_task_log["errors"].append(message)
    _add_error(
        result,
        "root_task_log",
        path=path,
        day=None,
        reason=reason,
        message=message,
        hint=hint,
        journal_path=journal_path,
    )


def _delete_target(
    journal_path: Path,
    result: PruneResult,
    class_name: str,
    target: Path,
    day: str,
    *,
    target_kind: str,
    dry_run: bool,
) -> None:
    try:
        size = _path_size(target)
    except FileNotFoundError:
        _record_skip_error(
            journal_path,
            result,
            class_name,
            target,
            day,
            "missing_file_race",
            "path disappeared before pruning could delete it",
            None,
        )
        return
    except OSError as exc:
        _record_skip_error(
            journal_path,
            result,
            class_name,
            target,
            day,
            "delete_failed",
            f"failed to inspect path before pruning: {exc}",
            DELETE_FAILED_HINT,
        )
        return

    if dry_run:
        _add_counts(result, class_name, day, target_kind=target_kind, size=size)
        return

    try:
        if target_kind == "dir":
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
        else:
            target.unlink()
    except FileNotFoundError:
        _record_skip_error(
            journal_path,
            result,
            class_name,
            target,
            day,
            "missing_file_race",
            "path disappeared before pruning could delete it",
            None,
        )
        return
    except OSError as exc:
        _record_skip_error(
            journal_path,
            result,
            class_name,
            target,
            day,
            "delete_failed",
            f"failed to delete path during pruning: {exc}",
            DELETE_FAILED_HINT,
        )
        return

    _add_counts(result, class_name, day, target_kind=target_kind, size=size)


def _path_size(path: Path) -> int:
    stat_result = path.lstat()
    if path.is_dir() and not path.is_symlink():
        total = stat_result.st_size
        for child in path.iterdir():
            total += _path_size(child)
        return total
    return stat_result.st_size


def _add_counts(
    result: PruneResult,
    class_name: str,
    day: str,
    *,
    target_kind: str,
    size: int,
) -> None:
    class_stats = result.by_class[class_name]
    day_stats = result.by_day.setdefault(
        day,
        {"files_deleted": 0, "bytes_freed": 0, "dirs_deleted": 0},
    )
    if target_kind == "dir":
        class_stats["dirs_deleted"] += 1
        day_stats["dirs_deleted"] += 1
        result.dirs_deleted += 1
    else:
        class_stats["files_deleted"] += 1
        day_stats["files_deleted"] += 1
        result.files_deleted += 1
    class_stats["bytes_freed"] += size
    day_stats["bytes_freed"] += size
    result.bytes_freed += size


def _record_skip_error(
    journal_path: Path,
    result: PruneResult,
    class_name: str,
    path: Path,
    day: str | None,
    reason: str,
    message: str,
    hint: str | None,
) -> None:
    _mark_skipped(result, class_name)
    _add_error(
        result,
        class_name,
        path=path,
        day=day,
        reason=reason,
        message=message,
        hint=hint,
        journal_path=journal_path,
    )


def _mark_skipped(result: PruneResult, class_name: str) -> None:
    result.by_class[class_name]["skipped"] += 1


def _add_error(
    result: PruneResult,
    class_name: str,
    *,
    path: Path | None,
    day: str | None,
    reason: str,
    message: str,
    hint: str | None,
    journal_path: Path | None = None,
) -> None:
    if reason == "delete_failed" and not hint:
        hint = DELETE_FAILED_HINT
    entry = {
        "class": class_name,
        "path": _relative_path(path, journal_path) if path is not None else None,
        "day": day,
        "reason": reason,
        "message": message,
        "hint": hint,
    }
    result.errors.append(entry)
    if class_name in result.by_class:
        result.by_class[class_name]["errors"].append(message)
    result.partial_error = True


def _relative_path(path: Path, journal_path: Path | None) -> str:
    if journal_path is None:
        journal_path = Path(get_journal())
    try:
        return path.relative_to(journal_path).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_yyyymmdd(
    value: str,
    *,
    class_name: str,
    path: Path,
    result: PruneResult,
) -> date | None:
    if len(value) != 8 or not value.isdigit():
        _record_skip_error(
            Path(get_journal()),
            result,
            class_name,
            path,
            None,
            "malformed_date",
            f"malformed date in pruning path: {value}",
            None,
        )
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        _record_skip_error(
            Path(get_journal()),
            result,
            class_name,
            path,
            None,
            "malformed_date",
            f"impossible date in pruning path: {value}",
            None,
        )
        return None


def _parse_epoch_ms(stem: str, *, path: Path, result: PruneResult) -> date | None:
    if not stem.isdigit():
        _record_skip_error(
            Path(get_journal()),
            result,
            "talent_run_logs",
            path,
            None,
            "malformed_talent_timestamp",
            f"malformed talent run timestamp: {stem}",
            None,
        )
        return None
    try:
        return datetime.fromtimestamp(int(stem) / 1000).date()
    except (OSError, OverflowError, ValueError):
        _record_skip_error(
            Path(get_journal()),
            result,
            "talent_run_logs",
            path,
            None,
            "malformed_talent_timestamp",
            f"impossible talent run timestamp: {stem}",
            None,
        )
        return None


def _format_day(value: date) -> str:
    return value.strftime("%Y%m%d")

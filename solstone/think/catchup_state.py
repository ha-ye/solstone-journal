# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from solstone.think.utils import (
    day_dirs,
    day_is_complete,
    day_path,
    get_journal,
    iter_segments,
)

logger = logging.getLogger(__name__)

STATE_VERSION = 1

KIND_DAILY_CATCHUP = "daily-catchup"
KIND_DAILY_FROM_SCRATCH = "daily-from-scratch"
KIND_SEGMENT = "segment"
RECORDED_KINDS = frozenset({KIND_DAILY_CATCHUP, KIND_DAILY_FROM_SCRATCH, KIND_SEGMENT})

BACKOFF_BASE_SECONDS = 600
BACKOFF_MAX_SECONDS = 86400
STUCK_THRESHOLD = 3
RETENTION_DAYS = 30

RAW_HASHED_NAMES = frozenset(
    {
        "audio.json",
        "audio.jsonl",
        "screen.jsonl",
        "conversation_transcript.jsonl",
        "chat.jsonl",
    }
)
RAW_HASHED_SUFFIXES = ("_audio.jsonl", "_screen.jsonl", "_transcript.md")
RAW_HASHED_GLOBS = ("monitor_*_diff.json", "monitor_*_diff_box.json")
MEDIA_EXTENSIONS = frozenset(
    {".flac", ".opus", ".ogg", ".m4a", ".mp3", ".wav", ".webm", ".mp4", ".mov", ".png"}
)

_CATCHUP_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class RecordOutcomeResult:
    recorded: bool
    completed: bool
    entered_backoff: bool
    day: str | None
    command_kind: str | None
    attempts: int
    consecutive_non_completion: int
    last_outcome: str
    next_retry_at: float


@dataclass(frozen=True)
class BackoffTransition:
    day: str
    command_kind: str
    attempts: int
    consecutive_non_completion: int
    last_outcome: str
    next_retry_at: float
    entered_backoff_at: float


def _state_path() -> Path:
    health_dir = Path(get_journal()) / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    return health_dir / "catchup-state.json"


def derive_command_kind(cmd: list[str]) -> str | None:
    if any(flag in cmd for flag in ("--flush", "--activity", "--weekly", "--cadence")):
        return None
    if "--from-scratch" in cmd:
        return KIND_DAILY_FROM_SCRATCH
    if "--segment" in cmd or "--segments" in cmd:
        return KIND_SEGMENT
    if "--day" in cmd:
        return KIND_DAILY_CATCHUP
    return None


def extract_day(cmd: list[str]) -> str | None:
    try:
        index = cmd.index("--day")
    except ValueError:
        return None
    if index + 1 >= len(cmd):
        return None
    return cmd[index + 1]


def _key(day: str, kind: str) -> str:
    return f"{day}:{kind}"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_raw_hashed(name: str) -> bool:
    return (
        name in RAW_HASHED_NAMES
        or name.endswith(RAW_HASHED_SUFFIXES)
        or any(fnmatch.fnmatch(name, pattern) for pattern in RAW_HASHED_GLOBS)
    )


def _is_media(name: str) -> bool:
    return Path(name).suffix.lower() in MEDIA_EXTENSIONS


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "entries": {}}


def _normalize_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return _empty_state()
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"version": STATE_VERSION, "entries": entries}


def _read_state_from_disk() -> dict:
    path = _state_path()
    try:
        with open(path, "r", encoding="utf-8") as file:
            return _normalize_state(json.load(file))
    except FileNotFoundError:
        return _empty_state()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read catchup state: %s", exc)
        return _empty_state()


def _write_state(state: dict) -> None:
    state_path = _state_path()
    health_dir = state_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=health_dir, prefix=".catchup_", suffix=".tmp")
    tmp_file = Path(tmp_path)
    try:
        with open(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
        tmp_file.replace(state_path)
    except BaseException:
        tmp_file.unlink(missing_ok=True)
        raise


def _new_record(day: str, command_kind: str) -> dict:
    return {
        "day": day,
        "command_kind": command_kind,
        "attempts": 0,
        "consecutive_non_completion": 0,
        "last_attempt_at": 0,
        "last_outcome": "",
        "next_retry_at": 0,
        "entered_backoff_at": None,
        "notified_at": None,
        "fingerprint": None,
        "active": None,
    }


def _record_from_entry(entry: object, day: str, command_kind: str) -> dict:
    record = _new_record(day, command_kind)
    if isinstance(entry, dict):
        record.update(entry)
    record["day"] = day
    record["command_kind"] = command_kind
    return record


def _daily_marker_mtime(day: str) -> float | None:
    marker = day_path(day, create=False) / "health" / "daily.updated"
    try:
        return marker.stat().st_mtime
    except FileNotFoundError:
        return None


def _completed_daily(day: str, marker_mtime_at_start: float | None) -> bool:
    # Completion is scoped to marker-delta + day completeness. Force-draining an
    # already-complete no-op day can therefore record a low-harm non-completion.
    current_mtime = _daily_marker_mtime(day)
    return (
        current_mtime is not None
        and (marker_mtime_at_start is None or current_mtime > marker_mtime_at_start)
        and day_is_complete(day)
    )


def _mapped_non_completion(exit_status: str) -> str:
    if exit_status == "timeout":
        return "timeout"
    if exit_status == "error":
        return "error"
    return "ran-not-completed"


def _backoff_delay(consecutive_non_completion: int) -> float:
    return min(
        BACKOFF_BASE_SECONDS * 2 ** (consecutive_non_completion - 1),
        BACKOFF_MAX_SECONDS,
    )


def _outcome_result(
    *,
    recorded: bool,
    completed: bool = False,
    entered_backoff: bool = False,
    day: str | None = None,
    command_kind: str | None = None,
    attempts: int = 0,
    consecutive_non_completion: int = 0,
    last_outcome: str = "",
    next_retry_at: float = 0,
) -> RecordOutcomeResult:
    return RecordOutcomeResult(
        recorded=recorded,
        completed=completed,
        entered_backoff=entered_backoff,
        day=day,
        command_kind=command_kind,
        attempts=attempts,
        consecutive_non_completion=consecutive_non_completion,
        last_outcome=last_outcome,
        next_retry_at=next_retry_at,
    )


def read_raw_input_fingerprint(day: str) -> str:
    day_dir = day_path(day, create=False)
    entries = []
    for _stream, _segment, segment_dir in iter_segments(day):
        try:
            paths = list(segment_dir.iterdir())
        except OSError:
            continue
        for path in paths:
            if not path.is_file():
                continue
            name = path.name
            try:
                if _is_raw_hashed(name):
                    marker = _file_sha256(path)
                elif _is_media(name):
                    marker = f"size:{path.stat().st_size}"
                else:
                    continue
            except OSError:
                continue
            entries.append([path.relative_to(day_dir).as_posix(), marker])

    entries.sort(key=lambda entry: entry[0])
    payload = json.dumps(entries, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def read_catchup_state() -> dict:
    return _read_state_from_disk()


def read_day_record(day: str, command_kind: str) -> dict | None:
    return read_catchup_state()["entries"].get(_key(day, command_kind))


def day_eligible_to_drain(day: str, kind: str) -> bool:
    record = read_day_record(day, kind)
    if record is None:
        return True
    if record.get("active"):
        return False
    if time.time() >= float(record.get("next_retry_at") or 0):
        return True
    return read_raw_input_fingerprint(day) != record.get("fingerprint")


def read_backoff_summary(day: str) -> dict | None:
    record = read_day_record(day, KIND_DAILY_CATCHUP)
    if record is None or record.get("entered_backoff_at") is None:
        return None
    return {
        "backoff_stuck": True,
        "attempts": int(record.get("attempts") or 0),
        "consecutive_non_completion": int(
            record.get("consecutive_non_completion") or 0
        ),
        "last_outcome": record.get("last_outcome") or "",
        "next_retry_at": float(record.get("next_retry_at") or 0),
    }


def _prune(state: dict) -> None:
    days = day_dirs()
    if not days:
        return
    newest = max(days.keys())
    cutoff = (
        datetime.strptime(newest, "%Y%m%d") - timedelta(days=RETENTION_DAYS)
    ).strftime("%Y%m%d")
    entries = state["entries"]
    for key, record in list(entries.items()):
        if not isinstance(record, dict):
            continue
        if str(record.get("day") or "") >= cutoff:
            continue
        if record.get("active") is not None:
            continue
        is_completed = record.get("last_outcome") == "completed"
        is_cleared = (
            int(record.get("consecutive_non_completion") or 0) == 0
            and float(record.get("next_retry_at") or 0) == 0
            and record.get("entered_backoff_at") is None
        )
        if is_completed or is_cleared:
            entries.pop(key, None)


def record_attempt(
    cmd: list[str], day: str | None, ref: str, *, started_at: float
) -> None:
    try:
        kind = derive_command_kind(cmd)
        if kind not in RECORDED_KINDS:
            return
        cmd_day = extract_day(cmd)
        if cmd_day is None:
            logger.warning("Cannot record catchup attempt without --day: %s", cmd)
            return
        if day is not None and day != cmd_day:
            logger.warning(
                "Catchup attempt day mismatch: param=%s cmd=%s; using cmd day",
                day,
                cmd_day,
            )

        marker_mtime = _daily_marker_mtime(cmd_day)
        fingerprint = (
            read_raw_input_fingerprint(cmd_day) if kind == KIND_DAILY_CATCHUP else None
        )

        with _CATCHUP_STATE_LOCK:
            state = _read_state_from_disk()
            entries = state["entries"]
            key = _key(cmd_day, kind)
            record = _record_from_entry(entries.get(key), cmd_day, kind)
            if kind == KIND_DAILY_CATCHUP and record.get("fingerprint") != fingerprint:
                record["consecutive_non_completion"] = 0
                record["entered_backoff_at"] = None
                record["notified_at"] = None
                record["next_retry_at"] = 0
            if kind == KIND_DAILY_CATCHUP:
                record["fingerprint"] = fingerprint
            else:
                record["fingerprint"] = None
            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["last_attempt_at"] = started_at
            record["active"] = {
                "ref": ref,
                "started_at": started_at,
                "marker_mtime_at_start": marker_mtime,
            }
            entries[key] = record
            _prune(state)
            _write_state(state)
    except Exception:
        logger.warning("Failed to record catchup attempt", exc_info=True)


def record_outcome(
    cmd: list[str],
    day: str | None,
    ref: str,
    *,
    exit_status: str,
    ended_at: float,
) -> RecordOutcomeResult:
    del ref
    try:
        kind = derive_command_kind(cmd)
        if kind not in RECORDED_KINDS:
            return _outcome_result(recorded=False)
        cmd_day = extract_day(cmd)
        if cmd_day is None:
            logger.warning("Cannot record catchup outcome without --day: %s", cmd)
            return _outcome_result(recorded=False, command_kind=kind)
        if day is not None and day != cmd_day:
            logger.warning(
                "Catchup outcome day mismatch: param=%s cmd=%s; using cmd day",
                day,
                cmd_day,
            )

        with _CATCHUP_STATE_LOCK:
            state = _read_state_from_disk()
            entries = state["entries"]
            key = _key(cmd_day, kind)
            record = _record_from_entry(entries.get(key), cmd_day, kind)
            active = record.get("active")
            marker_at_start = (
                active.get("marker_mtime_at_start")
                if isinstance(active, dict)
                else None
            )
            record["active"] = None

            if kind == KIND_SEGMENT:
                completed = exit_status == "ok"
                last_outcome = (
                    "completed" if completed else _mapped_non_completion(exit_status)
                )
                record["last_outcome"] = last_outcome
                entries[key] = record
                _prune(state)
                _write_state(state)
                return _outcome_result(
                    recorded=True,
                    completed=completed,
                    day=cmd_day,
                    command_kind=kind,
                    attempts=int(record.get("attempts") or 0),
                    consecutive_non_completion=int(
                        record.get("consecutive_non_completion") or 0
                    ),
                    last_outcome=last_outcome,
                    next_retry_at=float(record.get("next_retry_at") or 0),
                )

            completed = _completed_daily(cmd_day, marker_at_start)
            attempts = int(record.get("attempts") or 0)
            if completed:
                entries.pop(_key(cmd_day, KIND_DAILY_CATCHUP), None)
                entries.pop(_key(cmd_day, KIND_DAILY_FROM_SCRATCH), None)
                _prune(state)
                _write_state(state)
                return _outcome_result(
                    recorded=True,
                    completed=True,
                    day=cmd_day,
                    command_kind=kind,
                    attempts=attempts,
                    last_outcome="completed",
                )

            last_outcome = _mapped_non_completion(exit_status)
            record["last_outcome"] = last_outcome
            entered_backoff = False
            if kind == KIND_DAILY_CATCHUP:
                consecutive = int(record.get("consecutive_non_completion") or 0) + 1
                next_retry_at = ended_at + _backoff_delay(consecutive)
                record["consecutive_non_completion"] = consecutive
                record["next_retry_at"] = next_retry_at
                if (
                    consecutive >= STUCK_THRESHOLD
                    and record.get("entered_backoff_at") is None
                ):
                    record["entered_backoff_at"] = ended_at
                    record["notified_at"] = ended_at
                    entered_backoff = True
            else:
                consecutive = int(record.get("consecutive_non_completion") or 0)
                next_retry_at = float(record.get("next_retry_at") or 0)

            entries[key] = record
            _prune(state)
            _write_state(state)
            return _outcome_result(
                recorded=True,
                completed=False,
                entered_backoff=entered_backoff,
                day=cmd_day,
                command_kind=kind,
                attempts=attempts,
                consecutive_non_completion=consecutive,
                last_outcome=last_outcome,
                next_retry_at=next_retry_at,
            )
    except Exception:
        logger.warning("Failed to record catchup outcome", exc_info=True)
        return _outcome_result(recorded=False)


def clear_day_backoff(day: str) -> None:
    try:
        with _CATCHUP_STATE_LOCK:
            state = _read_state_from_disk()
            entries = state["entries"]
            entries.pop(_key(day, KIND_DAILY_CATCHUP), None)
            entries.pop(_key(day, KIND_DAILY_FROM_SCRATCH), None)
            _prune(state)
            _write_state(state)
    except Exception:
        logger.warning("Failed to clear catchup backoff for %s", day, exc_info=True)


def reconcile_interrupted_attempts() -> list[BackoffTransition]:
    try:
        with _CATCHUP_STATE_LOCK:
            state = _read_state_from_disk()
            entries = state["entries"]
            transitions: list[BackoffTransition] = []
            changed = False
            now = time.time()
            for key, record in list(entries.items()):
                if key not in entries or not isinstance(record, dict):
                    continue
                if not record.get("active"):
                    continue
                kind = record.get("command_kind")
                day = record.get("day")
                if not isinstance(day, str) or kind not in RECORDED_KINDS:
                    record["active"] = None
                    changed = True
                    continue

                if kind in (
                    KIND_DAILY_CATCHUP,
                    KIND_DAILY_FROM_SCRATCH,
                ) and day_is_complete(day):
                    entries.pop(_key(day, KIND_DAILY_CATCHUP), None)
                    entries.pop(_key(day, KIND_DAILY_FROM_SCRATCH), None)
                    changed = True
                    continue

                record["active"] = None
                record["last_outcome"] = "interrupted"
                changed = True
                if kind == KIND_DAILY_CATCHUP:
                    consecutive = int(record.get("consecutive_non_completion") or 0) + 1
                    next_retry_at = now + _backoff_delay(consecutive)
                    record["consecutive_non_completion"] = consecutive
                    record["next_retry_at"] = next_retry_at
                    if (
                        consecutive >= STUCK_THRESHOLD
                        and record.get("entered_backoff_at") is None
                    ):
                        record["entered_backoff_at"] = now
                        record["notified_at"] = now
                        transitions.append(
                            BackoffTransition(
                                day=day,
                                command_kind=kind,
                                attempts=int(record.get("attempts") or 0),
                                consecutive_non_completion=consecutive,
                                last_outcome="interrupted",
                                next_retry_at=next_retry_at,
                                entered_backoff_at=now,
                            )
                        )
                entries[key] = record

            if changed:
                _prune(state)
                _write_state(state)
            return transitions
    except Exception:
        logger.warning(
            "Failed to reconcile interrupted catchup attempts", exc_info=True
        )
        return []

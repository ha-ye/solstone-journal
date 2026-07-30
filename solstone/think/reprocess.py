# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Submit past journal days for daily reprocessing."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from solstone.think.callosum import callosum_send
from solstone.think.catchup_state import read_drain_hold_retry_at
from solstone.think.cluster import cluster_segments
from solstone.think.streams import touch_stream_health_marker
from solstone.think.utils import (
    DATE_RE,
    day_is_complete,
    day_path,
    iter_segments,
    setup_cli,
)

UNREACHABLE_MESSAGE = "supervisor not reachable - start it (journal start), then retry"
FLAVOR_PROCESS_NOW = "process-now"
FLAVOR_FROM_SCRATCH = "from-scratch"
FLAVOR_MARK_UPDATED = "mark-updated"
THROUGH_REQUIRES_FROM_SCRATCH = "--through requires --from-scratch"
THROUGH_BEFORE_START = "--through must be on or after the start day"
NO_DATA_RANGE = "no data for days {start} through {end}"


class ReprocessCode(Enum):
    MALFORMED_DAY = "malformed_day"
    PAST_ONLY = "past_only"
    NO_DATA = "no_data"
    FROM_SCRATCH_SUBMITTED = "from_scratch_submitted"
    MARK_UPDATED_SUBMITTED = "mark_updated_submitted"
    ALREADY_COMPLETE = "already_complete"
    HELD_BY_BACKOFF = "held_by_backoff"
    PROCESS_NOW_SUBMITTED = "process_now_submitted"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class ReprocessOutcome:
    code: ReprocessCode
    when: str | None = None


@dataclass(frozen=True)
class RangeDay:
    day: str
    has_iter_segments_data: bool
    cluster_segment_count: int


_CLI_STDOUT = {
    ReprocessCode.PROCESS_NOW_SUBMITTED: "reprocess (process-now) submitted for {day}",
    ReprocessCode.FROM_SCRATCH_SUBMITTED: (
        "reprocess (from-scratch) submitted for {day}"
    ),
    ReprocessCode.MARK_UPDATED_SUBMITTED: (
        "reprocess (mark-updated) submitted for {day}"
    ),
    ReprocessCode.ALREADY_COMPLETE: (
        "day {day} already complete; use --from-scratch to force a full re-run"
    ),
    ReprocessCode.HELD_BY_BACKOFF: (
        "day {day} is held until {when}; use --from-scratch to start it over now"
    ),
}

_CLI_STDERR = {
    ReprocessCode.MALFORMED_DAY: "expected day in YYYYMMDD format",
    ReprocessCode.PAST_ONLY: (
        "reprocess is past-only (cannot reprocess today or a future day)"
    ),
    ReprocessCode.NO_DATA: "no data for day {day}",
    ReprocessCode.UNREACHABLE: UNREACHABLE_MESSAGE,
}


def _format_retry_clock(value: datetime) -> str:
    return value.strftime("%I:%M%p").lstrip("0").lower()


def _format_retry_when(epoch_seconds: float) -> str:
    retry = datetime.fromtimestamp(epoch_seconds)
    retry_day = retry.date()
    today = date.today()
    if retry_day == today:
        label = "today"
    elif retry_day == today + timedelta(days=1):
        label = "tomorrow"
    else:
        label = f"{retry:%b}".lower() + f" {retry.day}"
    return f"{label} at {_format_retry_clock(retry)}"


def reprocess_day(day: str, flavor: str) -> ReprocessOutcome:
    if not DATE_RE.fullmatch(day):
        return ReprocessOutcome(ReprocessCode.MALFORMED_DAY)
    try:
        parsed = datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return ReprocessOutcome(ReprocessCode.MALFORMED_DAY)
    if parsed >= date.today():
        return ReprocessOutcome(ReprocessCode.PAST_ONLY)
    day_dir = day_path(day, create=False)
    if not day_dir.is_dir() or not iter_segments(day):
        return ReprocessOutcome(ReprocessCode.NO_DATA)

    if flavor == FLAVOR_FROM_SCRATCH:
        # Supervisor request dedups by the "daily" command partition, not by day.
        # A successful send means the request reached supervisor, not that it ran.
        ok = callosum_send(
            "supervisor",
            "request",
            cmd=["journal", "think", "-v", "--day", day, "--from-scratch"],
            day=day,
            queue_if_active_cmd_differs=True,
        )
        return ReprocessOutcome(
            ReprocessCode.FROM_SCRATCH_SUBMITTED if ok else ReprocessCode.UNREACHABLE
        )

    if flavor == FLAVOR_MARK_UPDATED:
        # Durable effect first: advance stream.updated so updated_days() re-queues
        # the day even if it already completed. Then nudge a drain.
        touch_stream_health_marker(day)
        ok = callosum_send("supervisor", "drain", day=day)
        return ReprocessOutcome(
            ReprocessCode.MARK_UPDATED_SUBMITTED if ok else ReprocessCode.UNREACHABLE
        )

    if day_is_complete(day):
        return ReprocessOutcome(ReprocessCode.ALREADY_COMPLETE)

    retry_at = read_drain_hold_retry_at(day)
    if retry_at is not None:
        return ReprocessOutcome(
            ReprocessCode.HELD_BY_BACKOFF, when=_format_retry_when(retry_at)
        )

    ok = callosum_send("supervisor", "drain", day=day)
    return ReprocessOutcome(
        ReprocessCode.PROCESS_NOW_SUBMITTED if ok else ReprocessCode.UNREACHABLE
    )


def _parse_day(day: str) -> date | None:
    if not DATE_RE.fullmatch(day):
        return None
    try:
        return datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None


def _enumerate_range_days(start: date, through: date) -> list[RangeDay]:
    range_days: list[RangeDay] = []
    current = start
    while current <= through:
        day = current.strftime("%Y%m%d")
        iter_segments_entries = iter_segments(day)
        has_iter_segments_data = day_path(day, create=False).is_dir() and bool(
            iter_segments_entries
        )
        cluster_segment_count = (
            len(cluster_segments(day)) if has_iter_segments_data else 0
        )
        range_days.append(
            RangeDay(
                day=day,
                has_iter_segments_data=has_iter_segments_data,
                cluster_segment_count=cluster_segment_count,
            )
        )
        current += timedelta(days=1)
    return range_days


def _range_data_days(range_days: list[RangeDay]) -> list[RangeDay]:
    return [entry for entry in range_days if entry.has_iter_segments_data]


def _range_segment_count(range_days: list[RangeDay]) -> int:
    return sum(entry.cluster_segment_count for entry in _range_data_days(range_days))


def _print_range_plan(range_days: list[RangeDay]) -> None:
    data_days = _range_data_days(range_days)
    print("from-scratch reprocess plan:")
    print(
        f"{len(data_days)} days with data ({_range_segment_count(range_days)} segments) "
        "will be queued. Progress will be visible in journal top or journal health. "
        "Queued days do not survive a supervisor restart."
    )
    print(
        "These days run one at a time and can take hours; today's own journal "
        "processing waits until the whole range finishes."
    )
    print("re-run with --yes to proceed")


def _format_day_set(days: list[str]) -> str:
    return ", ".join(days) if days else "none"


def _run_from_scratch_range(range_days: list[RangeDay]) -> None:
    data_days = [entry.day for entry in _range_data_days(range_days)]
    queued_days: list[str] = []
    for entry in range_days:
        outcome = reprocess_day(entry.day, FLAVOR_FROM_SCRATCH)
        code = outcome.code
        if code is ReprocessCode.NO_DATA:
            continue
        if code is ReprocessCode.FROM_SCRATCH_SUBMITTED:
            queued_days.append(entry.day)
            continue
        if code is ReprocessCode.UNREACHABLE:
            not_queued_days = data_days[len(queued_days) :]
            print(
                "failed to queue day "
                f"{len(queued_days) + 1} of {len(data_days)} ({entry.day}): "
                f"{UNREACHABLE_MESSAGE}",
                file=sys.stderr,
            )
            print(f"queued day set: {_format_day_set(queued_days)}", file=sys.stderr)
            print(
                f"not-queued day set: {_format_day_set(not_queued_days)}",
                file=sys.stderr,
            )
            raise SystemExit(1)

    print(
        f"queued from-scratch reprocess for {len(data_days)} days "
        f"({_range_segment_count(range_days)} segments)"
    )
    print("progress is visible in journal top or journal health")
    print("queued days do not survive a supervisor restart")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit a past journal day for reprocessing"
    )
    parser.add_argument("day", help="Past day in YYYYMMDD format")
    parser.add_argument("--through", help="Inclusive range end in YYYYMMDD format")
    parser.add_argument("--yes", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--from-scratch",
        action="store_true",
        help="Force a full daily re-run, preserving markers (does not flag the day as updated)",
    )
    group.add_argument(
        "--mark-updated",
        action="store_true",
        help="Flag the day as having new raw data so daily processing re-queues it, then nudge a drain",
    )

    args = setup_cli(parser)
    if args.mark_updated:
        flavor = FLAVOR_MARK_UPDATED
    elif args.from_scratch:
        flavor = FLAVOR_FROM_SCRATCH
    else:
        flavor = FLAVOR_PROCESS_NOW

    if args.through:
        if flavor != FLAVOR_FROM_SCRATCH:
            print(THROUGH_REQUIRES_FROM_SCRATCH, file=sys.stderr)
            raise SystemExit(1)
        start = _parse_day(args.day)
        through = _parse_day(args.through)
        if start is None or through is None:
            print(_CLI_STDERR[ReprocessCode.MALFORMED_DAY], file=sys.stderr)
            raise SystemExit(1)
        if start >= date.today() or through >= date.today():
            print(_CLI_STDERR[ReprocessCode.PAST_ONLY], file=sys.stderr)
            raise SystemExit(1)
        if through < start:
            print(THROUGH_BEFORE_START, file=sys.stderr)
            raise SystemExit(1)

        range_days = _enumerate_range_days(start, through)
        if not _range_data_days(range_days):
            print(
                NO_DATA_RANGE.format(start=args.day, end=args.through),
                file=sys.stderr,
            )
            raise SystemExit(1)
        if not args.yes:
            _print_range_plan(range_days)
            return
        _run_from_scratch_range(range_days)
        return

    outcome = reprocess_day(args.day, flavor)
    code = outcome.code
    if code in _CLI_STDOUT:
        print(_CLI_STDOUT[code].format(day=args.day, when=outcome.when))
        return

    print(_CLI_STDERR[code].format(day=args.day), file=sys.stderr)
    raise SystemExit(1)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI for observer management.

Provides commands for listing, renaming, revoking, and checking status of
observer registrations. Operates directly on the journal filesystem — no
dependency on the Convey web server.

Usage:
    journal observer create [name]           Explain retired manual observer creation
    journal observer list                    List all registered observers
    journal observer rename <old> <new>      Rename an observer
    journal observer revoke <name-or-prefix> Revoke an observer registration
    journal observer status [name-or-prefix] Show observer status details
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys

import solstone.convey.state as convey_state
from solstone.apps.observer.utils import (
    _find_observer,
    find_observer_by_name,
    get_hist_dir,
    list_observers,
    load_history,
    observer_filename_prefix,
    pruned_segments,
    revoke_observer_record,
    save_observer,
)
from solstone.apps.utils import log_app_action
from solstone.think.utils import (
    get_journal,
    now_ms,
    require_solstone,
    setup_cli,
)

logger = logging.getLogger(__name__)

# Connected threshold: last_seen within 2 minutes (matches web UI)
CONNECTED_THRESHOLD_MS = 2 * 60 * 1000

CREATE_RETIRED_MESSAGE = (
    "journal observer create is retired. observers register themselves over "
    "a private link.\n"
    "pair the device instead:  sol call link pair --device-label <name>\n"
    "if a device was re-paired and its stream is stuck, clear the old record first:\n"
    "  journal observer revoke <name>\n"
)


def _status_label(observer: dict) -> str:
    """Get human-readable connection status."""
    if observer.get("revoked", False):
        return "revoked"
    last_seen = observer.get("last_seen")
    if last_seen is None:
        return "disconnected"
    if now_ms() - last_seen < CONNECTED_THRESHOLD_MS:
        return "connected"
    return "disconnected"


def _fmt_bytes(n: int) -> str:
    """Format byte count for display."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _fmt_time(ms: int | None) -> str:
    """Format millisecond timestamp for display."""
    if ms is None:
        return "never"
    dt = datetime.datetime.fromtimestamp(ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_compact_age(value: object) -> str:
    """Format a millisecond timestamp as a compact elapsed age."""
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, int):
        return "—"
    if value < 0:
        return "—"

    delta_ms = now_ms() - value
    if delta_ms < 0:
        return "—"

    seconds = delta_ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _aggregate_stats(records: list[dict]) -> dict:
    """Sum every numeric stat counter present across a group of records."""
    totals: dict[str, int | float] = {}
    for record in records:
        stats = record.get("stats", {})
        if not isinstance(stats, dict):
            continue
        for key, value in stats.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            totals[key] = totals.get(key, 0) + value
    return totals


def reconcile_observers(*, dry_run: bool) -> list[dict]:
    """Collapse duplicate unrevoked observer records, oldest survives.

    Groups unrevoked records by stream name. For each stream with more than one
    record the oldest (min created_at) survives and absorbs the group's summed
    stat counters; the rest are revoked (soft-delete). Single-record streams are
    left untouched. Returns one plan entry per collapsed stream; when dry_run is
    True nothing is written.
    """
    groups: dict[str, list[dict]] = {}
    for record in list_observers():
        if record.get("revoked", False):
            continue
        groups.setdefault(record.get("name", ""), []).append(record)

    plan: list[dict] = []
    for name, records in groups.items():
        if len(records) < 2:
            continue
        survivor = min(records, key=lambda r: r.get("created_at", 0))
        losers = [r for r in records if r is not survivor]
        totals = _aggregate_stats(records)
        plan.append(
            {
                "name": name,
                "survivor_prefix": observer_filename_prefix(survivor),
                "revoked_prefixes": [observer_filename_prefix(r) for r in losers],
                "stats": totals,
            }
        )
        if dry_run:
            continue
        survivor["stats"] = totals
        if not save_observer(survivor):
            raise RuntimeError(f"failed to save reconcile survivor for stream {name}")
        for loser in losers:
            revoke_observer_record(observer_filename_prefix(loser))
    return plan


# === Subcommands ===


def cmd_create(args: argparse.Namespace) -> int:
    """Explain that manual observer creation is retired."""
    print(CREATE_RETIRED_MESSAGE, file=sys.stderr, end="")
    return 2


def cmd_list(args: argparse.Namespace) -> int:
    """List all registered observers."""
    observers = list_observers()

    if args.json_output:
        result = []
        for r in observers:
            stats = r.get("stats", {})
            result.append(
                {
                    "name": r.get("name", ""),
                    "prefix": observer_filename_prefix(r),
                    "status": _status_label(r),
                    "last_seen": r.get("last_seen"),
                    "last_segment_received_at": r.get("last_segment_received_at"),
                    "last_segment_day": r.get("last_segment_day"),
                    "segments": stats.get("segments_received", 0),
                    "bytes": stats.get("bytes_received", 0),
                }
            )
        print(json.dumps(result))
        return 0

    if not observers:
        print("No observers registered.")
        return 0

    print(
        f"{'Name':<20} {'Prefix':<18} {'Status':<14} "
        f"{'Last Seen':<18} {'Last Segment':<12} {'Segments':>10} {'Bytes':>12}"
    )
    print("-" * 107)

    for r in observers:
        name = r.get("name", "")
        prefix = observer_filename_prefix(r)
        status = _status_label(r)
        last_seen = _fmt_time(r.get("last_seen"))
        last_segment = _fmt_compact_age(r.get("last_segment_received_at"))
        stats = r.get("stats", {})
        segments = stats.get("segments_received", 0)
        bytes_recv = _fmt_bytes(stats.get("bytes_received", 0))
        print(
            f"{name:<20} {prefix:<18} {status:<14} "
            f"{last_seen:<18} {last_segment:<12} {segments:>10} {bytes_recv:>12}"
        )

    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    """Revoke an observer registration (soft-delete)."""
    identifier = args.identifier

    try:
        observer = revoke_observer_record(identifier)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("observer not found:"):
            print(f"Error: observer '{identifier}' not found", file=sys.stderr)
            return 1
        name = message.removeprefix("observer already revoked: ")
        print(f"Observer '{name}' is already revoked.", file=sys.stderr)
        return 1
    except RuntimeError:
        print("Error: failed to save observer", file=sys.stderr)
        return 1

    name = observer.get("name", "")
    key_prefix = observer_filename_prefix(observer)

    if args.json_output:
        print(json.dumps({"name": name, "prefix": key_prefix, "revoked": True}))
        return 0

    print(f"Revoked observer '{name}' ({key_prefix})")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Collapse duplicate observer registrations per stream."""
    plan = reconcile_observers(dry_run=args.dry_run)
    if not plan:
        print("No duplicate observer streams to reconcile.")
        return 0
    prefix = "[dry-run] would reconcile" if args.dry_run else "Reconciled"
    for entry in plan:
        stats = entry["stats"]
        print(f"{prefix} stream '{entry['name']}':")
        print(f"  survivor:  {entry['survivor_prefix']}")
        print(f"  revoking:  {', '.join(entry['revoked_prefixes'])}")
        print(f"  segments:  {stats.get('segments_received', 0)}")
        print(f"  bytes:     {_fmt_bytes(stats.get('bytes_received', 0))}")
        if stats.get("duplicates_rejected"):
            print(f"  duplicates: {stats['duplicates_rejected']} rejected")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    """Rename an observer (affects future stream names)."""
    identifier = args.identifier
    new_name = args.new_name

    observer = _find_observer(identifier)
    if not observer:
        print(f"Error: observer '{identifier}' not found", file=sys.stderr)
        return 1

    # Check new name isn't taken
    existing = find_observer_by_name(new_name)
    if existing and existing.get("key") != observer.get("key"):
        print(f"Error: observer '{new_name}' already exists", file=sys.stderr)
        return 1

    old_name = observer.get("name", "")
    if old_name == new_name:
        print(f"Observer is already named '{new_name}'.", file=sys.stderr)
        return 1

    key_prefix = observer_filename_prefix(observer)
    observer["name"] = new_name

    if not save_observer(observer):
        print("Error: failed to save observer", file=sys.stderr)
        return 1

    log_app_action(
        app="observer",
        facet=None,
        action="observer_rename",
        params={"old_name": old_name, "new_name": new_name, "key_prefix": key_prefix},
    )

    if args.json_output:
        print(
            json.dumps(
                {"old_name": old_name, "new_name": new_name, "prefix": key_prefix}
            )
        )
        return 0

    print(f"Renamed observer '{old_name}' -> '{new_name}' ({key_prefix})")
    print(f"  Future segments will use stream: {new_name}")
    print(f"  Existing segments remain under stream: {old_name}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show observer status details."""
    if args.identifier:
        return _status_single(args.identifier, json_output=args.json_output)
    return _status_all(json_output=args.json_output)


def cmd_prune(args: argparse.Namespace) -> int:
    """Find or delete provable duplicate observer segments."""
    from solstone.apps.observer.prune import (
        format_result,
        resolve_prune_days,
        result_exit_code,
        run_prune,
    )

    try:
        days = resolve_prune_days(
            day=args.day,
            day_range=args.day_range,
            all_days=args.all_days,
        )
        result = run_prune(
            days=days,
            stream=args.stream,
            execute=args.execute,
            cross_start=args.cross_start,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("observer prune failed")
        print(f"Error: observer prune failed: {exc}", file=sys.stderr)
        return 1
    print(format_result(result), end="")
    return result_exit_code(result)


def _status_single(identifier: str, json_output: bool = False) -> int:
    """Detailed status for a single observer."""
    observer = _find_observer(identifier)
    if not observer:
        print(f"Error: observer '{identifier}' not found", file=sys.stderr)
        return 1

    name = observer.get("name", "")
    key_prefix = observer_filename_prefix(observer)
    stats = observer.get("stats", {})

    if json_output:
        print(
            json.dumps(
                {
                    "name": name,
                    "prefix": key_prefix,
                    "status": _status_label(observer),
                    "created_at": observer.get("created_at"),
                    "last_seen": observer.get("last_seen"),
                    "last_segment_received_at": observer.get(
                        "last_segment_received_at"
                    ),
                    "last_segment_day": observer.get("last_segment_day"),
                    "revoked": observer.get("revoked", False),
                    "segments": stats.get("segments_received", 0),
                    "bytes": stats.get("bytes_received", 0),
                }
            )
        )
        return 0

    today = datetime.date.today().strftime("%Y%m%d")
    history = load_history(key_prefix, today)
    pruned = pruned_segments(history)
    uploads = [
        r for r in history if not r.get("type") and r.get("segment") not in pruned
    ]
    last_segment_received_at = observer.get("last_segment_received_at")
    last_segment_day = observer.get("last_segment_day")
    if last_segment_received_at is None and uploads:
        last_upload = uploads[-1]
        ts = last_upload.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, int):
            last_segment_received_at = None
        else:
            last_segment_received_at = ts
            last_segment_day = today
    last_segment_age = _fmt_compact_age(last_segment_received_at)
    last_segment_context = (
        f"{last_segment_age} ({last_segment_day})"
        if isinstance(last_segment_day, str) and last_segment_day
        else last_segment_age
    )
    label_width = len("Last segment:")

    def print_field(label: str, value: str) -> None:
        print(f"  {label:<{label_width}} {value}")

    print(f"Observer: {name}")
    print_field("Prefix:", key_prefix)
    print_field("Status:", _status_label(observer))
    print_field("Created:", _fmt_time(observer.get("created_at")))
    print_field("Last seen:", _fmt_time(observer.get("last_seen")))
    print_field("Last segment:", last_segment_context)
    if observer.get("revoked"):
        print_field("Revoked at:", _fmt_time(observer.get("revoked_at")))
    print_field("Segments:", str(stats.get("segments_received", 0)))
    print_field("Bytes:", _fmt_bytes(stats.get("bytes_received", 0)))
    if stats.get("duplicates_rejected"):
        print_field("Duplicates:", f"{stats['duplicates_rejected']} rejected")

    # Today's sync history
    if history:
        print(f"\n  Today ({today}): {len(uploads)} segment(s) synced")
        for rec in uploads[-5:]:
            seg = rec.get("segment", "?")
            files = rec.get("files", [])
            total = sum(f.get("size", 0) for f in files)
            ts = _fmt_time(rec.get("ts"))
            print(f"    {seg}  {len(files)} file(s)  {_fmt_bytes(total)}  {ts}")

    # Segment count by recent days
    hist_dir = get_hist_dir(key_prefix, ensure_exists=False)
    if hist_dir.exists():
        day_files = sorted(hist_dir.glob("*.jsonl"), reverse=True)[:7]
        if day_files:
            print("\n  Recent days:")
            for df in day_files:
                day = df.stem
                records = load_history(key_prefix, day)
                pruned = pruned_segments(records)
                day_uploads = [
                    r
                    for r in records
                    if not r.get("type") and r.get("segment") not in pruned
                ]
                print(f"    {day}: {len(day_uploads)} segment(s)")

    return 0


def _status_all(json_output: bool = False) -> int:
    """Health overview for all observers."""
    observers = list_observers()

    if not observers and not json_output:
        print("No observers registered.")
        return 0

    labels = [_status_label(r) for r in observers]
    connected = labels.count("connected")
    disconnected = labels.count("disconnected")
    revoked = labels.count("revoked")
    total_segments = sum(
        r.get("stats", {}).get("segments_received", 0) for r in observers
    )
    total_bytes = sum(r.get("stats", {}).get("bytes_received", 0) for r in observers)

    if json_output:
        print(
            json.dumps(
                {
                    "total": len(observers),
                    "connected": connected,
                    "disconnected": disconnected,
                    "revoked": revoked,
                    "total_segments": total_segments,
                    "total_bytes": total_bytes,
                    "observers": [
                        {
                            "name": r.get("name", ""),
                            "prefix": observer_filename_prefix(r),
                            "status": _status_label(r),
                            "last_seen": r.get("last_seen"),
                            "last_segment_received_at": r.get(
                                "last_segment_received_at"
                            ),
                            "last_segment_day": r.get("last_segment_day"),
                        }
                        for r in observers
                    ],
                }
            )
        )
        return 0

    print(f"Observers: {len(observers)} total")
    print(f"  Connected:    {connected}")
    print(f"  Disconnected: {disconnected}")
    print(f"  Revoked:      {revoked}")
    print(f"  Total segments: {total_segments}")
    print(f"  Total bytes:    {_fmt_bytes(total_bytes)}")

    print(
        f"\n{'Name':<20} {'Prefix':<18} {'Status':<14} "
        f"{'Last Seen':<18} {'Last Segment':<12}"
    )
    print("-" * 87)
    for r in observers:
        name = r.get("name", "")
        prefix = observer_filename_prefix(r)
        status = _status_label(r)
        last_seen = _fmt_time(r.get("last_seen"))
        last_segment = _fmt_compact_age(r.get("last_segment_received_at"))
        print(
            f"{name:<20} {prefix:<18} {status:<14} {last_seen:<18} {last_segment:<12}"
        )

    return 0


# === Entry point ===


def main() -> None:
    """Entry point for journal observer CLI."""
    parser = argparse.ArgumentParser(
        prog="journal observer",
        description="Manage observer registrations",
    )

    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output as JSON"
    )

    sub = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="Explain retired manual observer creation")
    p_create.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Name for the observer",
    )
    p_create.add_argument(
        "--json", action="store_true", dest="json_output", help="Output as JSON"
    )

    # list
    sub.add_parser("list", help="List all registered observers")

    # rename
    p_rename = sub.add_parser(
        "rename", help="Rename an observer (affects future streams)"
    )
    p_rename.add_argument("identifier", help="Observer name or key prefix")
    p_rename.add_argument("new_name", help="New name for the observer")

    # revoke
    p_revoke = sub.add_parser("revoke", help="Revoke an observer registration")
    p_revoke.add_argument("identifier", help="Observer name or key prefix")

    # status
    p_status = sub.add_parser("status", help="Show observer status details")
    p_status.add_argument(
        "identifier",
        nargs="?",
        default=None,
        help="Observer name or key prefix (omit for overview)",
    )

    # reconcile
    p_reconcile = sub.add_parser(
        "reconcile",
        help="Collapse duplicate registrations per stream (oldest survives)",
    )
    p_reconcile.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print the reconciliation plan without revoking anything.",
    )

    # prune
    p_prune = sub.add_parser(
        "prune",
        help="Find or delete provable duplicate observer segments",
        description=(
            "Find byte-identical same-start observer duplicate segments. "
            "Canonical is the earliest same-start segment whose content is held "
            "by bytes or terminal proof. "
            "Opt-in cross-start mode also uses server-authored segment_original "
            "provenance after same-start pruning. "
            "Dry-run is the default and performs zero writes. Exit codes: "
            "0 clean, 2 refusals present, 1 usage/error."
        ),
    )
    day_group = p_prune.add_mutually_exclusive_group(required=True)
    day_group.add_argument("--day", help="Prune one day (YYYYMMDD)")
    day_group.add_argument("--day-range", help="Prune inclusive range A..B")
    day_group.add_argument(
        "--all",
        action="store_true",
        dest="all_days",
        help="Scan every journal day",
    )
    p_prune.add_argument("--stream", help="Limit to one stream")
    p_prune.add_argument(
        "--execute",
        action="store_true",
        help="Delete provable duplicates; dry-run is the default.",
    )
    p_prune.add_argument(
        "--cross-start",
        action="store_true",
        help=(
            "Also prune different-start duplicates proven by server-authored "
            "segment_original provenance; runs after same-start. Off by default."
        ),
    )

    args = setup_cli(parser)

    if args.command == "create":
        sys.exit(cmd_create(args))

    # Keep app helpers aligned with the active CLI journal.
    convey_state.journal_root = get_journal()

    require_solstone()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "list": cmd_list,
        "prune": cmd_prune,
        "rename": cmd_rename,
        "reconcile": cmd_reconcile,
        "revoke": cmd_revoke,
        "status": cmd_status,
    }

    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()

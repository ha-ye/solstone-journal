# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for transcript browsing.

Auto-discovered by ``think.call`` and mounted as ``sol call transcripts ...``.
Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
"""

import os

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Transcript browsing.")


def _resolve_sol_day(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("SOL_DAY") or None
    if env:
        return env
    typer.echo("Error: day is required (pass as argument or set SOL_DAY).", err=True)
    raise typer.Exit(1)


def _resolve_sol_segment(arg: str | None) -> str | None:
    if arg:
        return arg
    return os.environ.get("SOL_SEGMENT") or None


def _get_sol_stream() -> str | None:
    return os.environ.get("SOL_STREAM") or None


def _truncated_echo(text: str, max_bytes: int) -> None:
    encoded = text.encode("utf-8")
    if max_bytes > 0 and len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        typer.echo(truncated)
        typer.echo(
            f"[truncated: {len(encoded):,} bytes total, --max {max_bytes:,}]", err=True
        )
    else:
        typer.echo(text)


def _pending_slot_range(start: str) -> tuple[str, str]:
    hour_s, minute_s = start.split(":")
    hour = int(hour_s)
    minute = int(minute_s)
    slot_minute = minute - (minute % 15)
    end_hour = hour
    end_minute = slot_minute + 15
    if end_minute >= 60:
        end_hour = (end_hour + 1) % 24
        end_minute -= 60
    return f"{hour:02d}:{slot_minute:02d}", f"{end_hour:02d}:{end_minute:02d}"


def _format_pending_scan_note(starts: list[str]) -> str:
    count = len(starts)
    noun = "segment" if count == 1 else "segments"
    return f"{count} {noun} pending at {', '.join(starts)}"


def _slot_overlaps_range(slot: tuple[str, str], range_: tuple[str, str]) -> bool:
    def _to_min(hhmm: str) -> int:
        hour_s, minute_s = hhmm.split(":")
        return int(hour_s) * 60 + int(minute_s)

    slot_start, slot_end = (_to_min(slot[0]), _to_min(slot[1]))
    range_start, range_end = (_to_min(range_[0]), _to_min(range_[1]))
    return slot_start < range_end and slot_end > range_start


@app.command("scan")
@convey_cli
def scan(
    day: str | None = typer.Argument(
        default=None, help="Day YYYYMMDD (default: SOL_DAY env)."
    ),
) -> None:
    """List transcript coverage ranges for a day."""
    day = _resolve_sol_day(day)
    data = get_client().request("GET", f"/app/transcripts/api/day/{day}")
    transcript_ranges = [(r["start"], r["end"]) for r in data["audio"]]
    screen_ranges = [(r["start"], r["end"]) for r in data["screen"]]
    segments = data["segments"]
    pending_by_slot: dict[tuple[str, str], list[str]] = {}
    for segment in segments:
        if segment.get("data_state", {}).get("audio") != "pending":
            continue
        slot = _pending_slot_range(segment["start"])
        pending_by_slot.setdefault(slot, []).append(segment["start"])
    for starts in pending_by_slot.values():
        starts.sort()

    typer.echo("Transcripts:")
    if transcript_ranges:
        for start, end in transcript_ranges:
            starts = [
                pending_start
                for slot, slot_starts in pending_by_slot.items()
                if _slot_overlaps_range(slot, (start, end))
                for pending_start in slot_starts
            ]
            line = f"  {start} - {end}"
            if starts:
                starts.sort()
                line += f" ({_format_pending_scan_note(starts)})"
            typer.echo(line)
    else:
        typer.echo("  (none)")

    typer.echo("Percepts:")
    if screen_ranges:
        for start, end in screen_ranges:
            typer.echo(f"  {start} - {end}")
    else:
        typer.echo("  (none)")


@app.command("segments")
@convey_cli
def segments(
    day: str | None = typer.Argument(
        default=None, help="Day YYYYMMDD (default: SOL_DAY env)."
    ),
) -> None:
    """List recording segments for a day."""
    day = _resolve_sol_day(day)
    segment_list = get_client().request("GET", f"/app/transcripts/api/segments/{day}")[
        "segments"
    ]
    if not segment_list:
        typer.echo("No segments.")
        return

    for segment in segment_list:
        key = segment.get("key", "")
        start = segment.get("start", "")
        end = segment.get("end", "")
        types = ", ".join(segment.get("types", []))
        typer.echo(f"{key}  {start} - {end}  [{types}]")


@app.command("read")
@convey_cli
def read(
    day: str | None = typer.Argument(
        default=None, help="Day YYYYMMDD (default: SOL_DAY env)."
    ),
    start: str | None = typer.Option(None, "--start", help="Start time (HHMMSS)."),
    length: int | None = typer.Option(None, "--length", help="Length in minutes."),
    segment: str | None = typer.Option(
        None, "--segment", help="Segment key (HHMMSS_LEN, default: SOL_SEGMENT env)."
    ),
    segments: str | None = typer.Option(
        None, "--segments", help="Comma-separated segment keys for a span."
    ),
    stream: str | None = typer.Option(
        None, "--stream", help="Stream name (default: SOL_STREAM env)."
    ),
    full: bool = typer.Option(
        False, "--full", help="Include transcripts, screen, and agents."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Include transcripts and screen only."
    ),
    transcripts: bool = typer.Option(
        False, "--transcripts", help="Include transcript content."
    ),
    audio: bool = typer.Option(
        False, "--audio", help="Alias for --transcripts.", hidden=True
    ),
    percepts: bool = typer.Option(False, "--percepts", help="Include screen percepts."),
    screen: bool = typer.Option(
        False, "--screen", help="Alias for --percepts.", hidden=True
    ),
    agents: bool = typer.Option(False, "--agents", help="Include agent outputs."),
    max_bytes: int = typer.Option(
        16384, "--max", help="Max output bytes (0 = unlimited)."
    ),
) -> None:
    """Read transcript content for a day, segment, or time range."""
    day = _resolve_sol_day(day)
    segment = _resolve_sol_segment(segment)
    stream = stream or _get_sol_stream()
    # --audio is an alias for --transcripts, --screen is an alias for --percepts
    transcripts = transcripts or audio
    percepts = percepts or screen

    if full and raw:
        typer.echo("Error: Cannot use --full and --raw together.", err=True)
        raise typer.Exit(1)

    if (full or raw) and (transcripts or percepts or agents):
        typer.echo(
            "Error: Cannot mix --full/--raw with individual source flags.", err=True
        )
        raise typer.Exit(1)

    if full:
        sources: dict[str, bool] = {
            "transcripts": True,
            "percepts": True,
            "agents": True,
        }
    elif raw:
        sources = {"transcripts": True, "percepts": True, "agents": False}
    elif transcripts or percepts or agents:
        sources = {"transcripts": transcripts, "percepts": percepts, "agents": agents}
    else:
        sources = {"transcripts": True, "percepts": False, "agents": True}

    # Validate mutually exclusive selection modes
    mode_count = sum(
        [
            segment is not None,
            segments is not None,
            start is not None or length is not None,
        ]
    )
    if mode_count > 1:
        typer.echo(
            "Error: Cannot mix --segment, --segments, and --start/--length.",
            err=True,
        )
        raise typer.Exit(1)

    if (start is not None) != (length is not None):
        typer.echo("Error: --start and --length must be used together.", err=True)
        raise typer.Exit(1)

    params: dict[str, str] = {
        "transcripts": "1" if sources["transcripts"] else "0",
        "percepts": "1" if sources["percepts"] else "0",
        "agents": "1" if sources["agents"] else "0",
    }
    if start is not None and length is not None:
        from datetime import datetime, timedelta

        start_dt = datetime.strptime(start, "%H%M%S")
        end_dt = start_dt + timedelta(minutes=length)
        params["start"] = start
        params["end"] = end_dt.strftime("%H%M%S")
    elif segments is not None:
        params["segments"] = segments
        if stream:
            params["stream"] = stream
    elif segment is not None:
        params["segment"] = segment
        if stream:
            params["stream"] = stream
    markdown = get_client().request(
        "GET", f"/app/transcripts/api/read/{day}", params=params
    )["markdown"]

    _truncated_echo(markdown, max_bytes)


@app.command("stats")
@convey_cli
def stats(month: str = typer.Argument(help="Month (YYYYMM).")) -> None:
    """Show daily transcript coverage counts for a month."""
    client = get_client()
    day_totals = client.request("GET", f"/app/transcripts/api/stats/{month}")
    days = sorted(day_totals.keys())
    days_with_data = 0
    for day in days:
        ranges = client.request("GET", f"/app/transcripts/api/ranges/{day}")
        n_transcripts = len(ranges["audio"])
        n_percepts = len(ranges["screen"])
        days_with_data += 1
        typer.echo(f"{day}  transcripts:{n_transcripts} percepts:{n_percepts}")
    if not days_with_data:
        typer.echo(f"No data for {month}.")
        return
    typer.echo("")
    typer.echo(f"Total: {days_with_data} days with data")

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for completed activity record management.

Auto-discovered by ``think.call`` and mounted as ``sol call activities ...``.
Every verb reaches the journal only over HTTP via the Convey client.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import typer

from solstone.convey.reasons import (
    ACTIVITY_ALREADY_EXISTS,
    ACTIVITY_INVALID,
    ACTIVITY_NOT_FOUND,
)
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

_PARTICIPATION_ROLES = {"attendee", "mentioned"}
_PARTICIPATION_SOURCES = {"voice", "speaker_label", "transcript", "screen", "other"}

app = typer.Typer(help="Completed activity record management.")


def _get_sol_day() -> str | None:
    return os.environ.get("SOL_DAY") or None


def _get_sol_facet() -> str | None:
    return os.environ.get("SOL_FACET") or None


def _resolve_sol_day(arg: str | None) -> str:
    if arg:
        return arg
    env = _get_sol_day()
    if env:
        return env
    typer.echo("Error: day is required (pass as argument or set SOL_DAY).", err=True)
    raise typer.Exit(1)


def _resolve_sol_day_or_today(arg: str | None) -> str:
    if arg:
        return arg
    env = _get_sol_day()
    if env:
        return env
    return datetime.now().strftime("%Y%m%d")


def _resolve_sol_facet(arg: str | None) -> str:
    if arg:
        return arg
    env = _get_sol_facet()
    if env:
        return env
    typer.echo(
        "Error: facet is required (pass as argument or set SOL_FACET).", err=True
    )
    raise typer.Exit(1)


def _valid_segment_key(segment: str) -> bool:
    if "_" not in segment:
        return False
    time_part, len_part = segment.split("_", 1)
    if len(time_part) != 6 or not time_part.isdigit() or not len_part.isdigit():
        return False
    hour = int(time_part[:2])
    minute = int(time_part[2:4])
    second = int(time_part[4:6])
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def _read_stdin_json(*, allow_empty: bool = False) -> dict[str, object]:
    """Parse a single JSON object from stdin."""
    raw = sys.stdin.read().strip()
    if not raw:
        if allow_empty:
            return {}
        typer.echo("Error: expected JSON object on stdin.", err=True)
        raise typer.Exit(1)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: invalid JSON on stdin: {exc}", err=True)
        raise typer.Exit(1) from None

    if not isinstance(payload, dict):
        typer.echo("Error: expected JSON object on stdin.", err=True)
        raise typer.Exit(1)
    return payload


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _parse_day(value: str, *, label: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        typer.echo(f"Error: invalid {label} '{value}'", err=True)
        raise typer.Exit(1) from None


def _iter_days(start_day: str, end_day: str) -> list[str]:
    start = _parse_day(start_day, label="day")
    end = _parse_day(end_day, label="day")
    if end < start:
        typer.echo(
            f"Error: --to ({end_day}) must not be before --from ({start_day})",
            err=True,
        )
        raise typer.Exit(1)

    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return days


def _validate_participation(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        typer.echo("Error: participation must be an array", err=True)
        raise typer.Exit(1)

    cleaned_entries: list[dict[str, object]] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            typer.echo(f"Error: participation[{i}] must be an object", err=True)
            raise typer.Exit(1)

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            typer.echo(
                f"Error: participation[{i}] requires a non-empty string 'name'",
                err=True,
            )
            raise typer.Exit(1)

        role = entry.get("role")
        if role not in _PARTICIPATION_ROLES:
            typer.echo(
                f"Error: participation[{i}] has invalid role '{role}' "
                f"(must be one of {sorted(_PARTICIPATION_ROLES)})",
                err=True,
            )
            raise typer.Exit(1)

        source = entry.get("source")
        if source not in _PARTICIPATION_SOURCES:
            typer.echo(
                f"Error: participation[{i}] has invalid source '{source}' "
                f"(must be one of {sorted(_PARTICIPATION_SOURCES)})",
                err=True,
            )
            raise typer.Exit(1)

        confidence = entry.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            typer.echo(
                f"Error: participation[{i}] 'confidence' must be a number",
                err=True,
            )
            raise typer.Exit(1)

        context = entry.get("context")
        if not isinstance(context, str):
            typer.echo(
                f"Error: participation[{i}] 'context' must be a string",
                err=True,
            )
            raise typer.Exit(1)

        cleaned_entry = {key: item for key, item in entry.items() if key != "entity_id"}
        cleaned_entry["name"] = name.strip()
        cleaned_entry["role"] = role
        cleaned_entry["source"] = source
        cleaned_entry["confidence"] = confidence
        cleaned_entry["context"] = context
        cleaned_entries.append(cleaned_entry)

    return cleaned_entries


def _filter_items(
    items: list[dict[str, object]],
    *,
    activity: str | None,
    entity: str | None,
    source: str | None,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    entity_query = entity.lower() if entity else None
    for item in items:
        record = item["record"]
        if not isinstance(record, dict):
            continue
        if activity and record.get("activity") != activity:
            continue
        if source and record.get("source") != source:
            continue
        if entity_query:
            active_entities = record.get("active_entities", [])
            if not any(
                entity_query in str(active_entity).lower()
                for active_entity in active_entities
            ):
                continue
        matches.append(item)
    return matches


def _sort_item_key(item: dict[str, object]) -> tuple[str, str, int, str]:
    record = item["record"]
    if not isinstance(record, dict):
        return ("", "", 0, "")
    return (
        str(record.get("day", "")),
        str(record.get("facet", "")),
        int(record.get("created_at", 0) or 0),
        str(record.get("id", "")),
    )


def _echo_item_markdown(item: dict[str, object]) -> None:
    typer.echo(item["markdown"])


@app.command("list")
@convey_cli
def list_records(
    day: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day in YYYYMMDD format (or set SOL_DAY).",
    ),
    from_day: str | None = typer.Option(
        None,
        "--from",
        help="Start day for an inclusive range query (YYYYMMDD).",
    ),
    to_day: str | None = typer.Option(
        None,
        "--to",
        help="End day for an inclusive range query (YYYYMMDD).",
    ),
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET). Omit to query all facets.",
    ),
    activity: str | None = typer.Option(
        None,
        "--activity",
        "-a",
        help="Filter by activity type.",
    ),
    entity: str | None = typer.Option(
        None,
        "--entity",
        help="Filter by active entity.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Filter by record source: anticipated, user, or cogitate.",
    ),
    include_all: bool = typer.Option(
        False,
        "--all",
        help="Include hidden activity records.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List activity records for one day or an inclusive day range."""
    if day and (from_day or to_day):
        typer.echo("Error: --day is incompatible with --from/--to.", err=True)
        raise typer.Exit(1)

    if day:
        resolved_days = [_resolve_sol_day(day)]
    elif from_day or to_day:
        start_day = from_day or _resolve_sol_day_or_today(None)
        end_day = to_day or start_day
        resolved_days = _iter_days(start_day, end_day)
    else:
        resolved_days = [_resolve_sol_day_or_today(None)]

    if source and source not in {"anticipated", "cogitate", "user"}:
        typer.echo(
            "Error: --source must be 'anticipated', 'cogitate', or 'user'.",
            err=True,
        )
        raise typer.Exit(1)

    client = get_client()
    facet_param = facet or _get_sol_facet() or None
    items: list[dict[str, object]] = []
    for resolved_day in resolved_days:
        params = {"include_hidden": "1" if include_all else "0"}
        if facet_param is not None:
            params["facet"] = facet_param
        body = client.request(
            "GET", f"/app/activities/api/day/{resolved_day}/records", params=params
        )
        items.extend(body["items"])

    items = _filter_items(items, activity=activity, entity=entity, source=source)
    items.sort(key=_sort_item_key)

    if json_output:
        _echo_json([item["record"] for item in items])
    elif not items:
        typer.echo("No activities found.")
    else:
        typer.echo("\n\n".join(str(item["markdown"]) for item in items))


@app.command("get")
@convey_cli
def get_record(
    span_id: str = typer.Argument(help="Activity record ID."),
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET).",
    ),
    day: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day in YYYYMMDD format (or set SOL_DAY).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Fetch one activity record by ID."""
    resolved_facet = _resolve_sol_facet(facet)
    resolved_day = _resolve_sol_day(day)
    try:
        body = get_client().request(
            "GET",
            f"/app/activities/api/day/{resolved_day}/record/{span_id}",
            params={"facet": resolved_facet},
        )
    except ConveyClientError as err:
        if err.reason_code == ACTIVITY_NOT_FOUND.code:
            typer.echo(f"activity not found: {span_id}", err=True)
            raise typer.Exit(1) from err
        raise

    if json_output:
        _echo_json(body["record"])
    else:
        _echo_item_markdown(body)


@app.command("create")
@convey_cli
def create_record(
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET).",
    ),
    day: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day in YYYYMMDD format (or set SOL_DAY).",
    ),
    since_segment: str | None = typer.Option(
        None,
        "--since-segment",
        help="Segment key to anchor the new activity span (HHMMSS_LEN).",
    ),
    source: str = typer.Option(
        "user",
        "--source",
        help="Record source label: user or cogitate.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Activity title (argv mode).",
    ),
    activity: str | None = typer.Option(
        None,
        "--activity",
        help="Activity type (argv mode).",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        help="One-line description (argv mode).",
    ),
    details: str | None = typer.Option(
        None,
        "--details",
        help="Longer details (argv mode).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Create a new synthetic activity record from argv flags or JSON on stdin."""
    if source not in {"cogitate", "user"}:
        typer.echo("Error: --source must be 'cogitate' or 'user'.", err=True)
        raise typer.Exit(1)

    resolved_facet = _resolve_sol_facet(facet)
    resolved_day = _resolve_sol_day(day)

    if since_segment is not None and not _valid_segment_key(since_segment):
        typer.echo(
            f"Error: invalid --since-segment '{since_segment}' (expected HHMMSS_LEN)",
            err=True,
        )
        raise typer.Exit(1)

    payload_flags_supplied = any(
        value is not None for value in (title, activity, description, details)
    )
    if payload_flags_supplied:
        if title is None:
            typer.echo("Error: --title is required.", err=True)
            raise typer.Exit(1)
        if activity is None:
            typer.echo("Error: --activity is required.", err=True)
            raise typer.Exit(1)
        activity_type = activity
        body: dict[str, object] = {
            "title": title,
            "activity": activity,
            "source": source,
        }
        if description is not None:
            body["description"] = description
        if details is not None:
            body["details"] = details
    else:
        payload = _read_stdin_json()
        participation_provided = "participation" in payload

        title = str(payload.get("title") or "").strip()
        if not title:
            typer.echo("Error: title is required.", err=True)
            raise typer.Exit(1)

        activity_type = str(payload.get("activity") or "").strip()
        if not activity_type:
            typer.echo("Error: activity is required.", err=True)
            raise typer.Exit(1)

        body = {
            "title": title,
            "activity": activity_type,
            "source": source,
        }
        if "description" in payload:
            body["description"] = payload["description"]
        if "details" in payload:
            body["details"] = payload["details"]
        if participation_provided:
            body["participation"] = _validate_participation(payload["participation"])

    if since_segment is not None:
        body["since_segment"] = since_segment

    try:
        response = get_client().request(
            "POST",
            f"/app/activities/api/day/{resolved_day}/records",
            params={"facet": resolved_facet},
            json=body,
        )
    except ConveyClientError as err:
        if err.reason_code == ACTIVITY_NOT_FOUND.code:
            typer.echo(
                f"Error: unknown activity for facet '{resolved_facet}': {activity_type}",
                err=True,
            )
            raise typer.Exit(1) from err
        if err.reason_code == ACTIVITY_ALREADY_EXISTS.code:
            typer.echo(f"Error: activity already exists: {err.detail}", err=True)
            raise typer.Exit(1) from err
        if err.reason_code == ACTIVITY_INVALID.code:
            typer.echo(f"Error: {err.detail}", err=True)
            raise typer.Exit(1) from err
        raise

    if json_output:
        _echo_json(response["record"])
    else:
        _echo_item_markdown(response)


@app.command("update")
@convey_cli
def update_record_command(
    span_id: str = typer.Argument(help="Activity record ID."),
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET).",
    ),
    day: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day in YYYYMMDD format (or set SOL_DAY).",
    ),
    note: str | None = typer.Option(None, "--note", help="Edit note."),
    title: str | None = typer.Option(
        None,
        "--title",
        help="Activity title (argv mode).",
    ),
    description: str | None = typer.Option(
        None,
        "--description",
        help="One-line description (argv mode).",
    ),
    details: str | None = typer.Option(
        None,
        "--details",
        help="Longer details (argv mode).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Apply an argv flag or stdin JSON patch to one activity record."""
    resolved_facet = _resolve_sol_facet(facet)
    resolved_day = _resolve_sol_day(day)

    payload_flags_supplied = any(
        value is not None for value in (title, description, details)
    )
    if payload_flags_supplied:
        patch: dict[str, object] = {}
        if title is not None:
            patch["title"] = title
        if description is not None:
            patch["description"] = description
        if details is not None:
            patch["details"] = details
    else:
        payload = _read_stdin_json(allow_empty=True)
        patch = {
            key: value
            for key, value in payload.items()
            if key in {"title", "description", "details"}
        }
        if set(payload) - set(patch):
            extra = ", ".join(sorted(set(payload) - set(patch)))
            typer.echo(f"Error: disallowed update fields: {extra}", err=True)
            raise typer.Exit(1)

    if not patch:
        typer.echo(
            "Error: update payload must include at least one mutable field.", err=True
        )
        raise typer.Exit(1)

    note_text = note or f"updated fields: {', '.join(sorted(patch))}"
    try:
        body = get_client().request(
            "POST",
            f"/app/activities/api/day/{resolved_day}/record/{span_id}/update",
            params={"facet": resolved_facet},
            json={"patch": patch, "note": note_text},
        )
    except ConveyClientError as err:
        if err.reason_code == ACTIVITY_NOT_FOUND.code:
            typer.echo(f"activity not found: {span_id}", err=True)
            raise typer.Exit(1) from err
        raise

    if json_output:
        _echo_json(body["record"])
    else:
        _echo_item_markdown(body)


def _set_mute_state(
    span_id: str,
    *,
    facet: str | None,
    day: str | None,
    reason: str | None,
    json_output: bool,
    verb: str,
) -> None:
    resolved_facet = _resolve_sol_facet(facet)
    resolved_day = _resolve_sol_day(day)
    try:
        body = get_client().request(
            "POST",
            f"/app/activities/api/day/{resolved_day}/record/{span_id}/{verb}",
            params={"facet": resolved_facet},
            json={"reason": reason},
        )
    except ConveyClientError as err:
        if err.reason_code == ACTIVITY_NOT_FOUND.code:
            typer.echo(f"activity not found: {span_id}", err=True)
            raise typer.Exit(1) from err
        raise

    if json_output:
        _echo_json(body["record"])
    else:
        _echo_item_markdown(body)


@app.command("mute")
@convey_cli
def mute_record(
    span_id: str = typer.Argument(help="Activity record ID."),
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET).",
    ),
    day: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day in YYYYMMDD format (or set SOL_DAY).",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Mute reason."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Hide an activity record without deleting it."""
    _set_mute_state(
        span_id,
        facet=facet,
        day=day,
        reason=reason,
        json_output=json_output,
        verb="mute",
    )


@app.command("unmute")
@convey_cli
def unmute_record(
    span_id: str = typer.Argument(help="Activity record ID."),
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET).",
    ),
    day: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day in YYYYMMDD format (or set SOL_DAY).",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Unmute reason."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Restore a previously hidden activity record."""
    _set_mute_state(
        span_id,
        facet=facet,
        day=day,
        reason=reason,
        json_output=json_output,
        verb="unmute",
    )

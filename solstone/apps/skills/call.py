# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for owner-wide skill patterns and edit requests.

Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
Auto-discovered by ``think.call`` and mounted as ``sol call skills ...``.
"""

from __future__ import annotations

import json
from typing import Any

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Owner-wide skill patterns and edit requests.")

_PATTERNS_PAGE_SIZE = 100


def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def _parse_activity_ids(raw_value: str) -> list[str]:
    activity_ids = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not activity_ids:
        typer.echo("Error: --activity-ids requires at least one id.", err=True)
        raise typer.Exit(1) from None
    return activity_ids


def _fetch_all_patterns(status: str | None) -> list[dict[str, Any]]:
    client = get_client()
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {"limit": _PATTERNS_PAGE_SIZE, "offset": offset}
        if status is not None:
            params["status"] = status
        body = client.request("GET", "/app/skills/api/patterns", params=params)
        items = body["items"]
        rows.extend(items)
        if not items or len(rows) >= int(body["total"]):
            return rows
        offset += len(items)


def _emit_pattern_result(
    pattern: dict[str, Any], *, json_output: bool, text_message: str
) -> None:
    if json_output:
        _echo_json(pattern)
        return
    typer.echo(text_message)


@app.command("list")
@convey_cli
def list_skills(
    status: str | None = typer.Option(
        None,
        "--status",
        help="Filter by one status or a comma-separated list of statuses.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List owner-wide skill patterns."""
    rows = _fetch_all_patterns(status)

    if json_output:
        _echo_json(rows)
        return

    for row in rows:
        slug = str(row.get("slug") or "")[:40]
        status_value = str(row.get("status") or "")[:10]
        observations = row.get("observations", [])
        last_seen = str(row.get("last_seen") or "")
        facets = ",".join(str(item) for item in row.get("facets_touched", []))
        typer.echo(
            f"{slug:<40} {status_value:<10} "
            f"obs={len(observations):<3} last={last_seen} facets={facets}"
        )


@app.command("show")
@convey_cli
def show_skill(
    slug: str = typer.Argument(help="Skill slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show one owner-wide skill pattern and its profile."""
    body = get_client().request("GET", f"/app/skills/api/patterns/{slug}")
    pattern = body["pattern"]
    profile = body["profile"]
    if json_output:
        _echo_json(body)
        return

    typer.echo(f"name: {pattern.get('name', '')}")
    typer.echo(f"slug: {pattern.get('slug', '')}")
    typer.echo(f"status: {pattern.get('status', '')}")
    typer.echo(f"first_seen: {pattern.get('first_seen', '')}")
    typer.echo(f"last_seen: {pattern.get('last_seen', '')}")
    typer.echo(f"obs_count: {len(pattern.get('observations', []))}")
    typer.echo(f"facets_touched: {','.join(pattern.get('facets_touched', []))}")
    observations = sorted(
        pattern.get("observations", []),
        key=lambda observation: (
            str(observation.get("day", "")),
            str(observation.get("recorded_at", "")),
        ),
    )
    for observation in observations:
        activity_ids = ",".join(
            str(item) for item in observation.get("activity_ids", [])
        )
        notes = str(observation.get("notes") or "")
        typer.echo(
            f"- {observation.get('day', '')} [{observation.get('facet', '')}] "
            f"activity_ids={activity_ids} notes={notes}"
        )
    if profile is not None:
        typer.echo("---")
        typer.echo(profile.rstrip("\n"))


@app.command("observe")
@convey_cli
def observe_skill(
    slug: str = typer.Argument(help="Skill slug."),
    day: str = typer.Option(..., "--day", help="Observation day in YYYY-MM-DD format."),
    facet: str = typer.Option(..., "--facet", help="Facet name."),
    activity_ids: str = typer.Option(
        ...,
        "--activity-ids",
        help="Comma-separated activity ids.",
    ),
    notes: str = typer.Option("", "--notes", help="Optional observation notes."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Record one new observation for an existing skill."""
    normalized_activity_ids = _parse_activity_ids(activity_ids)
    pattern = get_client().request(
        "POST",
        f"/app/skills/api/patterns/{slug}/observations",
        json={
            "day": day,
            "facet": facet,
            "activity_ids": normalized_activity_ids,
            "notes": notes,
        },
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"observation saved: {slug}",
    )


@app.command("seed")
@convey_cli
def seed_skill(
    slug: str = typer.Argument(help="Skill slug."),
    name: str = typer.Option(..., "--name", help="Human-readable skill name."),
    day: str = typer.Option(..., "--day", help="Observation day in YYYY-MM-DD format."),
    facet: str = typer.Option(..., "--facet", help="Facet name."),
    activity_ids: str = typer.Option(
        ...,
        "--activity-ids",
        help="Comma-separated activity ids.",
    ),
    notes: str = typer.Option("", "--notes", help="Optional observation notes."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Seed one new emerging skill pattern."""
    normalized_activity_ids = _parse_activity_ids(activity_ids)
    pattern = get_client().request(
        "POST",
        "/app/skills/api/patterns",
        json={
            "slug": slug,
            "name": name,
            "day": day,
            "facet": facet,
            "activity_ids": normalized_activity_ids,
            "notes": notes,
        },
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"created skill: {slug}",
    )


@app.command("promote")
@convey_cli
def promote_skill(
    slug: str = typer.Argument(help="Skill slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Flag one skill for profile generation."""
    pattern = get_client().request(
        "POST", f"/app/skills/api/patterns/{slug}/promote", json={}
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"flagged for profile: {slug}",
    )


@app.command("refresh")
@convey_cli
def refresh_skill(
    slug: str = typer.Argument(help="Skill slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Flag one mature skill for profile refresh."""
    pattern = get_client().request(
        "POST", f"/app/skills/api/patterns/{slug}/refresh", json={}
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"flagged for refresh: {slug}",
    )


@app.command("mark-dormant")
@convey_cli
def mark_dormant_skill(
    slug: str = typer.Argument(help="Skill slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Mark one skill dormant."""
    pattern = get_client().request(
        "POST", f"/app/skills/api/patterns/{slug}/mark-dormant", json={}
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"marked dormant: {slug}",
    )


@app.command("retire")
@convey_cli
def retire_skill(
    slug: str = typer.Argument(help="Skill slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Mark one skill retired."""
    pattern = get_client().request(
        "POST", f"/app/skills/api/patterns/{slug}/retire", json={}
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"retired skill: {slug}",
    )


@app.command("edit-request")
@convey_cli
def edit_request_skill(
    slug: str = typer.Argument(help="Skill slug."),
    instructions: str = typer.Option(..., "--instructions", help="Edit instructions."),
    requested_by: str = typer.Option("chat", "--requested-by", help="Request source."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Append one owner-authored edit request for a skill."""
    body = get_client().request(
        "POST",
        f"/app/skills/api/patterns/{slug}/edit-requests",
        json={"instructions": instructions, "requested_by": requested_by},
    )
    if json_output:
        _echo_json({"request_id": body["request_id"], "slug": body["slug"]})
        return
    typer.echo(f"request_id: {body['request_id']}")


@app.command("rename")
@convey_cli
def rename_skill(
    old_slug: str = typer.Argument(help="Existing skill slug."),
    new_slug: str = typer.Argument(help="New skill slug."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Rename one skill slug and move its profile if present."""
    pattern = get_client().request(
        "POST",
        f"/app/skills/api/patterns/{old_slug}/rename",
        json={"new_slug": new_slug},
    )
    _emit_pattern_result(
        pattern,
        json_output=json_output,
        text_message=f"renamed skill: {new_slug}",
    )

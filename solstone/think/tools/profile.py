# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from typing import Any, Callable, NoReturn
from urllib.parse import quote

import typer

from solstone.convey.reasons import ENTITY_NOT_FOUND
from solstone.think.convey_client import (
    ConveyClientError,
    get_client,
    paginate_collection,
)

app = typer.Typer(help="Profile consumer surface.", no_args_is_help=True)


def _params(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _exit_with(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _handle_profile_error(err: ConveyClientError, *, name: str) -> NoReturn:
    if err.reason_code == ENTITY_NOT_FOUND.code or err.status == 404:
        _exit_with(f"profile not found: {name}")
    _exit_with(err.detail or err.error)


def _render_table(
    items: list[Any], columns: list[tuple[str, Callable[[Any], str]]]
) -> None:
    if not items:
        return
    headers = [header for header, _ in columns]
    rows = [[getter(item) for _, getter in columns] for item in items]
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    typer.echo(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    )
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo(
            "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        )


def _item_summary(item: dict) -> str:
    if item["counterparty"]:
        return f"{item['owner']}: {item['summary']} -> {item['counterparty']}"
    return f"{item['owner']}: {item['summary']}"


def _render_full(profile: dict) -> None:
    facets_label = ",".join(profile["facets"]) if profile["facets"] else "-"
    typer.echo(
        f"{profile['name']} · {profile['type']} · "
        f"facets={facets_label} · self={profile['is_self']}"
    )
    typer.echo("")
    cadence = profile["cadence"]
    typer.echo("Cadence:")
    typer.echo(f"  last_seen: {cadence['last_seen']}")
    typer.echo(
        f"  recent_interactions_count_30d: {cadence['recent_interactions_count_30d']}"
    )
    typer.echo(f"  avg_interval_days: {cadence['avg_interval_days']}")
    typer.echo(f"  gone_quiet_since: {cadence['gone_quiet_since']}")
    typer.echo("")

    typer.echo("Open loops")
    if profile["open_with_them"]:
        _render_table(
            list(profile["open_with_them"]),
            [
                ("id", lambda item: item["id"]),
                ("state", lambda item: item["state"]),
                ("age_days", lambda item: str(item["age_days"])),
                ("summary", _item_summary),
                ("when", lambda item: item["when"] or ""),
            ],
        )
    else:
        typer.echo("No open loops.")
    typer.echo("")

    typer.echo("Closed 30d")
    if profile["closed_with_them_30d"]:
        _render_table(
            list(profile["closed_with_them_30d"]),
            [
                ("id", lambda item: item["id"]),
                ("closed_at", lambda item: str(item["closed_at"] or "")),
                ("summary", _item_summary),
            ],
        )
    else:
        typer.echo("No closed items.")
    typer.echo("")

    typer.echo("Decisions")
    if profile["decisions_involving_them"]:
        _render_table(
            list(profile["decisions_involving_them"]),
            [
                ("id", lambda item: item["id"]),
                ("day", lambda item: item["day"]),
                ("owner", lambda item: item["owner"]),
                ("action", lambda item: item["action"]),
                ("context", lambda item: item["context"]),
            ],
        )
    else:
        typer.echo("No decisions.")


def _render_brief(profile_brief: dict) -> None:
    typer.echo(f"entity_id: {profile_brief['entity_id']}")
    typer.echo(f"name: {profile_brief['name']}")
    typer.echo(f"type: {profile_brief['type']}")
    typer.echo(f"description: {profile_brief['description']}")
    typer.echo(f"last_seen: {profile_brief['last_seen']}")
    typer.echo(f"open_loop_count: {profile_brief['open_loop_count']}")
    typer.echo(f"decisions_count_30d: {profile_brief['decisions_count_30d']}")


def _render_cadence(cadence: dict) -> None:
    typer.echo(
        f"recent_interactions_count_30d: {cadence['recent_interactions_count_30d']}"
    )
    typer.echo(f"last_seen: {cadence['last_seen']}")
    typer.echo(f"avg_interval_days: {cadence['avg_interval_days']}")
    typer.echo(f"gone_quiet_since: {cadence['gone_quiet_since']}")


@app.command("full")
def full_cmd(
    name: str,
    facets: str | None = typer.Option(None, "--facets"),
    include_mentions: bool = typer.Option(False, "--include-mentions"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    try:
        profile = get_client().request(
            "GET",
            f"/api/profile/{quote(name, safe='')}",
            params=_params(
                facets=facets,
                include_mentions="true" if include_mentions else None,
            ),
        )
    except ConveyClientError as err:
        _handle_profile_error(err, name=name)
    if json_out:
        typer.echo(json.dumps(profile))
        return
    _render_full(profile)


@app.command("brief")
def brief_cmd(name: str, json_out: bool = typer.Option(False, "--json")) -> None:
    try:
        profile_brief = get_client().request(
            "GET", f"/api/profile/{quote(name, safe='')}/brief"
        )
    except ConveyClientError as err:
        _handle_profile_error(err, name=name)
    if json_out:
        typer.echo(json.dumps(profile_brief))
        return
    _render_brief(profile_brief)


@app.command("cadence")
def cadence_cmd(
    name: str,
    include_mentions: bool = typer.Option(False, "--include-mentions"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    try:
        cadence = get_client().request(
            "GET",
            f"/api/profile/{quote(name, safe='')}/cadence",
            params=_params(include_mentions="true" if include_mentions else None),
        )
    except ConveyClientError as err:
        _handle_profile_error(err, name=name)
    if json_out:
        typer.echo(json.dumps(cadence))
        return
    _render_cadence(cadence)


@app.command("list-active")
def list_active(
    window_days: int = typer.Option(30, "--window-days"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    try:
        ids = paginate_collection(
            get_client(),
            "/api/profiles/active",
            params={"window_days": window_days},
        )
    except ConveyClientError as err:
        _exit_with(err.detail or err.error)
    if json_out:
        typer.echo(json.dumps(ids))
        return
    for entity_id in ids:
        typer.echo(entity_id)

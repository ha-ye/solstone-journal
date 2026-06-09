# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from typing import NoReturn

import typer

from solstone.convey.reasons import ACTIVITIES_BUSY, LEDGER_ITEM_NOT_FOUND
from solstone.think.convey_client import (
    ConveyClientError,
    get_client,
    paginate_collection,
)

app = typer.Typer(help="Ledger: commitments ↔ closures view", no_args_is_help=True)


def _params(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _exit_with(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _handle_ledger_error(
    err: ConveyClientError,
    *,
    item_id: str | None = None,
) -> NoReturn:
    if err.reason_code == LEDGER_ITEM_NOT_FOUND.code:
        _exit_with(f"ledger item not found: {item_id}")
    if err.reason_code == ACTIVITIES_BUSY.code:
        _exit_with(ACTIVITIES_BUSY.message)
    _exit_with(err.detail or err.error)


def _render_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        return
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


def _render_items(items: list[dict]) -> None:
    if not items:
        typer.echo("No ledger items found.")
        return
    rows = [
        [
            item["id"],
            item["state"],
            str(item["age_days"]),
            _item_summary(item),
            item["when"] or "",
            str(item["opened_at"]),
            str(item["closed_at"] or ""),
        ]
        for item in items
    ]
    _render_table(
        ["id", "state", "age_days", "summary", "when", "opened_at", "closed_at"],
        rows,
    )


def _render_decisions(items: list[dict]) -> None:
    if not items:
        typer.echo("No decisions found.")
        return
    rows = [
        [item["id"], item["day"], item["owner"], item["action"], item["context"]]
        for item in items
    ]
    _render_table(["id", "day", "owner", "action", "context"], rows)


@app.command("list")
def list_cmd(
    state: str = typer.Option("open"),
    owner: str | None = typer.Option(None),
    counterparty: str | None = typer.Option(None),
    age_days_gte: int | None = typer.Option(None, "--age-days-gte"),
    closed_since: str | None = typer.Option(None, "--closed-since"),
    top: int | None = typer.Option(None, "--top"),
    sort: str | None = typer.Option(None),
    facets: str | None = typer.Option(None, help="csv"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List ledger items."""
    if sort is not None and sort not in {
        "age_days_desc",
        "opened_at_desc",
        "closed_at_desc",
    }:
        raise typer.BadParameter(
            "sort must be one of age_days_desc, opened_at_desc, closed_at_desc"
        )
    try:
        items = paginate_collection(
            get_client(),
            "/api/ledger",
            params=_params(
                state=state,
                owner=owner,
                counterparty=counterparty,
                age_days_gte=age_days_gte,
                closed_since=closed_since,
                sort=sort,
                facets=facets,
            ),
            top=top,
        )
    except ConveyClientError as err:
        _handle_ledger_error(err)
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    _render_items(items)


@app.command("get")
def get_cmd(item_id: str, json_out: bool = typer.Option(False, "--json")) -> None:
    """Fetch one ledger item."""
    try:
        item = get_client().request("GET", f"/api/ledger/{item_id}")
    except ConveyClientError as err:
        _handle_ledger_error(err, item_id=item_id)
    if json_out:
        typer.echo(json.dumps([item], indent=2))
        return
    _render_items([item])


@app.command("close")
def close_cmd(
    item_id: str,
    note: str = typer.Option(..., "--note"),
    as_state: str = typer.Option("closed", "--as"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Manually close or drop one ledger item."""
    if as_state not in {"closed", "dropped"}:
        raise typer.BadParameter("as_state must be 'closed' or 'dropped'")
    try:
        item = get_client().request(
            "POST",
            f"/api/ledger/{item_id}/close",
            json={"note": note, "as_state": as_state},
        )
    except ConveyClientError as err:
        _handle_ledger_error(err, item_id=item_id)
    if json_out:
        typer.echo(json.dumps([item], indent=2))
        return
    _render_items([item])


@app.command("decisions")
def decisions_cmd(
    owner: str | None = typer.Option(None),
    since: str | None = typer.Option(None),
    involving: str | None = typer.Option(None),
    top: int | None = typer.Option(None),
    facets: str | None = typer.Option(None, help="csv"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List deduplicated decisions."""
    try:
        items = paginate_collection(
            get_client(),
            "/api/ledger/decisions",
            params=_params(
                owner=owner, since=since, involving=involving, facets=facets
            ),
            top=top,
        )
    except ConveyClientError as err:
        _handle_ledger_error(err)
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    _render_decisions(items)

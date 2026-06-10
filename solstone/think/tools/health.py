# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import NoReturn, Optional

import typer

from solstone.convey.readiness_snapshot import highest_severity_group
from solstone.think.convey_client import ConveyClientError, get_client
from solstone.think.pipeline_health import summarize_pipeline_day

app = typer.Typer(
    help="Health: journal-data trust signals (for infrastructure/service liveness, use `journal health`).",
    no_args_is_help=True,
)


def _params(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _exit_with(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _handle_health_error(err: ConveyClientError) -> NoReturn:
    _exit_with(err.detail or err.error)


def _dash(value: object) -> object:
    return "—" if value is None else value


def _render_summary(report: dict) -> None:
    capture = report["capture_health"]
    synthesis = report["synthesis_health"]
    consumer_signal = report["consumer_signal"]

    typer.echo(f"Range: {report['range'][0]} -> {report['range'][1]}")
    typer.echo("Capture")
    typer.echo(f"  hours_with_capture: {capture['hours_with_capture']}")
    typer.echo(f"  hours_total: {capture['hours_total']}")
    typer.echo(f"  coverage_ratio: {_dash(capture['coverage_ratio'])}")
    typer.echo(
        "  facets_with_recent_capture: "
        + ", ".join(capture["facets_with_recent_capture"])
    )
    typer.echo("  facets_silent_24h: " + ", ".join(capture["facets_silent_24h"]))
    typer.echo(f"  last_segment_at: {_dash(capture['last_segment_at'])}")
    typer.echo("Synthesis")
    typer.echo(f"  activities_count: {synthesis['activities_count']}")
    typer.echo(
        "  activities_with_participation: "
        + str(synthesis["activities_with_participation"])
    )
    typer.echo(f"  activities_with_story: {synthesis['activities_with_story']}")
    typer.echo(f"  activities_user_edited: {synthesis['activities_user_edited']}")
    typer.echo(
        "  activities_anticipated_unfilled: "
        + str(synthesis["activities_anticipated_unfilled"])
    )
    typer.echo(
        "  talent_run_failures_24h: " + str(_dash(synthesis["talent_run_failures_24h"]))
    )
    typer.echo(
        "  talent_degraded_outputs_24h: "
        + str(_dash(synthesis["talent_degraded_outputs_24h"]))
    )
    typer.echo(
        "  indexer_last_rebuild_at: " + str(_dash(synthesis["indexer_last_rebuild_at"]))
    )
    backlog = report["segment_backlog"]
    n = backlog["not_thought"]
    m = backlog["days_with_backlog"]
    seg_word = "segment" if n == 1 else "segments"
    day_word = "day" if m == 1 else "days"
    if backlog["errors"] and n > 0:
        typer.echo(
            f"  at least {n} {seg_word} across {m} {day_word} "
            "awaiting thinking (status incomplete)"
        )
    elif backlog["errors"]:
        typer.echo("  Segment thinking status unavailable")
    elif n > 0:
        typer.echo(f"  {n} {seg_word} across {m} {day_word} awaiting thinking")
    typer.echo("Consumer Signals")
    typer.echo(
        f"  ledger_open_items_total: {consumer_signal['ledger_open_items_total']}"
    )
    typer.echo(
        f"  ledger_stale_items_count: {consumer_signal['ledger_stale_items_count']}"
    )
    typer.echo(f"  profile_entities_total: {consumer_signal['profile_entities_total']}")
    snap = report["provider_readiness"]
    typer.echo("Provider Readiness")
    if snap.get("unavailable"):
        typer.echo("  readiness status unavailable")
    elif snap.get("summary", {}).get("active_groups", 0) == 0:
        summary = snap.get("summary", {})
        if summary.get("status") == "ready" or summary.get("severity") == "ok":
            typer.echo("  all providers ready")
        else:
            typer.echo("  no active provider blockers")
    else:
        summary = snap.get("summary", {})
        active = summary.get("active_groups", 0)
        group_word = "provider group" if active == 1 else "provider groups"
        top = highest_severity_group(snap)
        if top is not None:
            typer.echo(f"  [{top.get('severity')}] {top.get('summary')}")
        typer.echo(f"  {active} {group_word} need attention")
        recovery = (top or {}).get("recovery_action")
        if isinstance(recovery, dict) and recovery.get("label"):
            href = recovery.get("href")
            if href:
                typer.echo(f"  → {recovery['label']}: {href}")
            else:
                typer.echo(f"  → {recovery['label']}")
    typer.echo("Notes")
    if not report["notes"]:
        typer.echo("  none")
        return
    for note in report["notes"]:
        typer.echo(f"  [{note['severity']}] {note['category']}: {note['message']}")


def _render_full(report: dict) -> None:
    _render_summary(report)
    if not report["capture_health"]["facets_silent_24h"]:
        return

    typer.echo("Silent Facet Detail")
    for facet in report["capture_health"]["facets_silent_24h"]:
        matching = [
            note
            for note in report["notes"]
            if note["category"] == "capture" and note["message"].startswith(f"{facet}:")
        ]
        if not matching:
            continue
        for note in matching:
            typer.echo(f"  {facet}: [{note['severity']}] {note['message']}")


@app.command("summary")
def summary(
    day: str | None = typer.Option(None, "--day"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Summarize journal-data trust signals for one day."""
    try:
        report = get_client().request(
            "GET", "/api/health/summary", params=_params(day=day)
        )
    except ConveyClientError as err:
        _handle_health_error(err)
    if json_out:
        typer.echo(json.dumps(report, indent=2))
        return
    _render_summary(report)


@app.command("full")
def full(
    day: str | None = typer.Option(None, "--day"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Render the full journal-data trust report for one day."""
    try:
        report = get_client().request(
            "GET", "/api/health/full", params=_params(day=day)
        )
    except ConveyClientError as err:
        _handle_health_error(err)
    if json_out:
        typer.echo(json.dumps(report, indent=2))
        return
    _render_full(report)


@app.command("for-range")
def for_range(
    day_from: str | None = typer.Option(None, "--day-from"),
    day_to: str | None = typer.Option(None, "--day-to"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Render the journal-data trust report for an inclusive day range."""
    try:
        report = get_client().request(
            "GET",
            "/api/health/range",
            params=_params(day_from=day_from, day_to=day_to),
        )
    except ConveyClientError as err:
        _handle_health_error(err)
    if json_out:
        typer.echo(json.dumps(report, indent=2))
        return
    _render_full(report)


@app.command(
    "pipeline",
    help="Thin wrapper around think.pipeline_health. For journal-data trust checks use `summary` / `full` / `for-range`.",
)
def pipeline(
    day: Optional[str] = typer.Option(
        None, "--day", help="Day to summarize (YYYYMMDD)."
    ),
    yesterday: bool = typer.Option(
        False, "--yesterday", help="Summarize yesterday's pipeline."
    ),
) -> None:
    """Summarize think pipeline health for one day."""
    if day is not None and yesterday:
        typer.echo("--day and --yesterday are mutually exclusive", err=True)
        raise typer.Exit(1)

    if day is not None:
        target = day
    elif yesterday:
        target = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    else:
        target = datetime.now().strftime("%Y%m%d")

    summary = summarize_pipeline_day(target)
    typer.echo(json.dumps(summary, indent=2, sort_keys=False))

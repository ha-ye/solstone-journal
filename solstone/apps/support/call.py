# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for solstone support.

Auto-discovered by ``think.call`` and mounted as ``sol call support ...``.

Subcommands provide full access to the support portal: registration, KB search,
ticket management, feedback, announcements, and local diagnostics.
"""

from __future__ import annotations

import functools
import json
import platform
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer

from solstone.think.convey_client import (
    ConveyClient,
    ConveyClientError,
    ConveyUnreachableError,
)

app = typer.Typer(help="Support tools — file tickets, search KB, give feedback.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_client() -> ConveyClient:
    return ConveyClient(require_service=False)


def _emit_unreachable_notice() -> None:
    typer.echo(
        "I couldn't reach support because solstone isn't reachable right now.",
        err=True,
    )
    typer.echo("To file a support ticket, visit https://support.solstone.app", err=True)


def _support_cli(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConveyUnreachableError:
            _emit_unreachable_notice()
            raise typer.Exit(1) from None
        except ConveyClientError as err:
            typer.echo(err.error, err=True)
            raise typer.Exit(1) from err

    return wrapper


def _json_out(data: object) -> None:
    """Pretty-print JSON to stdout."""
    typer.echo(json.dumps(data, indent=2, default=str))


def _check_enabled(client: ConveyClient) -> dict:
    config = client.request("GET", "/app/support/api/config")
    if not config.get("enabled"):
        typer.echo("Support agent is disabled in settings.", err=True)
        raise typer.Exit(1)
    return config


def _print_dry_run_preview(
    *,
    subject: str,
    product: str,
    severity: str,
    category: str | None,
    body: str,
    diagnostics: dict,
    portal_url: str,
) -> None:
    """Print the would-be ticket payload for a dry run. No network I/O."""
    version = diagnostics.get("version") or "unknown"
    revision = diagnostics.get("revision") or "none"
    typer.echo(
        "DRY RUN — nothing was sent. Re-run with --submit to actually file this."
    )
    typer.echo(f"Build identity — version: {version}  revision: {revision}")
    typer.echo("\n--- Would send ---")
    typer.echo(f"Subject:     {subject}")
    typer.echo(f"Product:     {product}")
    typer.echo(f"Severity:    {severity}")
    if category:
        typer.echo(f"Category:    {category}")
    typer.echo(f"Body:        {body}")
    typer.echo(f"\nuser_context ({len(json.dumps(diagnostics, default=str))} bytes):")
    typer.echo(json.dumps(diagnostics, indent=2, default=str))
    typer.echo(f"\nWould POST to: {portal_url}")
    typer.echo("--- End dry run ---")


def _capture_draft(
    client: ConveyClient,
    *,
    verb: str,
    payload: dict,
    diagnostics_snapshot: dict | None,
) -> None:
    """POST the exact submit-path payload to the dormant draft-capture endpoint.

    Non-fatal: a capture failure prints a notice and returns; it never raises, so a
    dry-run / no-network reply stays infallible. ConveyUnreachableError is a
    ConveyClientError subclass, so the single except covers unreachable too.
    """
    try:
        client.request(
            "POST",
            "/app/support/api/draft",
            json={
                "verb": verb,
                "payload": payload,
                "diagnostics_snapshot": diagnostics_snapshot,
            },
        )
    except ConveyClientError:
        typer.echo(
            "(Draft not captured — solstone wasn't reachable to save it for review.)",
            err=True,
        )


def _local_build_identity() -> dict:
    try:
        ver = _pkg_version("solstone")
    except PackageNotFoundError:
        ver = None
    # parents[2] of solstone/apps/support/call.py is the solstone package dir;
    # git rev-parse walks up from there to the checkout .git. Never raises.
    package_dir = Path(__file__).parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=package_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        rev = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        rev = None
    return {
        "version": ver,
        "revision": rev,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("register")
@_support_cli
def register() -> None:
    """(Re-)register with the support portal."""
    client = get_client()
    _check_enabled(client)
    result = client.request("POST", "/app/support/api/register")
    typer.echo(f"Registered as: {result.get('handle', '?')}")


@app.command("search")
@_support_cli
def search(
    query: str = typer.Argument(..., help="Search query for KB articles."),
) -> None:
    """Search knowledge base articles."""
    client = get_client()
    _check_enabled(client)
    articles = client.request("GET", "/app/support/api/articles", params={"q": query})
    if not articles:
        typer.echo("No articles found.")
        return

    for a in articles:
        typer.echo(f"  [{a.get('slug', '?')}] {a.get('title', 'Untitled')}")
    typer.echo(
        f"\n{len(articles)} article(s) found. Use `sol call support article <slug>` to read."
    )


@app.command("article")
@_support_cli
def article(
    slug: str = typer.Argument(..., help="Article slug."),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Read a KB article."""
    client = get_client()
    _check_enabled(client)
    data = client.request("GET", f"/app/support/api/articles/{slug}")

    if as_json:
        _json_out(data)
    else:
        typer.echo(f"# {data.get('title', 'Untitled')}\n")
        typer.echo(data.get("content", "(no content)"))


@app.command("create")
@_support_cli
def create(
    subject: str = typer.Option(..., "--subject", "-s", help="Ticket subject."),
    description: str = typer.Option(
        ..., "--description", "-d", help="Ticket description."
    ),
    product: str = typer.Option("solstone", "--product", "-p", help="Product name."),
    severity: str = typer.Option(
        "medium", "--severity", help="low, medium, high, critical."
    ),
    category: str | None = typer.Option(
        None, "--category", help="bug, feature, question, account."
    ),
    skip_kb: bool = typer.Option(
        False, "--skip-kb", help="Skip KB search before filing."
    ),
    submit: bool = typer.Option(
        False,
        "--submit",
        help=(
            "Actually file the ticket with sol pbc. Without --submit this is a "
            "dry run and nothing is sent."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
    anonymous: bool = typer.Option(
        False, "--anonymous", help="Strip installation identifiers."
    ),
) -> None:
    """File a support ticket (KB-first flow with consent gate)."""
    client = get_client()
    config = _check_enabled(client)
    if not submit:
        diagnostics = client.request("GET", "/app/support/api/diagnostics")
        _print_dry_run_preview(
            subject=subject,
            product=product,
            severity=severity,
            category=category,
            body=description,
            diagnostics=diagnostics,
            portal_url=config["portal_url"],
        )
        _capture_draft(
            client,
            verb="create",
            payload={
                "subject": subject,
                "description": description,
                "product": product,
                "severity": severity,
                "category": category,
                "user_context": diagnostics,
                "auto_context": False,
                "anonymous": anonymous,
            },
            diagnostics_snapshot=diagnostics,
        )
        return

    # Step 1: KB-first — search before filing
    if not skip_kb:
        typer.echo("Searching knowledge base...")
        articles = client.request(
            "GET", "/app/support/api/articles", params={"q": subject}
        )
        if articles:
            typer.echo(f"\nFound {len(articles)} related article(s):")
            for a in articles:
                typer.echo(f"  [{a.get('slug', '?')}] {a.get('title', '')}")
            typer.echo(
                "\nThese may answer your question. "
                "Use `sol call support article <slug>` to read."
            )
            if not yes:
                proceed = typer.confirm("Still want to file a ticket?")
                if not proceed:
                    typer.echo("Cancelled.")
                    return

    # Step 2: Collect diagnostics
    diagnostics = client.request("GET", "/app/support/api/diagnostics")

    # Step 3: Present draft for review (consent gate)
    typer.echo("\n--- Ticket Draft ---")
    typer.echo(f"Subject:     {subject}")
    typer.echo(f"Product:     {product}")
    typer.echo(f"Severity:    {severity}")
    if category:
        typer.echo(f"Category:    {category}")
    typer.echo(f"Description: {description}")
    typer.echo(f"\nDiagnostic data ({len(json.dumps(diagnostics))} bytes):")
    typer.echo(json.dumps(diagnostics, indent=2, default=str))
    typer.echo("--- End Draft ---\n")

    if not yes:
        approved = typer.confirm("Submit this ticket?")
        if not approved:
            typer.echo("Cancelled — nothing was sent.")
            return

    # Step 4: Submit
    result = client.request(
        "POST",
        "/app/support/api/tickets",
        json={
            "subject": subject,
            "description": description,
            "product": product,
            "severity": severity,
            "category": category,
            "user_context": diagnostics,
            "auto_context": False,
            "anonymous": anonymous,
        },
    )
    typer.echo(f"Ticket created: #{result.get('id', '?')}")


@app.command("list")
@_support_cli
def list_tickets(
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """List your support tickets."""
    client = get_client()
    _check_enabled(client)
    params = {"status": status} if status else None
    tickets = client.request("GET", "/app/support/api/tickets", params=params)
    if as_json:
        _json_out(tickets)
        return

    if not tickets:
        typer.echo("No tickets found.")
        return

    for t in tickets:
        status_str = t.get("status", "?")
        typer.echo(
            f"  #{t.get('id', '?'):>4}  [{status_str:<12}] {t.get('subject', 'Untitled')}"
        )
    typer.echo(f"\n{len(tickets)} ticket(s).")


@app.command("show")
@_support_cli
def show(
    ticket_id: int = typer.Argument(..., help="Ticket ID."),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """View a ticket with its message thread."""
    client = get_client()
    _check_enabled(client)
    data = client.request("GET", f"/app/support/api/tickets/{ticket_id}")

    if as_json:
        _json_out(data)
        return

    typer.echo(f"# Ticket #{data.get('id', '?')}: {data.get('subject', '')}")
    typer.echo(
        f"Status: {data.get('status', '?')}  |  Severity: {data.get('severity', '?')}"
    )
    typer.echo(f"Created: {data.get('created_at', '?')}")
    typer.echo(f"\n{data.get('description', '')}")

    messages = data.get("messages", [])
    if messages:
        typer.echo(f"\n--- {len(messages)} message(s) ---")
        for msg in messages:
            handle = msg.get("handle", "?")
            typer.echo(f"\n[{handle}] {msg.get('created_at', '')}")
            typer.echo(msg.get("content", ""))
            attachments = msg.get("attachments", [])
            if attachments:
                for att in attachments:
                    size = att.get("size_bytes", 0)
                    if size >= 1024 * 1024:
                        size_str = f"{size / 1024 / 1024:.1f} MB"
                    elif size >= 1024:
                        size_str = f"{size / 1024:.0f} KB"
                    else:
                        size_str = f"{size} bytes"
                    typer.echo(f"  📎 {att.get('filename', '?')} ({size_str})")


@app.command("reply")
@_support_cli
def reply(
    ticket_id: int = typer.Argument(..., help="Ticket ID."),
    body: str = typer.Option(..., "--body", "-b", help="Reply content."),
    submit: bool = typer.Option(
        True,
        "--submit/--no-submit",
        help=(
            "Send the reply to solstone support (default). Pass --no-submit to "
            "capture a draft for review without contacting the portal."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Reply to a ticket."""
    client = get_client()
    _check_enabled(client)

    if not submit:
        # No-network capture: stash the exact submit body as a draft. The portal is
        # not contacted at all; nothing is sent.
        typer.echo(
            "DRY RUN — nothing was sent. Re-run with --submit to actually send this."
        )
        typer.echo(f"Reply to ticket #{ticket_id}:\n{body}")
        _capture_draft(
            client,
            verb="reply",
            payload={"ticket_id": ticket_id, "content": body},
            diagnostics_snapshot=None,
        )
        return

    if not yes:
        typer.echo(f"Reply to ticket #{ticket_id}:\n{body}\n")
        if not typer.confirm("Send this reply?"):
            typer.echo("Cancelled.")
            return

    client.request(
        "POST",
        f"/app/support/api/tickets/{ticket_id}/reply",
        json={"content": body},
    )
    typer.echo(f"Reply sent to ticket #{ticket_id}.")


@app.command("attach")
@_support_cli
def attach(
    ticket_id: int = typer.Argument(..., help="Ticket ID to attach files to."),
    files: list[Path] = typer.Argument(..., help="File(s) to attach."),
    submit: bool = typer.Option(
        True,
        "--submit/--no-submit",
        help=(
            "Upload the file(s) to solstone support (default). Pass --no-submit "
            "to prepare a single-file draft for review without contacting the portal."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Attach file(s) to a ticket."""
    client = get_client()
    _check_enabled(client)

    if not submit:
        if len(files) > 1:
            typer.echo(
                "Attach one file at a time when preparing a draft for review.",
                err=True,
            )
            raise typer.Exit(1)
        f = files[0]
        if not f.is_file():
            typer.echo(f"Error: file not found: {f}", err=True)
            raise typer.Exit(1)
        try:
            client.upload(
                "/app/support/api/draft",
                files={"file": (f.name, str(f), None)},
                data={"verb": "attach", "ticket_id": str(ticket_id)},
            )
        except ConveyUnreachableError:
            raise
        except ConveyClientError as err:
            typer.echo(err.detail or err.error, err=True)
            raise typer.Exit(1) from err

        size = f.stat().st_size
        if size >= 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.0f} KB"
        else:
            size_str = f"{size} bytes"
        typer.echo(
            "DRY RUN — nothing was sent. Re-run without --no-submit to upload this."
        )
        typer.echo(f"Attachment draft for ticket #{ticket_id}: {f.name} ({size_str})")
        return

    # Validate files up front
    for f in files:
        if not f.is_file():
            typer.echo(f"Error: file not found: {f}", err=True)
            raise typer.Exit(1)

    # Consent gate — show what will be uploaded
    typer.echo(f"\n--- Attachment Review (ticket #{ticket_id}) ---")
    for f in files:
        size = f.stat().st_size
        if size >= 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.0f} KB"
        else:
            size_str = f"{size} bytes"
        typer.echo(f"  {f.name}  ({size_str})")
    typer.echo("--- End Review ---\n")

    if not yes:
        approved = typer.confirm("Upload these files?")
        if not approved:
            typer.echo("Cancelled — nothing was sent.")
            return

    path = f"/app/support/api/tickets/{ticket_id}/attachments"
    for f in files:
        try:
            result = client.upload(path, files={"file": (f.name, str(f), None)})
        except ConveyUnreachableError:
            raise
        except ConveyClientError as err:
            typer.echo(f"Skipped {f.name}: {err.error}", err=True)
            continue
        typer.echo(f"Attached: {f.name} (id: {result.get('id', '?')})")


@app.command("feedback")
@_support_cli
def feedback(
    body: str = typer.Option(..., "--body", "-b", help="Your feedback."),
    product: str = typer.Option("solstone", "--product", "-p", help="Product name."),
    anonymous: bool = typer.Option(False, "--anonymous", help="Submit anonymously."),
    submit: bool = typer.Option(
        False,
        "--submit",
        help=(
            "Actually send the feedback to sol pbc. Without --submit this is a "
            "dry run and nothing is sent."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Submit feedback (lower friction than a full ticket)."""
    client = get_client()
    config = _check_enabled(client)
    if not submit:
        # Fixed display values mirror tools.support_feedback (source of truth):
        # subject="User feedback", severity="low", category="feedback".
        diagnostics = client.request("GET", "/app/support/api/diagnostics")
        _print_dry_run_preview(
            subject="User feedback",
            product=product,
            severity="low",
            category="feedback",
            body=body,
            diagnostics=diagnostics,
            portal_url=config["portal_url"],
        )
        _capture_draft(
            client,
            verb="feedback",
            payload={"body": body, "product": product, "anonymous": anonymous},
            diagnostics_snapshot=diagnostics,
        )
        return

    if not yes:
        typer.echo(f"Feedback:\n{body}\n")
        anon_note = " (anonymous)" if anonymous else ""
        if not typer.confirm(f"Submit this feedback{anon_note}?"):
            typer.echo("Cancelled.")
            return

    result = client.request(
        "POST",
        "/app/support/api/feedback",
        json={"body": body, "product": product, "anonymous": anonymous},
    )
    typer.echo(f"Feedback submitted: #{result.get('id', '?')}")


@app.command("announcements")
@_support_cli
def announcements(
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Check for product updates and known issues."""
    client = get_client()
    _check_enabled(client)
    items = client.request("GET", "/app/support/api/announcements")
    if as_json:
        _json_out(items)
        return

    if not items:
        typer.echo("No active announcements.")
        return

    for a in items:
        icon = {"known-issue": "⚠️", "maintenance": "🔧"}.get(a.get("type", ""), "📢")
        typer.echo(f"  {icon} {a.get('title', 'Untitled')}")
        if a.get("content"):
            typer.echo(f"     {a['content'][:120]}")
    typer.echo(f"\n{len(items)} announcement(s).")


@app.command("diagnose")
def diagnose(
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show journal-host diagnostics (read-only)."""
    client = get_client()
    try:
        data = client.request("GET", "/app/support/api/diagnostics")
    except ConveyUnreachableError:
        identity = _local_build_identity()
        if as_json:
            _json_out(identity)
        else:
            typer.echo("# Local Diagnostics\n")
            typer.echo(f"Version:  {identity.get('version') or 'unknown'}")
            typer.echo(f"Revision: {identity.get('revision') or 'none'}")
            plat = identity.get("platform", {})
            typer.echo(
                f"Platform: {plat.get('system', '?')} {plat.get('release', '')} "
                f"({plat.get('machine', '')})"
            )
            typer.echo(f"Python:   {plat.get('python', '?')}")
        _emit_unreachable_notice()
        raise typer.Exit(1) from None
    except ConveyClientError as err:
        typer.echo(err.error, err=True)
        raise typer.Exit(1) from err

    if as_json:
        _json_out(data)
    else:
        typer.echo("# Local Diagnostics\n")
        typer.echo(f"Version:  {data.get('version', 'unknown')}")
        plat = data.get("platform", {})
        typer.echo(
            f"Platform: {plat.get('system', '?')} {plat.get('release', '')} "
            f"({plat.get('machine', '')})"
        )
        typer.echo(f"Python:   {plat.get('python', '?')}")

        services = data.get("services", {})
        if services:
            typer.echo("\nServices:")
            for name, status in sorted(services.items()):
                icon = "✓" if status == "running" else "✗"
                typer.echo(f"  {icon} {name}: {status}")

        errors = data.get("recent_errors", [])
        if errors:
            typer.echo(f"\nRecent errors ({len(errors)}):")
            for e in errors:
                t = e.get("time", "")
                if t and e.get("time_approximate"):
                    t = "~" + t
                prefix = (t + " ") if t else ""
                typer.echo(
                    f"  {prefix}[{e.get('service', '?')}] {e.get('message', '')[:100]}"
                )
        else:
            typer.echo("\nNo recent errors.")

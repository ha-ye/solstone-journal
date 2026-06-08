# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for link pairing and paired devices.

Auto-discovered by ``think.call`` and mounted as ``sol call link ...``.
Every verb reaches the journal only over HTTP via the Convey client.
"""

import datetime as dt
import math
import socket
import time

import typer

from solstone.convey.reasons import PAIRED_DEVICE_NOT_FOUND
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

app = typer.Typer(
    help="Link — tunnel service for reaching this solstone from paired phones."
)

PAIR_TIMEOUT_SECONDS = 300
VALID_ROLES = {"phone", "observer", "peer"}
ROLE_HEADINGS = {
    "phone": "Phones:",
    "observer": "Observers:",
    "peer": "Peers:",
}


def _detect_lan_ip() -> str | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


def _plural(value: int, unit: str) -> str:
    return f"{value} {unit}{'s' if value != 1 else ''}"


def relative_time(seconds: int | float) -> str:
    """Return canonical human readable duration for ``seconds``."""
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0
    seconds = int(seconds)
    if seconds < 60:
        return _plural(seconds, "second")
    minutes = seconds // 60
    if minutes < 60:
        return _plural(minutes, "minute")
    hours = minutes // 60
    if hours < 24:
        return _plural(hours, "hour")
    days = hours // 24
    if days < 7:
        return _plural(days, "day")
    if days < 28:
        return _plural(days // 7, "week")
    if days < 60:
        return "1 month"
    return _plural(days // 30, "month")


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _relative_time(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return iso
    now = _now_utc()
    delta_seconds = max(0, (now - then).total_seconds())
    return f"{relative_time(delta_seconds)} ago"


@app.command()
@convey_cli
def pair(
    device_label: str = typer.Option(
        ..., "--device-label", help="Label for the phone being paired"
    ),
    as_role: str = typer.Option(
        "phone",
        "--as",
        help=(
            "Role tag stored with the pairing — identity metadata that future route "
            "handlers will key on (not just CLI grouping). One of: phone, observer, "
            "peer."
        ),
    ),
    convey_host: str = typer.Option(
        "",
        "--convey-host",
        help="Override host[:port] for the pair URL (default: auto-detect LAN IP)",
    ),
    convey_port: int = typer.Option(
        0,
        "--convey-port",
        help="Override convey port (default: read from service port file or 5015)",
    ),
    timeout_seconds: int = typer.Option(
        PAIR_TIMEOUT_SECONDS,
        "--timeout",
        help="How long to wait for the phone before giving up",
    ),
) -> None:
    """Mint a one-shot nonce, print the pair URL + QR-ready payload, wait for completion."""
    if as_role not in VALID_ROLES:
        typer.echo("invalid role; expected one of: phone, observer, peer", err=True)
        raise typer.Exit(2)

    client = get_client()
    mint = client.request(
        "POST",
        "/app/link/api/pair/mint",
        json={"device_label": device_label, "role": as_role},
    )
    value = mint["nonce"]
    manual_code = mint["manual_code"]
    ca_fp = mint["ca_fingerprint"]

    host = convey_host or _detect_lan_ip() or "localhost"
    port = convey_port or mint.get("port") or 5015
    url = f"http://{host}:{port}/app/link/pair?token={value}"

    typer.echo(f"Pair code: {value} (expires in 5 minutes)")
    typer.echo(f"manual code: {manual_code}")
    typer.echo(f"Pair URL: {url}")
    typer.echo(f"CA fingerprint: sha256:{ca_fp}")
    typer.echo(f"Device: {device_label} (role: {as_role})")
    typer.echo("")
    typer.echo("Waiting for phone…")

    before = {
        d["fingerprint"]
        for d in client.request("GET", "/app/link/api/devices")["devices"]
    }
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(1.0)
        devices = client.request("GET", "/app/link/api/devices")["devices"]
        new_entries = [d for d in devices if d["fingerprint"] not in before]
        if new_entries:
            entry = new_entries[-1]
            typer.echo(f"Paired: {entry['device_label']} (role: {entry['role']})")
            typer.echo(f"  fingerprint: {entry['fingerprint']}")
            typer.echo(f"  paired_at:   {entry['paired_at']}")
            raise typer.Exit(0)
        nonce_status = client.request(
            "GET",
            "/app/link/api/pair/nonce-status",
            params={"nonce": value},
        )
        if nonce_status["used"]:
            typer.echo(
                "Pair request completed; device should appear in `sol call link list`."
            )
            raise typer.Exit(0)
    typer.echo("Timed out. Pair code expired.")
    raise typer.Exit(2)


@app.command("list")
@convey_cli
def list_devices() -> None:
    """Print every paired device with its last-seen time."""
    devices = get_client().request("GET", "/app/link/api/devices")["devices"]
    if not devices:
        typer.echo("No devices linked yet.")
        return
    grouped = {role: [] for role in ROLE_HEADINGS}
    for device in devices:
        grouped.setdefault(device["role"], []).append(device)

    printed_section = False
    for role, heading in ROLE_HEADINGS.items():
        role_entries = grouped[role]
        if not role_entries:
            continue
        if printed_section:
            typer.echo("")
        typer.echo(heading)
        for device in role_entries:
            typer.echo(
                f"- {device['device_label']}"
                f" — added {_relative_time(device['paired_at'])}"
                f" — last seen {_relative_time(device['last_seen_at'])}"
                f" [{device['fingerprint_short']}]"
            )
        printed_section = True


@app.command()
@convey_cli
def unpair(
    target: str = typer.Argument(
        ..., help="Device label or fingerprint (sha256:<hex>)"
    ),
) -> None:
    """Revoke a paired device. Next reconnect from that device fails at TLS handshake."""
    payload = (
        {"fingerprint": target}
        if target.startswith("sha256:")
        else {"device_label": target}
    )
    try:
        get_client().request("POST", "/app/link/unpair", json=payload)
    except ConveyClientError as err:
        if err.reason_code == PAIRED_DEVICE_NOT_FOUND.code:
            if target.startswith("sha256:"):
                typer.echo(f"No paired device with fingerprint {target}")
            else:
                typer.echo(f"No paired device with label {target!r}")
            raise typer.Exit(1) from err
        raise
    typer.echo("Unpaired.")


@app.command()
@convey_cli
def status() -> None:
    """Report enrollment, listen-WS state, active tunnel count, relay endpoint."""
    client = get_client()
    state = client.request("GET", "/app/link/api/status")
    paired_count = len(client.request("GET", "/app/link/api/devices")["devices"])
    if state["instance_id"] is None:
        typer.echo("Instance ID:   (not provisioned — pair a device to provision)")
        typer.echo("Home label:    (not provisioned)")
    else:
        typer.echo(f"Instance ID:   {state['instance_id']}")
        typer.echo(f"Home label:    {state['home_label']}")
    typer.echo(f"Relay URL:     {state['relay_url']}")
    typer.echo(f"Enrolled:      {'yes' if state['enrolled'] else 'no'}")
    typer.echo(f"Paired devices: {paired_count}")
    typer.echo("Listen-WS state: (query convey /app/link/api/status for live state)")

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for link pairing and paired devices.

Auto-discovered by ``think.call`` and mounted as ``sol call link ...``.
Every verb reaches the journal only over HTTP via the Convey client.
"""

import datetime as dt
import math
import shlex
import time
from typing import Any

import typer

from solstone.convey.reasons import (
    INVALID_OPERATION_FOR_STATE,
    PAIRED_DEVICE_NOT_FOUND,
    PAIRING_REQUEST_INVALID,
    SERVICE_BUSY,
    SERVICE_OPERATION_FAILED,
)
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

app = typer.Typer(
    help="Link — tunnel service for reaching this solstone from linked systems."
)
private_link_app = typer.Typer(help="solstone private link — reach home from anywhere.")
app.add_typer(private_link_app, name="private-link")

PAIR_TIMEOUT_SECONDS = 300
VALID_ROLES = {"", "phone", "observer", "peer"}
LINKED_SYSTEMS_HEADING = "Linked systems:"
PEERS_HEADING = "Peers:"
PRIVATE_LINK_TERMINAL_PHASES = {"enabled", "revoked", "error", "needs_subscription"}
PRIVATE_LINK_SETTING_UP = "setting up solstone private link..."
PRIVATE_LINK_SETUP_SUCCESS = (
    "solstone private link is on. your devices can reach home from anywhere."
)
PRIVATE_LINK_SETUP_FAILED = "couldn't finish setting up solstone private link."
PRIVATE_LINK_BROWSER_FALLBACK = "couldn't open your browser. open this link to finish:"
PRIVATE_LINK_NEEDS_SUBSCRIPTION = (
    "private link needs an active subscription before it can turn on. "
    "your consent is saved; set one up, then enable private link again:"
)
PRIVATE_LINK_DISABLE_SUCCESS = (
    "solstone private link is off. devices connect directly again."
)
PRIVATE_LINK_DISABLE_FAILED = (
    "couldn't turn off solstone private link — it's still on. try again."
)
PRIVATE_LINK_NEEDS_REPAIR = "solstone private link needs setting up again."
PRIVATE_LINK_STATE_LABELS = {
    "enabled": "enabled",
    "not_enabled": "not enabled",
    "inconsistent": "needs repair",
}
CLI_PAIR_LINK_LABEL = "pair-link"
CLI_PAIR_JOIN_HINT = "link this device with:"
CLI_PAIR_CA_FINGERPRINT_LABEL = "CA fingerprint"
CLI_PAIR_NO_LAN_ADDRESS = (
    "can't start pairing — your solstone isn't reachable on a network address "
    "yet. turn on solstone private link to pair from anywhere, or connect this "
    "device to your home network."
)


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


def _exit_with(message: str, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _private_link_state_label(state: Any) -> str:
    return PRIVATE_LINK_STATE_LABELS.get(str(state or ""), str(state or "unknown"))


def _get_private_link_status() -> dict[str, Any]:
    return get_client().request("GET", "/app/link/api/private-link")


def _post_private_link(path: str) -> dict[str, Any]:
    try:
        return get_client().request("POST", path)
    except ConveyClientError as err:
        if err.reason_code == INVALID_OPERATION_FOR_STATE.code and err.detail:
            _exit_with(err.detail)
        if err.reason_code == SERVICE_BUSY.code:
            _exit_with(err.detail or "operation already running")
        raise


def _maybe_echo_private_link_portal(
    operation: dict[str, Any],
    *,
    already_echoed: bool,
) -> bool:
    if already_echoed:
        return True
    if operation.get("browser_open_succeeded") is not False:
        return False
    portal_url = operation.get("portal_url")
    if not portal_url:
        return False
    typer.echo(f"{PRIVATE_LINK_BROWSER_FALLBACK} {portal_url}")
    return True


def _poll_private_link_until_terminal(
    *,
    wait_seconds: float,
    poll_interval: float,
) -> tuple[dict[str, Any], str | None, str | None]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    interval = max(0.0, poll_interval)
    portal_echoed = False

    while True:
        status = _get_private_link_status()
        operation = status.get("operation")
        if not isinstance(operation, dict):
            return status, None, None

        portal_echoed = _maybe_echo_private_link_portal(
            operation,
            already_echoed=portal_echoed,
        )
        phase = str(operation.get("phase") or "")
        if phase in PRIVATE_LINK_TERMINAL_PHASES:
            guidance = operation.get("guidance")
            return status, phase, str(guidance) if guidance else None

        if time.monotonic() >= deadline:
            return status, "timeout", "timed out waiting for solstone private link."

        if interval:
            time.sleep(interval)


def _echo_private_link_status(status: dict[str, Any]) -> None:
    posture = "solstone private link" if status.get("posture") == "spl" else "direct"
    typer.echo(f"posture: {posture}")
    typer.echo(f"state: {_private_link_state_label(status.get('state'))}")
    typer.echo(f"enrolled: {'yes' if status.get('enrolled') else 'no'}")
    if status.get("state") == "enabled" and status.get("relay_url"):
        typer.echo(f"relay URL: {status['relay_url']}")
    operation = status.get("operation")
    if isinstance(operation, dict):
        phase = operation.get("phase")
        if phase:
            typer.echo(f"operation: {phase}")
        guidance = operation.get("guidance")
        if guidance:
            typer.echo(str(guidance))


def _echo_private_link_terminal(
    status: dict[str, Any],
    phase: str | None,
    operation_guidance: str | None,
) -> None:
    if phase == "enabled":
        typer.echo(PRIVATE_LINK_SETUP_SUCCESS)
        return
    if phase == "needs_subscription":
        operation = status.get("operation")
        subscribe_url = (
            operation.get("subscribe_url") if isinstance(operation, dict) else None
        )
        typer.echo(PRIVATE_LINK_NEEDS_SUBSCRIPTION)
        if subscribe_url:
            typer.echo(str(subscribe_url))
        return
    if phase == "revoked":
        typer.echo(PRIVATE_LINK_SETUP_FAILED, err=True)
        if operation_guidance:
            typer.echo(operation_guidance, err=True)
        raise typer.Exit(1)
    if phase in {"error", "timeout"}:
        typer.echo(PRIVATE_LINK_SETUP_FAILED, err=True)
        if operation_guidance:
            typer.echo(operation_guidance, err=True)
        raise typer.Exit(1)
    _echo_private_link_status(status)


@private_link_app.command("status")
@convey_cli
def private_link_status() -> None:
    """Show solstone private link status."""

    _echo_private_link_status(_get_private_link_status())


@private_link_app.command("setup")
@convey_cli
def private_link_setup(
    wait_seconds: float = typer.Option(
        900.0, "--wait-seconds", help="Maximum seconds to wait for the operation."
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between status polls."
    ),
) -> None:
    """Set up solstone private link."""

    typer.echo(PRIVATE_LINK_SETTING_UP)
    _post_private_link("/app/link/private-link/enable")
    status, phase, operation_guidance = _poll_private_link_until_terminal(
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )
    _echo_private_link_terminal(status, phase, operation_guidance)


@private_link_app.command("disable")
@convey_cli
def private_link_disable() -> None:
    """Turn off solstone private link."""

    try:
        response = _post_private_link("/app/link/private-link/disable")
    except ConveyClientError as err:
        if err.reason_code == SERVICE_OPERATION_FAILED.code:
            typer.echo(PRIVATE_LINK_DISABLE_FAILED, err=True)
            raise typer.Exit(1) from err
        raise

    status = response.get("status") if isinstance(response, dict) else None
    state = status.get("state") if isinstance(status, dict) else None
    if state == "not_enabled":
        typer.echo(PRIVATE_LINK_DISABLE_SUCCESS)
        return
    typer.echo(PRIVATE_LINK_NEEDS_REPAIR, err=True)
    raise typer.Exit(1)


@app.command()
@convey_cli
def pair(
    device_label: str | None = typer.Option(
        None, "--device-label", help="Label for the linked system being paired"
    ),
    as_role: str | None = typer.Option(
        None,
        "--as",
        help=(
            "Optional tag for the linked system. Links are role-less by default; "
            "only peer has special behavior. One of: phone, observer, peer."
        ),
    ),
    timeout_seconds: int = typer.Option(
        PAIR_TIMEOUT_SECONDS,
        "--timeout",
        help="How long to wait for the linked system before giving up",
    ),
) -> None:
    """Start a pairing link, print join-ready credentials, wait for completion."""
    if as_role is not None and as_role not in VALID_ROLES:
        typer.echo("invalid role; expected one of: phone, observer, peer", err=True)
        raise typer.Exit(2)

    client = get_client()
    payload = {"device_label": device_label or ""}
    if as_role is not None:
        payload["role"] = as_role
    try:
        resp = client.request("POST", "/app/link/pair-start", json=payload)
    except ConveyClientError as err:
        if err.reason_code == PAIRING_REQUEST_INVALID.code:
            typer.echo(CLI_PAIR_NO_LAN_ADDRESS, err=True)
            raise typer.Exit(1) from err
        raise
    nonce = resp["nonce"]
    pair_link = resp["pair_link"]
    ca_fp = resp["ca_fingerprint"]

    typer.echo(f"{CLI_PAIR_LINK_LABEL}: {pair_link}")
    typer.echo(CLI_PAIR_JOIN_HINT)
    join_cmd = ["sol", "link", "join", "--code", pair_link]
    if device_label:
        join_cmd += ["--label", device_label]
    typer.echo("  " + shlex.join(join_cmd))
    typer.echo(f"{CLI_PAIR_CA_FINGERPRINT_LABEL}: sha256:{ca_fp}")
    if device_label:
        typer.echo(f"Device: {device_label}{' (peer)' if as_role == 'peer' else ''}")
    typer.echo("")
    typer.echo("Waiting for linked system…")

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
            suffix = " (peer)" if entry["role"] == "peer" else ""
            label = entry.get("display_label") or entry["device_label"]
            typer.echo(f"Paired: {label}{suffix}")
            typer.echo(f"  fingerprint: {entry['fingerprint']}")
            typer.echo(f"  paired_at:   {entry['paired_at']}")
            raise typer.Exit(0)
        nonce_status = client.request(
            "GET",
            "/app/link/api/pair/nonce-status",
            params={"nonce": nonce},
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
    linked_systems = []
    peers = []
    for device in devices:
        # call.py is a pure HTTP client, so it cannot import link.auth.is_peer.
        if device.get("role") == "peer":
            peers.append(device)
        else:
            linked_systems.append(device)

    printed_section = False
    for heading, entries in (
        (LINKED_SYSTEMS_HEADING, linked_systems),
        (PEERS_HEADING, peers),
    ):
        if not entries:
            continue
        if printed_section:
            typer.echo("")
        typer.echo(heading)
        for device in entries:
            label = device.get("display_label") or device["device_label"]
            typer.echo(
                f"- {label}"
                f" — added {_relative_time(device['paired_at'])}"
                f" — last seen {_relative_time(device['last_seen_at'])}"
                f" [{device['fingerprint_short']}]"
            )
        printed_section = True


@app.command("authorized-clients")
@convey_cli
def authorized_clients() -> None:
    """List every authorized client cert: fingerprint, label, last-seen (flat view)."""
    devices = get_client().request("GET", "/app/link/api/devices")["devices"]
    if not devices:
        typer.echo("No authorized clients.")
        return
    for device in devices:
        label = device.get("display_label") or device["device_label"]
        typer.echo(
            f"{device['fingerprint']}  {label}"
            f"  last seen {_relative_time(device['last_seen_at'])}"
        )


@app.command("observer-pause")
def observer_pause() -> None:
    """Pause linked observers (not yet available)."""
    typer.echo("observer-pause is not yet available.")


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
    private_link = client.request("GET", "/app/link/api/private-link")
    paired_count = len(client.request("GET", "/app/link/api/devices")["devices"])
    if state["instance_id"] is None:
        typer.echo("Instance ID:   (not provisioned — pair a device to provision)")
        typer.echo("Home label:    (not provisioned)")
    else:
        typer.echo(f"Instance ID:   {state['instance_id']}")
        typer.echo(f"Home label:    {state['home_label']}")
    typer.echo(f"Relay URL:     {state['relay_url']}")
    typer.echo(f"Enrolled:      {'yes' if state['enrolled'] else 'no'}")
    posture = (
        "solstone private link" if private_link.get("posture") == "spl" else "direct"
    )
    typer.echo(f"Reach posture: {posture}")
    typer.echo(f"Private link:  {_private_link_state_label(private_link.get('state'))}")
    typer.echo(f"Paired devices: {paired_count}")
    typer.echo("Listen-WS state: (query convey /app/link/api/status for live state)")

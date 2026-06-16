# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""link app routes — pair ceremony + paired-device dashboard.

All user-facing work for the spl tunnel integration happens here. The
protocol-level code (TLS, framing, mux) lives in `think/link/`; this
module is the HTTP surface that mobiles and the convey UI hit.

Routes:

  GET  /link                    dashboard (paired devices + pair button)
  GET  /link/qr.png             QR image for an active nonce (via ?token=)
  POST /link/pair-start         generate a new nonce + return QR payload
  POST /link/pair               mobile posts CSR + nonce; we sign + attest
  POST /link/unpair             remove a fingerprint (immediate revocation)
  GET  /link/api/devices        JSON list of paired devices for JS polling
  GET  /link/api/status         service status (for dashboard refresh)

Pair-link QR joins target the secure listener advertised by LINK_DIRECT_PORT
(:7657) and speak its TLS + framed mux protocol before dispatching POST
/app/link/pair into this Flask route. The open nonce admits a cert-less
pairing stream; the QR's CA fingerprint pins the home CA before the signed
client certificate is issued.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json as _json
import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from flask import Blueprint, Response, abort, g, jsonify, request

from solstone.apps.link import copy as link_copy
from solstone.apps.link.copy import (
    PAIR_LINK_HOST,
    PAIR_LINK_PATH,
)
from solstone.apps.link.crockford32 import encode as crockford_encode
from solstone.apps.link.relay_link import (
    TOTP_STEP_SECONDS,
    compute_current_totp,
    encode_relay_pair_link,
)
from solstone.apps.utils import log_app_action
from solstone.convey import emit
from solstone.convey.bridge import get_cached_state
from solstone.convey.reasons import (
    CONVEY_OPERATION_FAILED,
    INVALID_CONFIG_VALUE,
    INVALID_OPERATION_FOR_STATE,
    INVALID_REQUEST_VALUE,
    MISSING_REQUIRED_FIELD,
    OPERATION_NO_LONGER_AVAILABLE,
    PAIRED_DEVICE_NOT_FOUND,
    PAIRING_KEY_INVALID,
    PAIRING_REQUEST_INVALID,
    SERVICE_BUSY,
    SERVICE_OPERATION_FAILED,
)
from solstone.convey.utils import error_response
from solstone.think.link import interface_watcher
from solstone.think.link.auth import AuthorizedClients, ClientEntry, is_peer
from solstone.think.link.ca import (
    generate_nonce,
    generate_relay_nonce,
    load_or_generate_ca,
    mint_attestation,
    sign_csr,
)
from solstone.think.link.interface_watcher import get_interface_watcher
from solstone.think.link.local_endpoints import (
    LocalEndpoint,
    LocalEndpointsResponse,
    endpoint_to_dict,
    response_to_dict,
)
from solstone.think.link.nonces import Nonce, NonceStore
from solstone.think.link.paths import (
    DEFAULT_RELAY_URL,
    LinkState,
    authorized_clients_path,
    ca_dir,
    load_service_token,
    load_totp_secret,
    nonces_path,
    relay_url,
)
from solstone.think.link.window import read_posture
from solstone.think.pairing.config import (
    InvalidHostUrl,
    clear_host_url,
    override_host_port,
    set_host_url,
    validate_host_url,
)
from solstone.think.services import operations, spl, spl_handoff
from solstone.think.services import status as service_status
from solstone.think.utils import get_journal, now_ms

logger = logging.getLogger(__name__)
_SENDER_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,256}$")
VALID_ROLES = {"", "phone", "observer", "peer"}
# The watcher emits only lan/ula today; vpn stays empty until a scope is wired.
VPN_SCOPES = {"vpn"}
journal_sources = import_module("solstone.apps.import.journal_sources")
create_state_directory = journal_sources.create_state_directory
load_journal_source_by_fingerprint = journal_sources.load_journal_source_by_fingerprint
save_journal_source = journal_sources.save_journal_source
journal_source_state_prefix = journal_sources.journal_source_state_prefix
mint_pl_journal_source_record = journal_sources.mint_pl_journal_source_record

link_bp = Blueprint(
    "app:link",
    __name__,
    url_prefix="/app/link",
)


def _authorized() -> AuthorizedClients:
    return AuthorizedClients(authorized_clients_path())


def _nonces() -> NonceStore:
    return NonceStore(nonces_path())


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_device_label() -> str:
    now = dt.datetime.now()
    return link_copy.DEVICE_LABEL_DEFAULT_FORMAT.format(
        month=now.strftime("%b"),
        day=now.strftime("%d"),
    )


def _display_label(assigned: str, client: str) -> str:
    assigned = (assigned or "").strip()
    client = (client or "").strip()
    if assigned and client and assigned != client:
        return f"{assigned} ({client})"
    return assigned or client


def _rough_network(mode: str) -> str:
    return "anywhere" if mode == "pl-via-spl" else "network"


def _is_loopback_request() -> bool:
    return request.remote_addr in {"127.0.0.1", "::1"}


def _read_link_connection_event() -> str | None:
    event = get_cached_state().get("link_connection")
    return event if isinstance(event, str) else None


def _current_local_endpoints() -> list[LocalEndpoint]:
    watcher = get_interface_watcher()
    return watcher.snapshot() if watcher else []


def _list_pair_link_candidates() -> list[str]:
    """Return up to 4 watcher IPv4 candidates, detect-ip hinted, deduped then capped."""
    candidates: list[str] = []
    for endpoint in _current_local_endpoints():
        address = ipaddress.ip_address(endpoint.ip)
        if isinstance(address, ipaddress.IPv4Address):
            candidates.append(str(address))

    route_ip = _detect_lan_ip()
    if route_ip in candidates:
        candidates.remove(route_ip)
        candidates.insert(0, route_ip)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped[:4]


def _secure_listener_port() -> int:
    """Port the journal advertises in its secure-listener local endpoints.

    Read at call time (monkeypatch-able) and independent of whether the
    interface-watcher snapshot is populated — it can be empty in the CLI/test
    path, so do not read it from _current_local_endpoints().
    """
    return interface_watcher.LINK_DIRECT_PORT


def _effective_home_address() -> tuple[bool, str | None]:
    override_addr = override_host_port()
    if override_addr is not None:
        return True, override_addr
    return _is_lan_accessible(), None


def _detect_lan_ip() -> str | None:
    """Pick a reasonable LAN-facing IPv4 by opening a UDP socket.

    No packets are sent — we just read what src address the kernel would
    pick for a route to an external host. Returns None on any error.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


def _ca_fingerprint() -> str:
    ca = load_or_generate_ca(ca_dir())
    return ca.fingerprint_sha256()


def _build_pair_link(
    host: str,
    port: int,
    nonce: str,
    ca_fp: str,
) -> str:
    """Build the v04 pair-link URL.

    Layout:
    version(1) | addr_type(1) | ipv4(4) | port_be(2) | nonce(16) | ca_fp[:16].
    Encoded as 64-char uppercase Crockford base32 in the URL fragment.
    """
    ipv4_bytes = ipaddress.IPv4Address(host).packed
    port_bytes = port.to_bytes(2, "big")
    nonce_bytes = bytes.fromhex(nonce)
    ca_fp_bytes = bytes.fromhex(ca_fp)[:16]
    blob = b"\x04\x01" + ipv4_bytes + port_bytes + nonce_bytes + ca_fp_bytes
    assert len(blob) == 40
    return f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#{crockford_encode(blob)}"


def _build_pair_link_v05(
    candidates: list[str],
    port: int,
    nonce: str,
    ca_fp: str,
) -> str:
    """Build the v05 multi-address pair-link URL.

    Layout:
    version(1) | addr_type(1) | count(1) | port_be(2) | ipv4(4)*count |
    nonce(16) | ca_fp[:16].

    v05 places the shared port before the address list, unlike v04's single
    address-before-port layout. Count is capped at 4; length is 37 + 4*count.
    """
    count = len(candidates)
    blob = (
        b"\x05\x01"
        + bytes([count])
        + port.to_bytes(2, "big")
        + b"".join(ipaddress.IPv4Address(c).packed for c in candidates)
        + bytes.fromhex(nonce)
        + bytes.fromhex(ca_fp)[:16]
    )
    assert len(blob) == 37 + 4 * count
    return f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#{crockford_encode(blob)}"


@dataclass(frozen=True)
class PairStartResponse:
    nonce: str
    pair_link: str
    expires_in: int
    device_label: str
    ca_fingerprint: str


def _jsonify_preserving_order(payload: dict[str, Any]) -> Response:
    return Response(_json.dumps(payload), mimetype="application/json")


def _is_lan_accessible() -> bool:
    """Check whether the journal's home address is reachable on the LAN.

    Feeds the home-address reachability status on /link. Best-effort: the
    signal is the Host header the dashboard loaded under.
    """
    hostname, _, _ = request.host.partition(":")
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return bool(_detect_lan_ip())
    return True


def _derive_relay_state(token_present: bool) -> str:
    """Return pre-mechanism relay attachment state.

    connecting/parked are valid contract values but are not produced until
    parking is wired.
    """
    return "offline" if token_present else "not-enrolled"


def _derive_spl_relay_state(
    token_present: bool,
    connection_event: str | None,
) -> str:
    if not token_present:
        return "not-enrolled"
    # A missing event usually means convey restarted before observing the link
    # service transition. Treat it as connecting; a genuinely down relay can
    # read as finishing setup briefly until a disconnect event arrives.
    if connection_event == "connected":
        # Connected has no freshness bound. A hard service crash can leave this
        # parked until convey restarts, which resets the cache to connecting.
        return "parked"
    if connection_event == "disconnect":
        return "offline"
    return "connecting"


def _derive_reachability(
    lan_accessible: bool,
    posture: str,
    relay_state: str,
) -> str:
    if not lan_accessible:
        return "lan-unreachable"
    if posture == "direct":
        return "online"
    # posture == "spl": map relay_state. "reconnecting" is reserved.
    return {
        "connecting": "finishing-setup",
        "parked": "online",
        "offline": "offline",
        "not-enrolled": "finishing-setup",
    }[relay_state]


def _private_link_status() -> dict[str, Any]:
    resting = service_status.spl_status()
    state = str(resting["state"])
    return {
        "service": "spl",
        "state": state,
        "posture": read_posture(),
        "enrolled": load_service_token() is not None,
        "relay_url": relay_url(),
        "actions": {
            "enable": state in {"not_enabled", "inconsistent"},
            "disable": state in {"enabled", "inconsistent"},
        },
        "operation": operations.operation_for_service("spl"),
    }


def _start_operation_response(
    service: str,
    kind: str,
    portal_url: str | None,
    flow: Callable[[], operations.HandoffResult],
) -> tuple[Response, int]:
    try:
        operation = operations.start_operation(service, kind, portal_url, flow)
    except operations.OperationBusyError:
        return error_response(SERVICE_BUSY, detail="operation already running")
    return jsonify({"success": True, "service": service, "operation": operation}), 202


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


@link_bp.route("/api/devices")
def api_devices() -> Any:
    """JSON list of paired devices — used by the dashboard JS."""
    entries = _authorized().snapshot()
    devices = [_entry_to_json(e) for e in entries]
    return jsonify({"devices": devices})


@link_bp.route("/api/status")
def api_status() -> Any:
    """Snapshot of link-service state for the dashboard header."""
    state = LinkState.load()
    token = load_service_token()
    token_present = token is not None
    ca_fp = _ca_fingerprint() if ca_dir().exists() else None
    lan_accessible, home_address = _effective_home_address()
    posture = read_posture()
    relay_state = (
        _derive_spl_relay_state(token_present, _read_link_connection_event())
        if posture == "spl"
        else _derive_relay_state(token_present)
    )
    reachability = _derive_reachability(lan_accessible, posture, relay_state)
    vpn_candidates = [
        {"label": ep.scope, "address": f"{ep.ip}:{ep.port}"}
        for ep in _current_local_endpoints()
        if ep.scope in VPN_SCOPES
    ]
    return jsonify(
        {
            "instance_id": state.instance_id if state else None,
            "home_label": state.home_label if state else None,
            "enrolled": token_present,
            "relay_url": relay_url(),
            "ca_fingerprint": ca_fp,
            "lan_accessible": lan_accessible,
            "posture": posture,
            "reachability": reachability,
            "relay_state": relay_state,
            "home_address": home_address,
            "vpn": {"active": None, "candidates": vpn_candidates},
        }
    )


@link_bp.route("/api/private-link")
def api_private_link() -> Any:
    return jsonify({"success": True, **_private_link_status()})


@link_bp.route("/private-link/enable", methods=["POST"])
def private_link_enable() -> tuple[Response, int]:
    if _private_link_status()["state"] == "enabled":
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="solstone private link is already on",
        )
    try:
        consent_url, nonce, base_url = spl_handoff.build_spl_handoff_url()
    except OSError:
        return error_response(
            SERVICE_OPERATION_FAILED,
            detail="couldn't prepare the consent link",
        )
    return _start_operation_response(
        "spl",
        "spl_enable",
        consent_url,
        lambda: spl_handoff.run_spl_handoff(nonce=nonce, base_url=base_url),
    )


@link_bp.route("/private-link/disable", methods=["POST"])
def private_link_disable() -> tuple[Response, int]:
    try:
        outcome = spl.disable_spl()
    except Exception:
        logger.exception("link private-link disable failed")
        return error_response(SERVICE_OPERATION_FAILED)
    return (
        jsonify(
            {
                "success": True,
                "service": "spl",
                "result": {"was_enabled": outcome.was_enabled},
                "status": _private_link_status(),
            }
        ),
        200,
    )


@link_bp.route("/host-address", methods=["POST"])
def set_host_address() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    raw_address = payload.get("address")
    address = raw_address if isinstance(raw_address, str) else None
    try:
        if address is not None and address.strip():
            set_host_url(validate_host_url(address))
        else:
            clear_host_url()
    except InvalidHostUrl as exc:
        return error_response(INVALID_CONFIG_VALUE, detail=str(exc))
    _, home_address = _effective_home_address()
    return jsonify({"ok": True, "home_address": home_address})


@link_bp.get("/local-endpoints")
def local_endpoints() -> Any:
    if not _is_loopback_request():
        abort(404)
    response = LocalEndpointsResponse(
        v=1,
        endpoints=tuple(_current_local_endpoints()),
        ttl_s=3600,
        generated_at=_utc_now_iso(),
    )
    return jsonify(response_to_dict(response))


# ---------------------------------------------------------------------------
# pair ceremony
# ---------------------------------------------------------------------------


@link_bp.route("/api/pair/nonce-status")
def api_pair_nonce_status() -> Any:
    entry = _nonces().peek(request.args.get("nonce", ""))
    return jsonify({"present": entry is not None, "used": bool(entry and entry.used)})


@link_bp.route("/pair-start", methods=["POST"])
def pair_start() -> Any:
    """Generate a single-use 5-minute nonce and return link-ready payload."""
    payload = request.get_json(silent=True) or {}
    device_label = str(payload.get("device_label") or "").strip()
    raw_role = payload.get("role", "")
    role = "" if raw_role is None else raw_role
    if not isinstance(role, str) or role not in VALID_ROLES:
        return error_response(PAIRING_REQUEST_INVALID, detail="invalid role")

    nonce_ttl: int | None = None
    if read_posture() == "spl":
        secret = load_totp_secret()
        if secret is None:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="spl posture requires a relay TOTP secret; none is configured",
            )

        ca = load_or_generate_ca(ca_dir())
        ca_fp = ca.fingerprint_sha256()
        now = int(time.time())
        totp = compute_current_totp(secret, now)
        nonce = generate_relay_nonce()
        origin = relay_url()
        relay_origin = None if origin == DEFAULT_RELAY_URL else origin
        instance_id = LinkState.load_or_create().instance_id
        pair_link = encode_relay_pair_link(
            instance_id,
            totp,
            nonce,
            ca.spki_fingerprint_sha256(),
            relay_origin=relay_origin,
        )
        expires_in = TOTP_STEP_SECONDS
        nonce_ttl = TOTP_STEP_SECONDS
    else:
        ca_fp = _ca_fingerprint()
        port = _secure_listener_port()
        override = override_host_port()
        if override is not None:
            candidates = [override.partition(":")[0]]
        else:
            candidates = _list_pair_link_candidates()
        if not candidates:
            return error_response(
                PAIRING_REQUEST_INVALID,
                detail="pair-link requires an IPv4 LAN address; none found",
            )
        nonce = generate_nonce()
        if len(candidates) == 1:
            pair_link = _build_pair_link(candidates[0], port, nonce, ca_fp)
        else:
            pair_link = _build_pair_link_v05(candidates, port, nonce, ca_fp)
        expires_in = 300

    add_kwargs: dict[str, Any] = {}
    if nonce_ttl is not None:
        add_kwargs["ttl"] = nonce_ttl
    _nonces().add(
        nonce,
        device_label,
        role=role,
        **add_kwargs,
    )
    response = PairStartResponse(
        nonce=nonce,
        pair_link=pair_link,
        expires_in=expires_in,
        device_label=device_label,
        ca_fingerprint=ca_fp,
    )
    return _jsonify_preserving_order(asdict(response))


def _complete_pairing(
    consumed: Nonce,
    csr_pem: str,
    assigned_label: str,
    client_label: str,
    *,
    network: str,
    sender_instance_id: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    ca = load_or_generate_ca(ca_dir())
    cert_label = client_label or assigned_label or _default_device_label()
    client_cert_pem, fingerprint = sign_csr(ca, csr_pem, cert_label)

    state = LinkState.load_or_create()
    paired_at = _utc_now_iso()
    attestation = mint_attestation(ca, state.instance_id, fingerprint)
    ca_chain_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    response: dict[str, Any] = {
        "client_cert": client_cert_pem,
        "ca_chain": [ca_chain_pem],
        "instance_id": state.instance_id,
        "home_label": state.home_label,
        "home_attestation": attestation,
        "fingerprint": fingerprint,
    }
    endpoints = _current_local_endpoints()
    if endpoints:
        response["local_endpoints"] = [endpoint_to_dict(ep) for ep in endpoints]

    journal_source_record_path = None
    try:
        if is_peer(consumed.role):
            journal_source_record_path = mint_pl_journal_source_record(
                fingerprint=fingerprint,
                device_label=cert_label,
                paired_at=paired_at,
                peer_instance_id=sender_instance_id,
            )
            create_state_directory(Path(get_journal()), journal_source_record_path.stem)
        _authorized().add(
            fingerprint=fingerprint,
            device_label=assigned_label,
            instance_id=state.instance_id,
            role="peer" if is_peer(consumed.role) else "",
            paired_at=paired_at,
            network=network,
            client_label=client_label,
        )
    except Exception:
        if journal_source_record_path is not None:
            try:
                journal_source_record_path.unlink()
            except FileNotFoundError:
                pass
        raise

    return response, fingerprint, paired_at


def _emit_pair_complete(
    device_label: str,
    fingerprint: str,
    paired_at: str,
    *,
    network: str,
) -> None:
    emit(
        "link",
        "pair_complete",
        device_label=device_label,
        fingerprint=fingerprint,
        fingerprint_short=fingerprint.replace("sha256:", "")[:16],
        paired_at=paired_at,
        network=network,
    )


@link_bp.route("/pair", methods=["POST"])
def pair() -> Any:
    """Mobile pair endpoint — accepts CSR + nonce, signs + mints attestation.

    Query: `?token=<nonce>` (the nonce minted by /pair-start).
    Body  (JSON):
        {
          "csr":          "<PEM>",      // required
          "device_label": "<string>",   // optional client self-name
          "nonce":        "<hex>"       // optional: may be in body instead of query
        }

    Response on success (200):
        {
          "client_cert":       "<PEM>",
          "ca_chain":          ["<PEM>", ...],
          "instance_id":       "<uuid>",
          "home_label":        "<string>",
          "home_attestation":  "<JWT>",
          "fingerprint":       "sha256:<hex>"
        }
    """
    body = request.get_json(silent=True) or {}
    nonce_value = request.args.get("token") or body.get("nonce")
    csr_pem = body.get("csr")
    device_label = str(body.get("device_label") or "").strip()

    if not isinstance(nonce_value, str) or not isinstance(csr_pem, str):
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="missing fields (nonce + csr required)",
        )
    raw_sender_instance_id = body.get("sender_instance_id")
    sender_instance_id: str | None = None
    if raw_sender_instance_id is not None:
        if not isinstance(
            raw_sender_instance_id, str
        ) or not _SENDER_INSTANCE_ID_RE.fullmatch(raw_sender_instance_id):
            return error_response(
                PAIRING_REQUEST_INVALID,
                detail=f"bad sender_instance_id: {raw_sender_instance_id}",
            )
        sender_instance_id = raw_sender_instance_id

    consumed = _nonces().consume(nonce_value)
    if consumed is None:
        return error_response(
            OPERATION_NO_LONGER_AVAILABLE,
            detail="nonce expired or used",
        )

    assigned_label = consumed.device_label
    client_label = device_label

    network = _rough_network(g.identity.mode)
    try:
        response, fingerprint, paired_at = _complete_pairing(
            consumed,
            csr_pem,
            assigned_label,
            client_label,
            network=network,
            sender_instance_id=sender_instance_id,
        )
    except ValueError as exc:
        logger.info("pair: bad csr: %s", exc)
        return error_response(PAIRING_KEY_INVALID, detail=f"bad csr: {exc}")
    _emit_pair_complete(
        _display_label(assigned_label, client_label),
        fingerprint,
        paired_at,
        network=network,
    )
    return jsonify(response)


@link_bp.route("/rename", methods=["POST"])
def rename() -> Any:
    """Rename a paired device by fingerprint."""
    body = request.get_json(silent=True) or {}
    fingerprint = body.get("fingerprint")
    label = body.get("label")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="fingerprint and label required",
        )
    if not isinstance(label, str):
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="fingerprint and label required",
        )

    authorized = _authorized()
    try:
        updated = authorized.update_label(fingerprint.strip(), label)
    except ValueError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except OSError as exc:
        logger.error("rename: failed to persist label for %s: %s", fingerprint, exc)
        return error_response(
            CONVEY_OPERATION_FAILED,
            detail="couldn't save the new label",
        )
    if not updated:
        return error_response(PAIRED_DEVICE_NOT_FOUND, detail="fingerprint not paired")
    return jsonify({"fingerprint": fingerprint, "label": label.strip()})


@link_bp.route("/unpair", methods=["POST"])
def unpair() -> Any:
    """Revoke a paired device by label or fingerprint.

    Body (JSON): {"fingerprint": "sha256:..."} or {"device_label": "..."}
    """
    body = request.get_json(silent=True) or {}
    raw_fingerprint = body.get("fingerprint")
    raw_device_label = body.get("device_label")
    fingerprint = raw_fingerprint.strip() if isinstance(raw_fingerprint, str) else None
    device_label = (
        raw_device_label.strip() if isinstance(raw_device_label, str) else None
    )
    fingerprint = fingerprint or None
    device_label = device_label or None

    authorized = _authorized()
    if fingerprint is not None:
        entry = authorized.get(fingerprint)
    elif device_label is not None:
        entry = authorized.find_by_label(device_label)
        if entry is not None:
            fingerprint = entry.fingerprint
    else:
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="fingerprint or device_label required",
        )

    if entry is None:
        detail = (
            "fingerprint not paired"
            if fingerprint is not None
            else "no paired device with that label"
        )
        return error_response(
            PAIRED_DEVICE_NOT_FOUND,
            detail=detail,
        )

    fp_hex = fingerprint.removeprefix("sha256:")
    short_fp = fp_hex[:16]
    role = entry.role

    if is_peer(role):
        source = load_journal_source_by_fingerprint(fingerprint)
        if source is None:
            logger.warning("unpair: peer journal source missing for %s", short_fp)
        elif source.get("revoked"):
            logger.warning("unpair: peer journal source %s already revoked", short_fp)
        else:
            source["revoked"] = True
            source["revoked_at"] = now_ms()
            if save_journal_source(source):
                log_app_action(
                    app="import",
                    facet=None,
                    action="journal_source_revoke",
                    params={
                        "name": source.get("device_label") or source.get("name"),
                        "key_prefix": journal_source_state_prefix(source),
                    },
                )
            else:
                logger.error(
                    "unpair: failed to save peer journal source for %s", short_fp
                )
        authorized.remove(fingerprint)
    else:
        authorized.remove(fingerprint)
    return jsonify({"unpaired": fingerprint})


def _entry_to_json(entry: ClientEntry) -> dict[str, Any]:
    short_fp = entry.fingerprint.replace("sha256:", "")[:16]
    return {
        "fingerprint": entry.fingerprint,
        "fingerprint_short": short_fp,
        "device_label": entry.device_label,
        "display_label": _display_label(entry.device_label, entry.client_label),
        "paired_at": entry.paired_at,
        "last_seen_at": entry.last_seen_at,
        "role": entry.role,
        "network": entry.network,
    }


# ---------------------------------------------------------------------------
# helpers for the workspace template
# ---------------------------------------------------------------------------


@link_bp.app_context_processor
def _inject_link_helpers() -> dict[str, Any]:
    """Make `url_for` to link endpoints easy from templates."""
    return {"link_copy": link_copy, "posture": read_posture()}

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""SPP confidential RA-TLS transport and verifier hook."""

from __future__ import annotations

import logging
import secrets
import selectors
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from OpenSSL import SSL

from solstone.think.models import AttestationFailedError, AttestationStaleError
from solstone.think.providers.nvattest_install import resolve_nvattest_dir
from solstone.think.services import spp
from solstone.think.services.spp_attest.cadence import AttestationSession
from solstone.think.services.spp_attest.composite import verify_composite
from solstone.think.services.spp_attest.ratls.channel import (
    AttestedChannel,
    RatlsChannelError,
    RatlsEndpoint,
    establish_attested_channel,
)
from solstone.think.services.spp_attest.ratls.contract import OWNER_NONCE_BYTES
from solstone.think.services.spp_attest.ratls.verify import RatlsVerificationError

log = logging.getLogger(__name__)

POOLED_CHANNEL_MAX_IDLE_S = 120.0
FORWARDER_SELECT_TIMEOUT_S = 180.0

_LOCK = threading.RLock()
_POOL: list[AttestedChannel] = []
_ACTIVE: set[AttestedChannel] = set()
_EPOCH = 0
_LISTENER: socket.socket | None = None
_LISTENER_THREAD: threading.Thread | None = None
_FORWARDER_BASE_URL: str | None = None
_CONFIDENTIAL_BLOCK: dict[str, Any] | None = None


class ConfidentialEndpointError(RuntimeError):
    """Raised when confidential endpoint configuration is unusable."""

    reason_code = "endpoint_invalid"


def _failure_kind(exc: BaseException) -> Literal["failed", "unreachable"]:
    if isinstance(exc, RatlsChannelError) and exc.reason_code == "gateway_unreachable":
        return "unreachable"
    return "failed"


def _attestation_failed(
    kind: Literal["failed", "unreachable"],
    reason_code: str,
) -> None:
    log.warning("event=confidential_attestation_rejected reason=%s", reason_code)
    spp.record_attestation_failed(kind, reason_code)
    raise AttestationFailedError(
        f"the confidential attestation transport failed closed ({reason_code})"
    )


def _endpoint_from_block(block: dict[str, Any]) -> RatlsEndpoint:
    endpoint_url = str(block.get("endpoint_url") or "")
    parsed = urlsplit(endpoint_url)
    if not parsed.hostname:
        raise ConfidentialEndpointError("confidential endpoint hostname is invalid")
    try:
        port = parsed.port
    except ValueError:
        raise ConfidentialEndpointError("confidential endpoint port is invalid")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return RatlsEndpoint(host=parsed.hostname, port=port)


def _transport_live_locked() -> bool:
    return (
        _LISTENER is not None
        and _LISTENER_THREAD is not None
        and _LISTENER_THREAD.is_alive()
        and _FORWARDER_BASE_URL is not None
    )


def _close_channel(channel: AttestedChannel) -> None:
    channel.close()


def _teardown_locked() -> None:
    global _EPOCH, _FORWARDER_BASE_URL, _LISTENER, _LISTENER_THREAD

    _EPOCH += 1
    listener = _LISTENER
    pooled = tuple(_POOL)
    active = tuple(_ACTIVE)
    _LISTENER = None
    _LISTENER_THREAD = None
    _FORWARDER_BASE_URL = None
    _POOL.clear()
    _ACTIVE.clear()
    if listener is not None:
        try:
            listener.close()
        except OSError:
            pass
    for channel in pooled:
        _close_channel(channel)
    for channel in active:
        _close_channel(channel)


def teardown_confidential_transport() -> None:
    with _LOCK:
        _teardown_locked()
        spp.clear_attestation_state()


def _discard_idle_locked(now_monotonic: float) -> None:
    kept: list[AttestedChannel] = []
    for channel in _POOL:
        if (
            channel.epoch != _EPOCH
            or now_monotonic - channel.last_used_monotonic > POOLED_CHANNEL_MAX_IDLE_S
        ):
            _close_channel(channel)
        else:
            kept.append(channel)
    _POOL[:] = kept


def _start_listener_locked() -> None:
    global _FORWARDER_BASE_URL, _LISTENER, _LISTENER_THREAD

    if _transport_live_locked():
        return
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # The loopback listener is intentionally same-UID reachable: a local process
    # that can connect here can already read journal/config/journal.json and dial
    # the gateway with the plaintext credential. The load-bearing constraints are
    # loopback-only bind, ephemeral port, per-session lifetime, creation only
    # after P2-green, and teardown on stale/failure.
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(1.0)
    _LISTENER = listener
    port = listener.getsockname()[1]
    _FORWARDER_BASE_URL = f"http://127.0.0.1:{port}"
    thread = threading.Thread(
        target=_accept_loop,
        args=(listener,),
        name="spp-confidential-forwarder",
        daemon=True,
    )
    _LISTENER_THREAD = thread
    thread.start()


def _establish_channel_locked(block: dict[str, Any], now: datetime) -> AttestedChannel:
    try:
        endpoint = _endpoint_from_block(block)
        return establish_attested_channel(
            endpoint,
            owner_nonce=secrets.token_bytes(OWNER_NONCE_BYTES),
            nvattest_dir=resolve_nvattest_dir(block.get("nvattest_dir")),
            now=now,
            composite_verifier=verify_composite,
            monotonic_now=time.monotonic,
            epoch=_EPOCH,
        )
    except (RatlsChannelError, RatlsVerificationError) as exc:
        reason_code = exc.reason_code
        kind = _failure_kind(exc)
        _teardown_locked()
        _attestation_failed(kind, reason_code)
    except ConfidentialEndpointError as exc:
        _teardown_locked()
        _attestation_failed("failed", exc.reason_code)
    except AttestationFailedError as exc:
        _teardown_locked()
        _attestation_failed("failed", exc.reason_code)
    except Exception:
        _teardown_locked()
        _attestation_failed("failed", "unexpected_error")


def _establish_and_record_locked(block: dict[str, Any], now: datetime) -> None:
    channel = _establish_channel_locked(block, now)
    _start_listener_locked()
    _POOL.append(channel)
    spp.record_attestation_verified(
        AttestationSession(
            verdict=channel.verdict,
            started_at=now,
            tpm_heartbeat_at=now,
            gpu_reattest_at=now,
        )
    )


def verify_confidential_attestation(block: dict[str, Any]) -> None:
    global _CONFIDENTIAL_BLOCK

    now = datetime.now(timezone.utc)
    with _LOCK:
        _CONFIDENTIAL_BLOCK = dict(block)
        state = spp.get_attestation_state()
        if (
            state.session is not None
            and state.session.status(now) == "verified"
            and _transport_live_locked()
        ):
            return
        if (
            state.session is not None
            and state.session.status(now) != "verified"
            and _transport_live_locked()
        ):
            _teardown_locked()
            raise AttestationStaleError(
                "the confidential attestation cadence lapsed (attestation_stale)"
            )

        _establish_and_record_locked(block, now)


def confidential_egress_base_url(endpoint_base_url: str) -> str:
    block = spp.confidential_provenance()
    if block is None:
        return endpoint_base_url
    verify_confidential_attestation(block)
    with _LOCK:
        if _FORWARDER_BASE_URL is None:
            raise AttestationFailedError(
                "the confidential forwarder is unavailable (forwarder_unavailable)"
            )
        return _FORWARDER_BASE_URL


def confidential_probe_status(
    now: datetime | None = None,
) -> tuple[bool, str | None] | None:
    if spp.confidential_provenance() is None:
        return None
    now = now or datetime.now(timezone.utc)
    state = spp.get_attestation_state()
    if state.failure is not None:
        if state.failure.kind == "unreachable":
            return False, "attestation_unreachable"
        return False, "attestation_failed"
    if state.session is None:
        return False, "attestation_not_yet_verified"
    if state.session.status(now) == "stale":
        return False, "attestation_stale"
    return True, None


def recheck_confidential_attestation() -> None:
    """Recheck the configured confidential service without sending inference content."""

    global _CONFIDENTIAL_BLOCK

    block = spp.confidential_provenance()
    if block is None:
        return
    now = datetime.now(timezone.utc)
    with _LOCK:
        _CONFIDENTIAL_BLOCK = dict(block)
        _teardown_locked()
        spp.clear_attestation_state()
        try:
            _establish_and_record_locked(block, now)
        except AttestationFailedError:
            return


def _borrow_channel_locked(now_monotonic: float) -> AttestedChannel | None:
    _discard_idle_locked(now_monotonic)
    if not _POOL:
        return None
    return _POOL.pop()


def _borrow_or_establish_channel_locked(now: datetime) -> AttestedChannel | None:
    state = spp.get_attestation_state()
    if state.session is None or state.session.status(now) != "verified":
        return None
    channel = _borrow_channel_locked(time.monotonic())
    if _CONFIDENTIAL_BLOCK is None:
        return _activate_channel_locked(channel)
    if channel is None:
        channel = _establish_channel_locked(_CONFIDENTIAL_BLOCK, now)
    return _activate_channel_locked(channel)


def _activate_channel_locked(channel: AttestedChannel | None) -> AttestedChannel | None:
    if channel is None:
        return None
    _ACTIVE.add(channel)
    return channel


def _accept_loop(listener: socket.socket) -> None:
    while True:
        try:
            local, _addr = listener.accept()
        except TimeoutError:
            with _LOCK:
                if listener is not _LISTENER:
                    return
            continue
        except OSError:
            return
        thread = threading.Thread(
            target=_handle_loopback_connection,
            args=(local,),
            name="spp-confidential-forwarder-connection",
            daemon=True,
        )
        thread.start()


def _handle_loopback_connection(local: socket.socket) -> None:
    channel: AttestedChannel | None = None
    try:
        with _LOCK:
            channel = _borrow_or_establish_channel_locked(datetime.now(timezone.utc))
        if channel is None:
            local.close()
            return
        outcome = _pump(local, channel.tls)
        if outcome == "local_closed":
            channel.last_used_monotonic = time.monotonic()
            with _LOCK:
                _ACTIVE.discard(channel)
                if channel.epoch == _EPOCH and _transport_live_locked():
                    _POOL.append(channel)
                    channel = None
    finally:
        try:
            local.close()
        except OSError:
            pass
        if channel is not None:
            with _LOCK:
                _ACTIVE.discard(channel)
            _close_channel(channel)


def _pump(local: socket.socket, tls: SSL.Connection) -> str:
    selector = selectors.DefaultSelector()
    selector.register(local, selectors.EVENT_READ, (local, tls, "local"))
    selector.register(tls, selectors.EVENT_READ, (tls, local, "tls"))
    try:
        while True:
            events = selector.select(timeout=FORWARDER_SELECT_TIMEOUT_S)
            if not events:
                return "timeout"
            for key, _mask in events:
                source, destination, label = key.data
                try:
                    chunk = source.recv(65536)
                except (SSL.WantReadError, SSL.WantWriteError):
                    continue
                except (OSError, SSL.Error):
                    return f"{label}_closed"
                if not chunk:
                    return f"{label}_closed"
                destination.sendall(chunk)
    finally:
        selector.close()

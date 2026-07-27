# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""SPL relay tunnel proof primitive.

This library-only primitive performs the transport/identity half of the SPL
production proof. It creates an in-memory ECDSA-P256 client key and CSR, signs
it with the existing home CA, writes one strict attempt authorization, enrolls
and dials the pinned production relay, and returns an opaque lease plus a closed
diagnostic. It does not register observers, transfer segments, write probe
ledger records, start services, add endpoints, or create persistent identity
files.

Failure mapping:
- readiness or relay pin refusal -> capability_not_ready, checks=[]
- strict store rejection while creating authorization with no mutation ->
  capability_not_ready, checks=[]
- strict store rejection during cleanup -> cleanup_unverified, overriding all
  prior reasons
- local or request timeout -> deadline_exceeded, checks earned so far
- enrollment HTTP error, relay upgrade refusal, or inner-TLS authorization
  refusal -> remote_rejected, checks earned so far
- malformed enrollment response, malformed upgrade, or malformed mux/session
  success -> response_invalid, checks earned so far
- unexpected local failure -> internal_error, checks earned so far

Cancellation intentionally is not part of this primitive's diagnostic
vocabulary. Cooperative cancellation and outer task cancellation run bounded
cleanup; verified cleanup re-raises asyncio.CancelledError so the coordinator
can record probe_contract.REASON_CANCELLED. If cleanup is ambiguous, this
primitive returns cleanup_unverified instead because a possible leaked
authorization must not be hidden by cancellation.

RelayTunnelLease.close() returns None on verified success and is a no-op after
success. On ambiguous cleanup it raises RelayTunnelCloseError with only the
stable reason cleanup_unverified, leaves the lease open/retryable, and continues
to own the authorization. The proof diagnostic already returned on pass is
immutable; a later close failure is recorded separately by the coordinator.
The secure listener's touch_last_seen path is a non-strict writer and may
independently normalize unrelated entries while the lease is live. This
primitive's cleanup guarantee is scoped to its own removal: inside the cleanup
lock it re-reads the current raw store and verifies that removal touched only
the attempt entry present in that read.

Attestation note: current SPL relay enrollment verifies the home-signed ES256
attestation and uses its device_fp claim as the device identity; the relay does
not parse or recompute the client certificate. The certificate is for the home
secure listener's inner TLS authorization. The external proto/tokens.md text is
older and contradictory; this module follows current relay behavior.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import stat
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.think.link import client as link_client
from solstone.think.link.auth import (
    AuthorizedClients,
    StrictAuthorizationError,
    StrictAuthorizationReceipt,
)
from solstone.think.link.ca import (
    ca_is_present,
    cert_fingerprint,
    load_ca,
    mint_attestation,
    sign_csr,
)
from solstone.think.link.paths import DEFAULT_RELAY_URL
from solstone.think.sandbox_profile import probe_contract, spl_readiness

LOG = logging.getLogger(__name__)

WORK_DEADLINE_SECONDS = 60.0
ENROLLMENT_DEADLINE_SECONDS = 30.0
CLEANUP_SHIELD_SECONDS = 15.0
SPL_CHECKS = probe_contract.PROOF_CHECKS[probe_contract.CAPABILITY_SPL][:3]
ALLOWED_FAILED_REASONS = frozenset(
    {
        probe_contract.REASON_CAPABILITY_NOT_READY,
        probe_contract.REASON_DEADLINE_EXCEEDED,
        probe_contract.REASON_REMOTE_REJECTED,
        probe_contract.REASON_RESPONSE_INVALID,
        probe_contract.REASON_CLEANUP_UNVERIFIED,
        probe_contract.REASON_INTERNAL_ERROR,
    }
)


class SplRelayTunnelError(RuntimeError):
    """Stable-reason proof failure."""

    def __init__(
        self,
        reason: str,
        *,
        checks: tuple[str, ...] | None = None,
    ) -> None:
        self.reason = reason
        self.checks = checks
        super().__init__(reason)


class RelayTunnelCloseError(RuntimeError):
    """Stable-reason lease cleanup failure."""

    def __init__(self) -> None:
        self.reason = probe_contract.REASON_CLEANUP_UNVERIFIED
        super().__init__(self.reason)


@dataclass(frozen=True, slots=True)
class SplRelayTunnelOutcome:
    state: str
    checks: tuple[str, ...]
    reason: str | None
    duration_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            probe_contract.FIELD_STATE: self.state,
            probe_contract.FIELD_CHECKS: self.checks,
            probe_contract.FIELD_REASON: self.reason,
            probe_contract.FIELD_DURATION_MS: self.duration_ms,
        }


class RelayTunnelLease:
    def __init__(
        self,
        *,
        session: link_client.TunnelSession,
        store: AuthorizedClients,
        receipt: StrictAuthorizationReceipt,
    ) -> None:
        self._session: link_client.TunnelSession | None = session
        self._store = store
        self._receipt: StrictAuthorizationReceipt | None = receipt
        self._closed = False
        self._transport_closed = False

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"RelayTunnelLease(state={state}, <redacted>)"

    def __reduce__(self) -> object:
        raise TypeError("RelayTunnelLease is not serializable")

    def __copy__(self) -> object:
        raise TypeError("RelayTunnelLease is not copyable")

    def __deepcopy__(self, _memo: dict[int, object]) -> object:
        raise TypeError("RelayTunnelLease is not copyable")

    async def __aenter__(self) -> RelayTunnelLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | link_client.BodySource = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        session = self._require_session()
        return await session.request(method, path, headers=headers, body=body)

    async def stream_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | link_client.BodySource = b"",
    ) -> tuple[int, dict[str, str], bytes, object]:
        session = self._require_session()
        return await session.stream_request(method, path, headers=headers, body=body)

    async def close(self) -> None:
        if self._closed:
            return
        ok = await _cleanup_transport_and_authorization(
            session=self._session,
            store=self._store,
            receipt=self._receipt,
            transport_already_closed=self._transport_closed,
        )
        if not ok:
            raise RelayTunnelCloseError()
        self._closed = True
        self._transport_closed = True
        self._session = None
        self._receipt = None

    def _require_session(self) -> link_client.TunnelSession:
        if self._closed or self._session is None:
            raise RuntimeError("relay tunnel lease is closed")
        return self._session


async def prove_spl_relay_tunnel(
    journal: Path,
    *,
    attempt_dir: Path,
    cancel_requested: Callable[[], bool],
) -> tuple[RelayTunnelLease | None, dict[str, object]]:
    start = time.monotonic()
    work_deadline = start + WORK_DEADLINE_SECONDS
    checks: tuple[str, ...] = tuple(SPL_CHECKS[:0])
    session: link_client.TunnelSession | None = None
    store: AuthorizedClients | None = None
    receipt: StrictAuthorizationReceipt | None = None

    try:
        _check_cancel(cancel_requested)
        _validate_attempt_dir(Path(journal), attempt_dir)
        _assert_pinned_origin(Path(journal))
        with spl_readiness.open_spl_readiness_observer(Path(journal)) as observer:
            snapshot = observer.wait_snapshot(deadline=work_deadline)
            _check_cancel(cancel_requested)
            identity = _build_ephemeral_identity(Path(journal), attempt_dir)
            _assert_fingerprint_binding(identity)
            _assert_enrollment_origin()
            observer.reverify_before_authorization(snapshot)
            _check_cancel(cancel_requested)
            store = AuthorizedClients(_authorized_clients_path(Path(journal)))
            try:
                receipt = store.add_attempt_client_strict(
                    fingerprint=identity.fingerprint,
                    device_label=_authorization_label(attempt_dir),
                    instance_id=identity.home_instance_id,
                    network="pl-via-spl",
                )
            except StrictAuthorizationError as exc:
                if exc.mutated:
                    return None, _failed(
                        probe_contract.REASON_CLEANUP_UNVERIFIED,
                        checks,
                        _duration_ms(start),
                    ).to_dict()
                return None, _failed(
                    probe_contract.REASON_CAPABILITY_NOT_READY,
                    checks,
                    _duration_ms(start),
                ).to_dict()

            _check_cancel(cancel_requested)
            with _suppress_client_boundary_logs(identity.fingerprint):
                enrolled = await _enroll_with_deadline(identity, work_deadline)
                checks = tuple(SPL_CHECKS[:1])
                session = await _dial_with_deadline(enrolled, work_deadline)
                checks = tuple(SPL_CHECKS[:3])
            _redact_session_task_names(session)
            lease = RelayTunnelLease(session=session, store=store, receipt=receipt)
            return lease, _passed(_duration_ms(start)).to_dict()
    except asyncio.CancelledError:
        if receipt is None:
            raise
        ok = await _cleanup_with_shield(session=session, store=store, receipt=receipt)
        if ok:
            raise
        return None, _failed(
            probe_contract.REASON_CLEANUP_UNVERIFIED,
            checks,
            _duration_ms(start),
        ).to_dict()
    except SplRelayTunnelError as exc:
        if exc.checks is not None:
            checks = exc.checks
        ok = True
        if receipt is not None:
            ok = await _cleanup_with_shield(
                session=session, store=store, receipt=receipt
            )
        reason = exc.reason if ok else probe_contract.REASON_CLEANUP_UNVERIFIED
        return None, _failed(reason, checks, _duration_ms(start)).to_dict()
    except spl_readiness.SplReadinessError:
        return None, _failed(
            probe_contract.REASON_CAPABILITY_NOT_READY,
            checks,
            _duration_ms(start),
        ).to_dict()
    except Exception:
        ok = True
        if receipt is not None:
            ok = await _cleanup_with_shield(
                session=session, store=store, receipt=receipt
            )
        reason = (
            probe_contract.REASON_INTERNAL_ERROR
            if ok
            else probe_contract.REASON_CLEANUP_UNVERIFIED
        )
        return None, _failed(reason, checks, _duration_ms(start)).to_dict()


def _passed(duration_ms: int) -> SplRelayTunnelOutcome:
    return SplRelayTunnelOutcome(
        state=probe_contract.PROOF_STATE_PASSED,
        checks=tuple(SPL_CHECKS),
        reason=None,
        duration_ms=duration_ms,
    )


def _failed(
    reason: str,
    checks: tuple[str, ...],
    duration_ms: int,
) -> SplRelayTunnelOutcome:
    if reason not in ALLOWED_FAILED_REASONS:
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR)
    return SplRelayTunnelOutcome(
        state=probe_contract.PROOF_STATE_FAILED,
        checks=checks,
        reason=reason,
        duration_ms=duration_ms,
    )


async def _enroll_with_deadline(
    identity: link_client.ClientIdentity,
    work_deadline: float,
) -> link_client.EnrolledDevice:
    timeout = min(ENROLLMENT_DEADLINE_SECONDS, _remaining(work_deadline))
    if timeout <= 0:
        raise SplRelayTunnelError(probe_contract.REASON_DEADLINE_EXCEEDED)
    try:
        return await asyncio.wait_for(
            link_client.Client.enroll_device_async(
                DEFAULT_RELAY_URL,
                identity,
                timeout=timeout,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise SplRelayTunnelError(probe_contract.REASON_DEADLINE_EXCEEDED) from None
    except RuntimeError as exc:
        text = str(exc)
        if "missing string field" in text or "unexpected JSON response" in text:
            raise SplRelayTunnelError(probe_contract.REASON_RESPONSE_INVALID) from None
        raise SplRelayTunnelError(probe_contract.REASON_REMOTE_REJECTED) from None
    except Exception as exc:
        if exc.__class__.__name__.endswith("TimeoutException"):
            raise SplRelayTunnelError(probe_contract.REASON_DEADLINE_EXCEEDED) from None
        raise SplRelayTunnelError(probe_contract.REASON_REMOTE_REJECTED) from None


async def _dial_with_deadline(
    enrolled: link_client.EnrolledDevice,
    work_deadline: float,
) -> link_client.TunnelSession:
    url = link_client._relay_dial_url(DEFAULT_RELAY_URL, enrolled)
    _assert_ws_origin(url)
    timeout = _remaining(work_deadline)
    if timeout <= 0:
        raise SplRelayTunnelError(probe_contract.REASON_DEADLINE_EXCEEDED)
    try:
        link_client.LOG.info(
            "client %s: dialing %s",
            enrolled.identity.fingerprint,
            link_client._redact_url(url),
        )
        ws = await asyncio.wait_for(
            websockets.connect(url, max_size=None), timeout=timeout
        )
    except asyncio.TimeoutError:
        raise SplRelayTunnelError(probe_contract.REASON_DEADLINE_EXCEEDED) from None
    except Exception as exc:
        text = str(exc).lower()
        if "malformed" in text or "invalid" in text:
            raise SplRelayTunnelError(probe_contract.REASON_RESPONSE_INVALID) from None
        raise SplRelayTunnelError(probe_contract.REASON_REMOTE_REJECTED) from None

    try:
        session = await asyncio.wait_for(
            link_client._open_tunnel_session(
                link_client._WsEncryptedTransport(ws),
                enrolled.identity,
            ),
            timeout=max(0.0, _remaining(work_deadline)),
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            await ws.close()
        raise SplRelayTunnelError(
            probe_contract.REASON_DEADLINE_EXCEEDED,
            checks=tuple(SPL_CHECKS[:2]),
        ) from None
    except Exception as exc:
        with contextlib.suppress(Exception):
            await ws.close()
        if "handshake" in str(exc).lower() or "certificate" in str(exc).lower():
            raise SplRelayTunnelError(
                probe_contract.REASON_REMOTE_REJECTED,
                checks=tuple(SPL_CHECKS[:2]),
            ) from None
        raise SplRelayTunnelError(
            probe_contract.REASON_RESPONSE_INVALID,
            checks=tuple(SPL_CHECKS[:2]),
        ) from None
    if not session.is_alive:
        with contextlib.suppress(Exception):
            await session.close()
        raise SplRelayTunnelError(
            probe_contract.REASON_RESPONSE_INVALID,
            checks=tuple(SPL_CHECKS[:2]),
        )
    return session


async def _cleanup_with_shield(
    *,
    session: link_client.TunnelSession | None,
    store: AuthorizedClients | None,
    receipt: StrictAuthorizationReceipt,
) -> bool:
    try:
        return await asyncio.shield(
            asyncio.wait_for(
                _cleanup_transport_and_authorization(
                    session=session,
                    store=store,
                    receipt=receipt,
                ),
                timeout=CLEANUP_SHIELD_SECONDS,
            )
        )
    except Exception:
        return False


async def _cleanup_transport_and_authorization(
    *,
    session: link_client.TunnelSession | None,
    store: AuthorizedClients | None,
    receipt: StrictAuthorizationReceipt | None,
    transport_already_closed: bool = False,
) -> bool:
    if receipt is None or store is None:
        return session is None
    ok = True
    if session is not None and not transport_already_closed:
        try:
            await session.close()
        except Exception:
            return False
    try:
        store.remove_attempt_client_strict(receipt)
    except StrictAuthorizationError:
        ok = False
    return ok


def _check_cancel(cancel_requested: Callable[[], bool]) -> None:
    if cancel_requested():
        raise asyncio.CancelledError


def _duration_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _validate_attempt_dir(journal: Path, attempt_dir: Path) -> Path:
    parent = probe_contract.probe_attempts_parent_path(journal).resolve()
    try:
        current = attempt_dir.lstat()
    except OSError:
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR) from None
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR)
    if stat.S_IMODE(current.st_mode) != probe_contract.ATTEMPT_DIR_MODE:
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR)
    if attempt_dir.resolve().parent != parent:
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR)
    return attempt_dir.resolve()


def _authorization_label(attempt_dir: Path) -> str:
    digest = hashlib.sha256(attempt_dir.name.encode("ascii")).hexdigest()[:32]
    return f"sandbox-spl-{digest}"


def _build_ephemeral_identity(
    journal: Path,
    attempt_dir: Path,
) -> link_client.ClientIdentity:
    ca_root = journal / "link" / "ca"
    if not ca_is_present(ca_root):
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)
    try:
        home_ca = load_ca(ca_root)
        state = _read_link_state(journal)
    except (OSError, ValueError):
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    private_key, private_key_pem, csr_pem = _build_csr(
        _authorization_label(attempt_dir)
    )
    del private_key
    try:
        client_cert_pem, fingerprint = sign_csr(
            home_ca,
            csr_pem,
            _authorization_label(attempt_dir),
        )
        attestation = mint_attestation(home_ca, state["instance_id"], fingerprint)
    except (OSError, ValueError):
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    ca_chain_pem = home_ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return link_client.ClientIdentity(
        private_key_pem=private_key_pem.decode("ascii"),
        client_cert_pem=client_cert_pem,
        ca_chain_pem=ca_chain_pem,
        fingerprint=fingerprint,
        home_instance_id=state["instance_id"],
        home_label=state["home_label"],
        home_attestation=attestation,
        local_endpoints=(),
    )


def _build_csr(label: str) -> tuple[ec.EllipticCurvePrivateKey, bytes, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label[:64])]))
        .sign(private_key, hashes.SHA256())
    )
    private_key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        private_key,
        private_key_pem,
        csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )


def _read_link_state(journal: Path) -> dict[str, str]:
    try:
        raw = json.loads((journal / "link" / "state.json").read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    if not isinstance(raw, dict):
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)
    instance_id = raw.get("instance_id")
    home_label = raw.get("home_label") or "solstone"
    if not isinstance(instance_id, str) or not instance_id:
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)
    if not isinstance(home_label, str) or not home_label:
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)
    return {"instance_id": instance_id, "home_label": home_label}


def _assert_fingerprint_binding(identity: link_client.ClientIdentity) -> None:
    try:
        cert_fp = cert_fingerprint(identity.client_cert_pem)
        attestation_fp = _attestation_device_fp(identity.home_attestation)
    except (ValueError, json.JSONDecodeError):
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR) from None
    if cert_fp != identity.fingerprint or attestation_fp != identity.fingerprint:
        raise SplRelayTunnelError(probe_contract.REASON_INTERNAL_ERROR)


def _attestation_device_fp(attestation: str) -> str:
    parts = attestation.split(".")
    if len(parts) != 3:
        raise ValueError("bad attestation")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    value = claims.get("device_fp") if isinstance(claims, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError("bad device_fp")
    return value


def _assert_pinned_origin(journal: Path) -> None:
    if spl_readiness.observed_relay_origin(journal) != DEFAULT_RELAY_URL:
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)
    _assert_http_origin(f"{DEFAULT_RELAY_URL}/enroll/device")


def _assert_enrollment_origin() -> None:
    _assert_http_origin(link_client._enroll_device_endpoint(DEFAULT_RELAY_URL))


def _assert_http_origin(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if f"{parsed.scheme}://{parsed.netloc}" != DEFAULT_RELAY_URL:
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)


def _assert_ws_origin(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if f"{parsed.scheme}://{parsed.netloc}" != "wss://link.solstone.app":
        raise SplRelayTunnelError(probe_contract.REASON_CAPABILITY_NOT_READY)


def _authorized_clients_path(journal: Path) -> Path:
    return journal / "link" / "authorized_clients.json"


def _redact_session_task_names(session: link_client.TunnelSession) -> None:
    reader = getattr(session, "_reader_task", None)
    if isinstance(reader, asyncio.Task):
        reader.set_name("spl-proof-relay-reader")
    keepalive = getattr(session, "_keepalive_task", None)
    if isinstance(keepalive, asyncio.Task):
        keepalive.set_name("spl-proof-relay-keepalive")


class _ProofLogFilter(logging.Filter):
    def __init__(self, fingerprint: str) -> None:
        super().__init__()
        self._fingerprint = fingerprint

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != link_client.LOG.name:
            return True
        message = record.getMessage()
        if self._fingerprint in message and (
            "enrolling device token" in message
            or "enroll complete" in message
            or "dialing " in message
        ):
            return False
        return True


@contextlib.contextmanager
def _suppress_client_boundary_logs(fingerprint: str) -> Iterator[None]:
    logger = logging.getLogger(link_client.LOG.name)
    log_filter = _ProofLogFilter(fingerprint)
    logger.addFilter(log_filter)
    try:
        yield
    finally:
        logger.removeFilter(log_filter)


__all__ = [
    "CLEANUP_SHIELD_SECONDS",
    "ENROLLMENT_DEADLINE_SECONDS",
    "RelayTunnelCloseError",
    "RelayTunnelLease",
    "SplRelayTunnelError",
    "SplRelayTunnelOutcome",
    "WORK_DEADLINE_SECONDS",
    "prove_spl_relay_tunnel",
]

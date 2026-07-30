# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from solstone.convey.secure_listener.accept import SecureListener
from solstone.convey.secure_listener.admission import (
    SecureListenerAdmission,
    SecureListenerAdmissionConfig,
)
from solstone.convey.secure_listener.tls import (
    build_relaxed_server_context,
    build_server_context,
    issue_server_cert,
)
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.ca import LoadedCa, cert_fingerprint, load_or_generate_ca
from solstone.think.link.client import (
    ClientIdentity,
    TunnelSession,
    _open_tunnel_session,
    _TcpEncryptedTransport,
)
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import authorized_clients_path, ca_dir, nonces_path
from tests.link.certless_helpers import (
    DirectPairCandidate,
    DirectPairRequest,
    build_csr,
    make_convey_app,
    post_pair_framed,
)


@dataclass
class SecureListenerHarness:
    app: Any
    journal: Path
    ca: LoadedCa
    authorized: AuthorizedClients
    listener: SecureListener
    admission: SecureListenerAdmission
    host: str
    port: int

    @classmethod
    async def start(
        cls: type[SecureListenerHarness],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        link: dict[str, Any] | None = None,
        admission_config: SecureListenerAdmissionConfig | None = None,
    ) -> SecureListenerHarness:
        app, journal = make_convey_app(
            tmp_path,
            monkeypatch,
            link=link if link is not None else {"posture": "spl"},
        )
        ca = load_or_generate_ca(ca_dir())
        server_cert, server_key = issue_server_cert(ca)
        authorized = AuthorizedClients(authorized_clients_path())
        strict_tls_ctx = build_server_context(
            ca,
            server_cert,
            server_key,
            authorized,
        )
        relaxed_tls_ctx = build_relaxed_server_context(
            ca,
            server_cert,
            server_key,
            authorized,
        )
        admission = SecureListenerAdmission(
            admission_config
            or SecureListenerAdmissionConfig(
                capacity=4,
                streaming_capacity=4,
                refuse_when_full=False,
            ),
            thread_name_prefix="secure-listener-e2e-wsgi",
        )
        listener = SecureListener(
            app=app,
            strict_tls_ctx=strict_tls_ctx,
            relaxed_tls_ctx=relaxed_tls_ctx,
            authorized=authorized,
            admission=admission,
            callosum_emit=lambda _event, _fields: None,
            host="127.0.0.1",
            port=0,
        )
        await listener.start()
        sock = listener.sockets[0]
        host, port = sock.getsockname()[:2]
        return cls(
            app=app,
            journal=journal,
            ca=ca,
            authorized=authorized,
            listener=listener,
            admission=admission,
            host=str(host),
            port=int(port),
        )

    async def close(self) -> None:
        try:
            await self.listener.stop()
        finally:
            await asyncio.to_thread(
                self.admission.shutdown,
                wait=True,
                cancel_futures=True,
            )

    def seed_nonce(
        self,
        nonce: str,
        label: str,
        *,
        role: str = "",
        now: int | None = None,
    ) -> None:
        NonceStore(nonces_path()).add(nonce, label, role=role, now=now)

    def pair_url(self, nonce: str, *, path: str = "/app/network/pair") -> str:
        return f"https://{self.host}:{self.port}{path}?token={nonce}"


async def pair_and_open_session(
    harness: SecureListenerHarness,
    *,
    nonce: str,
    label: str,
) -> TunnelSession:
    private_key, private_key_pem, csr_pem = build_csr(label)
    harness.seed_nonce(nonce, label)
    response = await asyncio.to_thread(
        post_pair_framed,
        DirectPairRequest(
            candidates=(DirectPairCandidate(harness.host, harness.port),),
            path=f"/app/network/pair?token={nonce}",
            ca_fingerprint_pin=harness.ca.fingerprint_sha256(),
        ),
        {"csr": csr_pem, "device_label": label},
        private_key,
    )
    identity = ClientIdentity(
        private_key_pem=private_key_pem.decode("ascii"),
        client_cert_pem=response.client_cert,
        ca_chain_pem="".join(response.ca_chain),
        fingerprint=cert_fingerprint(response.client_cert),
        home_instance_id=response.instance_id,
        home_label=response.home_label,
        home_attestation=response.home_attestation,
        local_endpoints=tuple(response.local_endpoints),
    )
    reader, writer = await asyncio.open_connection(harness.host, harness.port)
    try:
        return await _open_tunnel_session(
            _TcpEncryptedTransport(reader, writer),
            identity,
        )
    except BaseException:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        raise

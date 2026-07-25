# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from solstone.convey.secure_listener.accept import SecureListener
from solstone.convey.secure_listener.tls import (
    build_relaxed_server_context,
    build_server_context,
    issue_server_cert,
)
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.ca import LoadedCa, load_or_generate_ca
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import authorized_clients_path, ca_dir, nonces_path
from tests.link.certless_helpers import make_convey_app


@dataclass
class SecureListenerHarness:
    app: Any
    journal: Path
    ca: LoadedCa
    authorized: AuthorizedClients
    listener: SecureListener
    executor: ThreadPoolExecutor
    host: str
    port: int

    @classmethod
    async def start(
        cls: type[SecureListenerHarness],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        link: dict[str, Any] | None = None,
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
        executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="secure-listener-e2e-wsgi",
        )
        listener = SecureListener(
            app=app,
            strict_tls_ctx=strict_tls_ctx,
            relaxed_tls_ctx=relaxed_tls_ctx,
            authorized=authorized,
            executor=executor,
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
            executor=executor,
            host=str(host),
            port=int(port),
        )

    async def close(self) -> None:
        try:
            await self.listener.stop()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)

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

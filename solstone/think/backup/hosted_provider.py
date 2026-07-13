# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Authenticated loopback credential provider for long operated restic runs."""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from solstone.think.backup.destination import Destination
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
    fetch_hosted_credentials,
    operated_repository,
)

_CREDENTIAL_PATH = "/credentials"


@dataclass(frozen=True)
class HostedResticSession:
    destination: Destination
    backend_env: Mapping[str, str]


class _CredentialState:
    def __init__(
        self,
        binding: HostedBinding,
        scope: str,
        initial_credentials: HostedCredentials,
    ) -> None:
        self.binding = binding
        self.scope = scope
        self._initial_credentials: HostedCredentials | None = initial_credentials
        self._lock = threading.Lock()

    def next_credentials(self) -> HostedCredentials:
        with self._lock:
            if self._initial_credentials is not None:
                credentials = self._initial_credentials
                self._initial_credentials = None
                return credentials
        return fetch_hosted_credentials(self.binding, scope=self.scope)


class _CredentialServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        state: _CredentialState,
        authorization_token: str,
    ) -> None:
        self.state = state
        self.authorization_token = authorization_token
        super().__init__(("127.0.0.1", 0), _CredentialHandler)


class _CredentialHandler(BaseHTTPRequestHandler):
    server: _CredentialServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != _CREDENTIAL_PATH:
            self.send_error(404)
            return
        if self.headers.get("Authorization") != self.server.authorization_token:
            self.send_error(401)
            return

        try:
            credentials = self.server.state.next_credentials()
        except HostedCredsUnavailable:
            self.send_error(503)
            return

        body = json.dumps(
            {
                "AccessKeyId": credentials.access_key_id,
                "SecretAccessKey": credentials.secret_access_key,
                "Token": credentials.session_token,
                "Expiration": credentials.expires_at,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def hosted_restic_session(
    binding: HostedBinding,
    *,
    scope: str,
    initial_credentials: HostedCredentials | None = None,
) -> Iterator[HostedResticSession]:
    credentials = initial_credentials or fetch_hosted_credentials(binding, scope=scope)
    authorization_token = secrets.token_urlsafe(32)
    state = _CredentialState(binding, scope, credentials)
    server = _CredentialServer(state, authorization_token)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="spb-credential-provider",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    try:
        yield HostedResticSession(
            destination=Destination(
                repository=operated_repository(binding, credentials),
                backend="s3",
                credentials={},
            ),
            backend_env={
                "AWS_CONTAINER_CREDENTIALS_FULL_URI": (
                    f"http://{host}:{port}{_CREDENTIAL_PATH}"
                ),
                "AWS_CONTAINER_AUTHORIZATION_TOKEN": authorization_token,
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


__all__ = ["HostedResticSession", "hosted_restic_session"]

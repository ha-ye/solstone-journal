#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from OpenSSL import SSL

from solstone.think.services.spp_attest.ratls import verify as ratls_verify
from solstone.think.services.spp_attest.ratls.channel import (
    RatlsEndpoint,
    establish_attested_channel,
)
from solstone.think.services.spp_attest.ratls.contract import (
    EXPORTER_BYTES,
    EXPORTER_LABEL,
    OWNER_NONCE_BYTES,
    PREFACE_MAGIC,
    exporter_context,
)


class Upstream:
    def __init__(self) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.request = b""
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass

    def _run(self) -> None:
        try:
            conn, _addr = self.listener.accept()
        except OSError:
            return
        with conn:
            self.request = _recv_http_request(conn)
            body = b'{"id":"ok","choices":[]}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
                + body
            )
        self.close()


class GatewayProcess:
    def __init__(
        self, gateway_path: Path, collector_path: Path, upstream_port: int
    ) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(gateway_path),
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                "0",
                "--upstream-port",
                str(upstream_port),
                "--collector-command",
                f"{sys.executable} {collector_path}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert self.process.stdout is not None
        ready = json.loads(self.process.stdout.readline())
        self.port = int(ready["port"])

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)


class AdversarialGateway:
    def __init__(
        self,
        gateway_module: ModuleType,
        collector_path: Path,
        mode: str,
        timeout_s: int,
    ) -> None:
        self.gateway = gateway_module
        self.collector_path = collector_path
        self.mode = mode
        self.timeout_s = timeout_s
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        try:
            self.listener.close()
        except OSError:
            pass

    def _collector(self):
        return self.gateway.CommandCollector(
            [sys.executable, str(self.collector_path)], self.timeout_s
        )

    def _run(self) -> None:
        try:
            raw, _addr = self.listener.accept()
        except OSError:
            return
        collector = self._collector()
        connection = None
        try:
            preface = self.gateway._recv_exact(
                raw, len(PREFACE_MAGIC) + OWNER_NONCE_BYTES
            )
            owner_nonce = preface[len(PREFACE_MAGIC) :]
            key_a = ec.generate_private_key(ec.SECP256R1())
            key_b = ec.generate_private_key(ec.SECP256R1())
            spki_a = _spki_der(key_a)

            if self.mode == "relay":
                evidence = collector.collect_composite(owner_nonce, spki_a)
                tls_key = key_b
            elif self.mode == "splice":
                foreign_nonce = b"s" * OWNER_NONCE_BYTES
                evidence = collector.collect_composite(foreign_nonce, spki_a)
                tls_key = key_a
            else:
                evidence = collector.collect_composite(owner_nonce, spki_a)
                tls_key = key_a

            cert = self.gateway._make_certificate(tls_key, evidence.to_der())
            connection = SSL.Connection(self.gateway._tls_context(tls_key, cert), raw)
            connection.setblocking(1)
            connection.set_accept_state()
            connection.do_handshake()

            if self.mode != "stale":
                return

            tls_exporter = connection.export_keying_material(
                EXPORTER_LABEL,
                EXPORTER_BYTES,
                exporter_context(owner_nonce, spki_a),
            )
            self.gateway._recv_proof_request(connection)
            proof = collector.collect_exporter_proof(
                owner_nonce,
                spki_a,
                b"x" * len(tls_exporter),
                evidence.gpu_envelope,
            )
            self.gateway._send_proof(connection, proof.to_der())
        except Exception:
            return
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            else:
                try:
                    raw.close()
                except OSError:
                    pass
            self.close()


def _spki_der(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _recv_http_request(conn: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body[:length]


def _recv_http_response(connection: SSL.Connection) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        data.extend(connection.recv(4096))
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        body += connection.recv(length - len(body))
    return head + b"\r\n\r\n" + body[:length]


def _load_gateway_module(gateway_path: Path) -> ModuleType:
    sys.path.insert(0, str(gateway_path.parent))
    spec = importlib.util.spec_from_file_location(
        "spp_ratls_gateway_harness", gateway_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("gateway_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_verifiers() -> None:
    ratls_verify.verify_quote = lambda **_kwargs: None


def _stub_composite_verifier(_bundle, **_kwargs):
    return object()


def _establish(port: int):
    return establish_attested_channel(
        RatlsEndpoint("127.0.0.1", port),
        owner_nonce=b"n" * OWNER_NONCE_BYTES,
        nvattest_dir=Path("."),
        now=datetime.now(timezone.utc),
        composite_verifier=_stub_composite_verifier,
        monotonic_now=time.monotonic,
    )


def run_positive(gateway_path: Path, collector_path: Path) -> str:
    upstream = Upstream()
    upstream.start()
    gateway = GatewayProcess(gateway_path, collector_path, upstream.port)
    try:
        channel = _establish(gateway.port)
        channel.tls.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: spp-engine\r\nContent-Length: 2\r\n\r\n{}"
        )
        response = _recv_http_response(channel.tls)
        channel.close()
        upstream.thread.join(timeout=5)
        if b"HTTP/1.1 200 OK" not in response:
            raise RuntimeError("positive_http_failed")
        if not upstream.request.startswith(b"POST /v1/chat/completions"):
            raise RuntimeError("positive_proxy_failed")
        return "verified"
    finally:
        gateway.close()
        upstream.close()


def run_adversarial(
    gateway_module: ModuleType,
    collector_path: Path,
    mode: str,
    expected_reason: str,
) -> str:
    gateway = AdversarialGateway(gateway_module, collector_path, mode, timeout_s=5)
    gateway.start()
    try:
        channel = _establish(gateway.port)
    except Exception as exc:
        reason = getattr(exc, "reason_code", type(exc).__name__)
        if reason != expected_reason:
            raise RuntimeError(
                f"expected {expected_reason}, observed {reason}"
            ) from exc
        return reason
    else:
        channel.close()
        raise RuntimeError("unexpected_success")
    finally:
        gateway.close()


def run_premature_inference(gateway_path: Path, collector_path: Path) -> str:
    upstream = Upstream()
    upstream.start()
    gateway = GatewayProcess(gateway_path, collector_path, upstream.port)
    raw = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    try:
        raw.sendall(PREFACE_MAGIC + b"p" * OWNER_NONCE_BYTES)
        context = SSL.Context(SSL.TLS_CLIENT_METHOD)
        context.set_min_proto_version(SSL.TLS1_3_VERSION)
        context.set_max_proto_version(SSL.TLS1_3_VERSION)
        context.set_verify(SSL.VERIFY_NONE, lambda *_args: True)
        connection = SSL.Connection(context, raw)
        connection.setblocking(1)
        connection.set_connect_state()
        connection.set_tlsext_host_name(b"spp-engine")
        connection.do_handshake()
        connection.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: spp-engine\r\nContent-Length: 2\r\n\r\n{}"
        )
        try:
            if connection.recv(1) != b"":
                raise RuntimeError("premature_inference_not_rejected")
        except (SSL.ZeroReturnError, SSL.SysCallError, ConnectionError, OSError):
            pass
        upstream.thread.join(timeout=0.5)
        if upstream.request:
            raise RuntimeError("premature_inference_reached_upstream")
        connection.close()
        return "protocol_or_tls_rejected"
    finally:
        raw.close()
        gateway.close()
        upstream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    args = parser.parse_args()

    _stub_verifiers()
    gateway_module = _load_gateway_module(args.gateway)
    cases: list[tuple[str, Callable[[], str]]] = [
        ("positive", lambda: run_positive(args.gateway, args.collector)),
        (
            "relay",
            lambda: run_adversarial(
                gateway_module, args.collector, "relay", "spki_mismatch"
            ),
        ),
        (
            "splice",
            lambda: run_adversarial(
                gateway_module, args.collector, "splice", "nonce_mismatch"
            ),
        ),
        (
            "stale",
            lambda: run_adversarial(
                gateway_module, args.collector, "stale", "exporter_mismatch"
            ),
        ),
        (
            "premature-inference",
            lambda: run_premature_inference(args.gateway, args.collector),
        ),
    ]

    failed = False
    for name, runner in cases:
        try:
            reason = runner()
        except Exception as exc:
            failed = True
            print(f"{name}: FAIL {type(exc).__name__}")
        else:
            print(f"{name}: PASS {reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

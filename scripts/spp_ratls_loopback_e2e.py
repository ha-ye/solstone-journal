#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from OpenSSL import SSL

from solstone.think.services.spp_attest.composite import (
    CompositeVerdict,
    verify_composite,
)
from solstone.think.services.spp_attest.nvgpu.binary import locate_nvattest
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.ratls import verify as ratls_verify
from solstone.think.services.spp_attest.ratls.channel import (
    AttestedChannel,
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

DEFAULT_REQUEST_BODY = b"{}"
DEFAULT_BANNER = (
    "mode: default synthetic loopback; verifier stubs installed; "
    "protocol regression only, not proof of real appraisal"
)
REAL_BANNER = "mode: real loopback; production verifiers against live hardware"


@dataclass(frozen=True, slots=True)
class RealModeConfig:
    nvattest_dir: Path
    upstream_port: int
    request_body: bytes


@dataclass(frozen=True, slots=True)
class RunContext:
    establish: Callable[[int], AttestedChannel]
    upstream_port: int | None
    request_body: bytes
    banner: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the SPP RA-TLS loopback harness. Default mode runs the five "
            "protocol-regression cases with synthetic upstream traffic and "
            "verifier stubs; it is not proof of real hardware appraisal."
        ),
        epilog=(
            "Real mode (--real) runs the same five cases with production verifiers. "
            "It requires live confidential hardware, a real collector and gateway, "
            "an nvattest install, and a separately-running loopback upstream "
            "listening on 127.0.0.1:<upstream-port>. --real requires "
            "--nvattest-dir, --upstream-port, and --request-body; those flags "
            "are rejected without --real."
        ),
    )
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument(
        "--real",
        action="store_true",
        help="run the same five cases with production verifiers against live hardware",
    )
    parser.add_argument(
        "--nvattest-dir",
        type=Path,
        help=(
            "nvattest install root containing bin/nvattest, lib/, and "
            "share/ca/ca-bundle.pem (requires --real)"
        ),
    )
    parser.add_argument(
        "--upstream-port",
        type=int,
        help=(
            "port of the separately-running 127.0.0.1 loopback upstream "
            "(requires --real)"
        ),
    )
    parser.add_argument(
        "--request-body",
        type=Path,
        help=(
            "path to the raw JSON request body sent to /v1/chat/completions "
            "(requires --real)"
        ),
    )
    return parser


def validate_runtime_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> RealModeConfig | None:
    real_only = {
        "--nvattest-dir": args.nvattest_dir,
        "--upstream-port": args.upstream_port,
        "--request-body": args.request_body,
    }
    provided_real_only = [
        name for name, value in real_only.items() if value is not None
    ]
    if not args.real:
        if provided_real_only:
            parser.error(f"{', '.join(provided_real_only)} require --real")
        return None

    missing = [name for name, value in real_only.items() if value is None]
    if missing:
        parser.error(f"--real requires {', '.join(missing)}")

    try:
        request_body = args.request_body.read_bytes()
    except OSError:
        parser.error(f"unable to read --request-body: {args.request_body}")
    try:
        json.loads(request_body)
    except ValueError:
        parser.error("--request-body must contain valid JSON")

    try:
        locate_nvattest(args.nvattest_dir)
    except GpuAppraisalError:
        parser.error(
            "--nvattest-dir must contain bin/nvattest, lib/, and share/ca/ca-bundle.pem"
        )

    return RealModeConfig(
        nvattest_dir=args.nvattest_dir.resolve(),
        upstream_port=args.upstream_port,
        request_body=request_body,
    )


def build_chat_completions_request(body: bytes, *, host: str = "spp-engine") -> bytes:
    return (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + f"Host: {host}\r\n".encode("ascii")
        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


def validate_chat_completion_envelope(response: bytes) -> None:
    try:
        _head, body = response.split(b"\r\n\r\n", 1)
        data = json.loads(body)
    except ValueError:
        raise RuntimeError("response_envelope_invalid") from None
    if not isinstance(data, dict):
        raise RuntimeError("response_envelope_invalid")
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise RuntimeError("response_envelope_invalid")
    if data.get("object") != "chat.completion":
        raise RuntimeError("response_envelope_invalid")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("response_envelope_invalid")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError("response_envelope_invalid")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("response_envelope_invalid")
    if message.get("role") != "assistant":
        raise RuntimeError("response_envelope_invalid")
    if "content" not in message:
        raise RuntimeError("response_envelope_invalid")
    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str) or not finish_reason:
        raise RuntimeError("response_envelope_invalid")


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


def _establish(
    port: int,
    *,
    owner_nonce: bytes,
    nvattest_dir: Path,
    composite_verifier: Callable[..., CompositeVerdict],
) -> AttestedChannel:
    return establish_attested_channel(
        RatlsEndpoint("127.0.0.1", port),
        owner_nonce=owner_nonce,
        nvattest_dir=nvattest_dir,
        now=datetime.now(timezone.utc),
        composite_verifier=composite_verifier,
        monotonic_now=time.monotonic,
        epoch=0,
    )


def _establish_default(port: int) -> AttestedChannel:
    return _establish(
        port,
        owner_nonce=b"n" * OWNER_NONCE_BYTES,
        nvattest_dir=Path("."),
        composite_verifier=cast(
            Callable[..., CompositeVerdict],
            _stub_composite_verifier,
        ),
    )


def _establish_real(port: int, *, nvattest_dir: Path) -> AttestedChannel:
    return _establish(
        port,
        owner_nonce=secrets.token_bytes(OWNER_NONCE_BYTES),
        nvattest_dir=nvattest_dir,
        composite_verifier=verify_composite,
    )


def make_run_context(config: RealModeConfig | None) -> RunContext:
    if config is None:
        _stub_verifiers()
        return RunContext(
            establish=_establish_default,
            upstream_port=None,
            request_body=DEFAULT_REQUEST_BODY,
            banner=DEFAULT_BANNER,
        )
    return RunContext(
        establish=lambda port: _establish_real(port, nvattest_dir=config.nvattest_dir),
        upstream_port=config.upstream_port,
        request_body=config.request_body,
        banner=REAL_BANNER,
    )


def run_positive(gateway_path: Path, collector_path: Path, context: RunContext) -> str:
    request = build_chat_completions_request(context.request_body)
    upstream = None
    upstream_port = context.upstream_port
    if upstream_port is None:
        upstream = Upstream()
        upstream.start()
        upstream_port = upstream.port

    gateway = GatewayProcess(gateway_path, collector_path, upstream_port)
    channel = None
    try:
        channel = context.establish(gateway.port)
        channel.tls.sendall(request)
        response = _recv_http_response(channel.tls)
        if b"HTTP/1.1 200 OK" not in response:
            raise RuntimeError("positive_http_failed")
        if upstream is not None:
            upstream.thread.join(timeout=5)
            # The forwarded request line, not the verbatim bytes: a gateway may
            # rewrite or add headers while proxying. Byte-exact construction of
            # the caller's request is a property of build_chat_completions_request
            # and is proven there.
            if not upstream.request.startswith(b"POST /v1/chat/completions"):
                raise RuntimeError("positive_proxy_failed")
        else:
            validate_chat_completion_envelope(response)
            substrate = channel.verdict.substrate
            if not substrate:
                raise RuntimeError("positive_substrate_missing")
            return f"verified substrate={substrate}"
        return "verified"
    finally:
        if channel is not None:
            channel.close()
        gateway.close()
        if upstream is not None:
            upstream.close()


def run_adversarial(
    gateway_module: ModuleType,
    collector_path: Path,
    context: RunContext,
    mode: str,
    expected_reason: str,
) -> str:
    gateway = AdversarialGateway(gateway_module, collector_path, mode, timeout_s=5)
    gateway.start()
    try:
        channel = context.establish(gateway.port)
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


def run_premature_inference(
    gateway_path: Path,
    collector_path: Path,
    context: RunContext,
) -> str:
    request = build_chat_completions_request(context.request_body)
    upstream = None
    upstream_port = context.upstream_port
    if upstream_port is None:
        upstream = Upstream()
        upstream.start()
        upstream_port = upstream.port

    gateway = GatewayProcess(gateway_path, collector_path, upstream_port)
    raw = socket.create_connection(("127.0.0.1", gateway.port), timeout=5)
    connection = None
    try:
        raw.sendall(PREFACE_MAGIC + b"p" * OWNER_NONCE_BYTES)
        tls_context = SSL.Context(SSL.TLS_CLIENT_METHOD)
        tls_context.set_min_proto_version(SSL.TLS1_3_VERSION)
        tls_context.set_max_proto_version(SSL.TLS1_3_VERSION)
        tls_context.set_verify(SSL.VERIFY_NONE, lambda *_args: True)
        connection = SSL.Connection(tls_context, raw)
        connection.setblocking(1)
        connection.set_connect_state()
        connection.set_tlsext_host_name(b"spp-engine")
        connection.do_handshake()
        connection.sendall(request)
        try:
            if connection.recv(1) != b"":
                raise RuntimeError("premature_inference_not_rejected")
        except (SSL.ZeroReturnError, SSL.SysCallError, ConnectionError, OSError):
            pass
        if upstream is not None:
            upstream.thread.join(timeout=0.5)
            if upstream.request:
                raise RuntimeError("premature_inference_reached_upstream")
        return "protocol_or_tls_rejected"
    finally:
        if connection is not None:
            connection.close()
        raw.close()
        gateway.close()
        if upstream is not None:
            upstream.close()


def build_cases(
    gateway_path: Path,
    collector_path: Path,
    gateway_module: ModuleType,
    context: RunContext,
) -> list[tuple[str, Callable[[], str]]]:
    return [
        ("positive", lambda: run_positive(gateway_path, collector_path, context)),
        (
            "relay",
            lambda: run_adversarial(
                gateway_module,
                collector_path,
                context,
                "relay",
                "spki_mismatch",
            ),
        ),
        (
            "splice",
            lambda: run_adversarial(
                gateway_module,
                collector_path,
                context,
                "splice",
                "nonce_mismatch",
            ),
        ),
        (
            "stale",
            lambda: run_adversarial(
                gateway_module,
                collector_path,
                context,
                "stale",
                "exporter_mismatch",
            ),
        ),
        (
            "premature-inference",
            lambda: run_premature_inference(gateway_path, collector_path, context),
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = validate_runtime_args(args, parser)
    context = make_run_context(config)
    gateway_module = _load_gateway_module(args.gateway)
    cases = build_cases(args.gateway, args.collector, gateway_module, context)

    print(context.banner)
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

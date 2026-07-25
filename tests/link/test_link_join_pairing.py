# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import inspect
import ipaddress
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.apps.network.routes import _build_pair_link, _build_pair_link_v05
from solstone.convey.secure_listener.framing import RESET_INTERNAL_ERROR
from solstone.convey.secure_listener.mux import RESET_CTX_HANDLER_EXCEPTION
from solstone.convey.secure_listener.tls import issue_server_cert
from solstone.think.link import client as link_client
from solstone.think.link import join_cli
from solstone.think.link.ca import (
    ca_pin_matches,
    generate_ca,
    load_or_generate_ca,
    sign_csr,
)
from solstone.think.link.client import StreamResetError
from solstone.think.link.paths import LinkState
from solstone.think.link.tls import TlsError
from tests.link.pairing_harness import PairingHarness, pairing_harness


def _args(
    *,
    home: str | None = None,
    code: str,
    as_role: str | None = None,
    label: str = "laptop",
) -> argparse.Namespace:
    return argparse.Namespace(home=home, code=code, as_role=as_role, label=label)


def _csr_material(label: str = "laptop") -> tuple[Any, dict[str, str]]:
    private_key, _private_key_pem, csr_pem = join_cli._build_csr(label)
    return private_key, {"csr": csr_pem, "device_label": label}


def _csr_body(label: str = "laptop") -> dict[str, str]:
    _private_key, body = _csr_material(label)
    return body


def _direct_request_from_url(
    url: str,
    ca_fp: str,
) -> join_cli.DirectPairRequest:
    host, port, path = join_cli._framed_target(url)
    return join_cli.DirectPairRequest(
        candidates=(join_cli.DirectPairCandidate(ipaddress.IPv4Address(host), port),),
        path=path,
        ca_fingerprint_pin=ca_fp,
    )


def _direct_request(
    *,
    host: str = "127.0.0.1",
    port: int = 7657,
    token: str = "x",
    ca_fp: str = "ab" * 16,
) -> join_cli.DirectPairRequest:
    return join_cli.DirectPairRequest(
        candidates=(join_cli.DirectPairCandidate(ipaddress.IPv4Address(host), port),),
        path=f"/app/network/pair?token={token}",
        ca_fingerprint_pin=ca_fp,
    )


def _disable_direct_cert_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(join_cli, "_verify_direct_ready", lambda *_args: None)
    monkeypatch.setattr(join_cli, "_verify_returned_ca", lambda *_args: object())
    monkeypatch.setattr(
        join_cli,
        "_validate_returned_client_cert",
        lambda *_args: None,
    )


def _pair_payload(
    harness: PairingHarness,
    *,
    instance_id: str = "inst-1",
    csr_pem: str | None = None,
    device_label: str = "laptop",
    local_endpoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _pair_payload_from_ca(
        harness.ca,
        instance_id=instance_id,
        csr_pem=csr_pem,
        device_label=device_label,
        local_endpoints=local_endpoints,
    )


def _pair_payload_from_ca(
    ca: Any,
    *,
    instance_id: str = "inst-1",
    csr_pem: str | None = None,
    device_label: str = "laptop",
    local_endpoints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ca_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    if csr_pem is None:
        _private_key, body = _csr_material(device_label)
        csr_pem = body["csr"]
    client_cert, _fingerprint = sign_csr(ca, csr_pem, device_label)
    return {
        "client_cert": client_cert,
        "ca_chain": [ca_pem],
        "instance_id": instance_id,
        "home_label": "solstone",
        "home_attestation": "header.payload.signature",
        "fingerprint": "sha256:client",
        "local_endpoints": local_endpoints
        if local_endpoints is not None
        else [{"host": "127.0.0.1", "port": 7657}],
    }


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    reason: str = "OK",
    content_type: str = "application/json",
) -> bytes:
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


def _json_response(
    payload: dict[str, Any],
    *,
    status: int = 200,
    reason: str = "OK",
) -> bytes:
    return _http_response(
        json.dumps(payload).encode("utf-8"),
        status=status,
        reason=reason,
    )


async def _read_request_json(reader) -> dict[str, Any]:
    raw = await reader.read()
    _head, body = raw.split(b"\r\n\r\n", 1)
    parsed = json.loads(body.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _closed_loopback_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self.wait_closed_called = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


class _AsyncioShim:
    def __init__(self, real_asyncio: Any, open_connection: Any) -> None:
        self._real_asyncio = real_asyncio
        self.open_connection = open_connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_asyncio, name)


class _FakeSession:
    def __init__(
        self,
        exc: Exception | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._exc = exc
        self._payload = payload or {
            "client_cert": "client-cert",
            "ca_chain": ["ca-cert"],
            "instance_id": "inst-1",
            "home_label": "solstone",
            "home_attestation": "header.payload.signature",
            "local_endpoints": [{"host": "127.0.0.1", "port": 7657}],
        }
        self.closed = False
        self.requests: list[tuple[object, ...]] = []
        self.request_kwargs: list[dict[str, object]] = []

    async def request(
        self, *_args: object, **_kwargs: object
    ) -> tuple[int, dict, bytes]:
        self.requests.append((*_args,))
        self.request_kwargs.append(dict(_kwargs))
        if self._exc is not None:
            raise self._exc
        return 200, {}, json.dumps(self._payload).encode("utf-8")

    async def close(self) -> None:
        self.closed = True


def _single_line(text: str) -> None:
    assert "\n" not in text.strip()


def test_framed_join_uses_certless_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 3: framed pair-link joins do not construct or require ClientIdentity.
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    def fail_cert_bearing_path(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cert-bearing client path should not be used")

    captured_identities: list[object] = []
    original_init = link_client.TunnelSession.__init__

    def spy_init(self, *args: object, **kwargs: object) -> None:
        captured_identities.append(kwargs.get("identity"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(link_client, "ClientIdentity", fail_cert_bearing_path)
    monkeypatch.setattr(link_client, "_build_tls_client_ctx", fail_cert_bearing_path)
    monkeypatch.setattr(link_client.TunnelSession, "__init__", spy_init)

    nonce = "10000000000000000000000000000001"
    with pairing_harness(tmp_path, monkeypatch) as harness:
        harness.seed_nonce(nonce, "laptop")
        result = join_cli.main(_args(code=harness.pair_link(nonce)))

    assert result == 0
    assert captured_identities == [None]
    assert (config_home / "solstone-observer" / "spl" / "laptop").is_dir()


def test_post_pair_framed_checks_ca_pin_after_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live TLS connection authenticated under CA A must still reject a
    # response that returns CA B and a B-signed client certificate.
    ca_b = generate_ca(tmp_path / "ca-b")
    private_key, body = _csr_material("pin-phone")

    async def handler(reader, writer) -> None:
        request = await _read_request_json(reader)
        await writer.write(
            _json_response(
                _pair_payload_from_ca(
                    ca_b,
                    csr_pem=request["csr"],
                    device_label=request["device_label"],
                )
            )
        )
        await writer.close()

    nonce = "10000000000000000000000000000002"
    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        harness.seed_nonce(nonce, "pin-phone")
        request = _direct_request_from_url(
            harness.pair_url(nonce),
            harness.ca.fingerprint_sha256(),
        )
        with pytest.raises(ValueError) as exc_info:
            join_cli._post_pair_framed(request, body, private_key)

    assert "CA fingerprint mismatch" in str(exc_info.value)


def test_returned_ca_pin_binds_first_persisted_chain_cert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ca_b = generate_ca(tmp_path / "ca-b-first")
    ca_b_pem = ca_b.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    requests = 0

    async def handler(reader, writer) -> None:
        nonlocal requests
        request = await _read_request_json(reader)
        requests += 1
        client_cert, _fingerprint = sign_csr(
            state["harness"].ca,
            request["csr"],
            request["device_label"],
        )
        pinned_ca_pem = (
            state["harness"]
            .ca.cert.public_bytes(serialization.Encoding.PEM)
            .decode("ascii")
        )
        await writer.write(
            _json_response(
                {
                    "client_cert": client_cert,
                    "ca_chain": [ca_b_pem, pinned_ca_pem],
                    "instance_id": "inst-1",
                    "home_label": "solstone",
                    "home_attestation": "header.payload.signature",
                    "fingerprint": "sha256:client",
                    "local_endpoints": [{"host": "127.0.0.1", "port": 7657}],
                }
            )
        )
        await writer.close()

    nonce = "10000000000000000000000000000035"
    state: dict[str, PairingHarness] = {}
    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        state["harness"] = harness
        harness.seed_nonce(nonce, "laptop")
        result = join_cli.main(_args(code=harness.pair_link(nonce)))

    err = capsys.readouterr().err.strip()
    assert result == 1
    assert requests == 1
    assert err == (
        "CA fingerprint mismatch: the pinned CA does not match the pair-link."
    )
    _single_line(err)
    assert harness.ca.fingerprint_sha256() not in err
    assert ca_b.fingerprint_sha256() not in err
    assert not (tmp_path / "config" / "solstone-observer" / "spl" / "laptop").exists()


def test_peer_pair_link_sends_sender_instance_id_and_writes_peer_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 7: peer pair-links use framed transport and include sender_instance_id.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    captured: dict[str, Any] = {}
    state: dict[str, PairingHarness] = {}

    async def handler(reader, writer) -> None:
        captured.update(await _read_request_json(reader))
        await writer.write(
            _json_response(
                _pair_payload(
                    state["harness"],
                    csr_pem=captured["csr"],
                    device_label=captured["device_label"],
                )
            )
        )
        await writer.close()

    nonce = "10000000000000000000000000000005"
    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        state["harness"] = harness
        expected_instance_id = LinkState.load_or_create().instance_id
        result = join_cli.main(
            _args(
                code=harness.pair_link(nonce),
                as_role="peer",
                label="my-peer",
            )
        )

    assert result == 0
    assert captured["sender_instance_id"] == expected_instance_id
    assert captured["device_label"] == "my-peer"
    assert isinstance(captured["csr"], str)
    bundle = tmp_path / "journal" / "peers" / "inst-1"
    for name in join_cli.BUNDLE_FILES:
        assert (bundle / name).exists()


def test_post_pair_framed_returns_plain_response_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 8: sync wrapper returns a PairResponse after closing the transport.
    private_key, body = _csr_material("sync-phone")
    sessions = [_FakeSession(), _FakeSession()]
    _disable_direct_cert_validation(monkeypatch)

    async def fake_open_connection(_host: str, _port: int):
        return link_client.asyncio.StreamReader(), _FakeWriter()

    async def fake_open_pairing_session(_transport):
        return sessions.pop(0)

    monkeypatch.setattr(
        join_cli,
        "asyncio",
        _AsyncioShim(join_cli.asyncio, fake_open_connection),
    )
    monkeypatch.setattr(join_cli, "_open_pairing_session", fake_open_pairing_session)

    first_session = sessions[0]
    first = join_cli._post_pair_framed(
        _direct_request(token="one"),
        body,
        private_key,
    )
    assert first_session.closed is True

    second_session = sessions[0]
    second = join_cli._post_pair_framed(
        _direct_request(token="two"),
        body,
        private_key,
    )
    assert second_session.closed is True

    assert isinstance(first, join_cli.PairResponse)
    assert isinstance(second, join_cli.PairResponse)
    assert not inspect.isawaitable(first)


def test_post_pair_framed_requires_explicit_port() -> None:
    # Criterion 9: pair-link framed targets must include an explicit port.
    with pytest.raises(ValueError) as exc_info:
        join_cli._framed_target("https://receiver/app/network/pair?token=x")

    assert "missing explicit port" in str(exc_info.value)
    _single_line(str(exc_info.value))


def test_post_pair_framed_reassembles_multiframe_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 10: multiple DATA frames are reassembled before response parsing.
    state: dict[str, PairingHarness] = {}

    async def handler(reader, writer) -> None:
        request = await _read_request_json(reader)
        response = _json_response(
            _pair_payload(
                state["harness"],
                csr_pem=request["csr"],
                device_label=request["device_label"],
            )
        )
        await writer.write(response[:25])
        await writer.write(response[25:80])
        await writer.write(response[80:])
        await writer.close()

    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        state["harness"] = harness
        private_key, body = _csr_material("multi-phone")
        response = join_cli._post_pair_framed(
            _direct_request_from_url(
                harness.pair_url("10000000000000000000000000000008"),
                harness.ca.fingerprint_sha256(),
            ),
            body,
            private_key,
        )

    assert response.instance_id == "inst-1"
    assert response.local_endpoints == [{"host": "127.0.0.1", "port": 7657}]


def test_framed_non_200_is_single_line_window_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Criterion 11: framed non-200 responses map to the window/used-code message.
    async def handler(reader, writer) -> None:
        await reader.read()
        await writer.write(
            _http_response(
                b"gone", status=410, reason="Gone", content_type="text/plain"
            )
        )
        await writer.close()

    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        result = join_cli.main(
            _args(code=harness.pair_link("10000000000000000000000000000009"))
        )

    err = capsys.readouterr().err.strip()
    assert result == 1
    assert "pairing window is closed or the code was already used" in err
    _single_line(err)


def test_framed_midstream_reset_is_single_line_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Criterion 12: mid-stream RESET has a distinct reset/closed message.
    async def handler(reader, writer) -> None:
        await reader.read()
        await writer.write(b"HTTP/1.1 200 OK\r\n")
        await writer.reset(RESET_INTERNAL_ERROR, RESET_CTX_HANDLER_EXCEPTION)

    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        result = join_cli.main(
            _args(code=harness.pair_link("1000000000000000000000000000000a"))
        )

    err = capsys.readouterr().err.strip()
    assert result == 1
    assert err == "Pairing stream reset or closed before a response was received."
    assert "Could not connect" not in err
    _single_line(err)


def test_framed_connect_refused_is_single_line_error() -> None:
    # Criterion 13: closed loopback ports map to connect errors, not hangs.
    port = _closed_loopback_port()
    private_key, body = _csr_material()

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            _direct_request(port=port),
            body,
            private_key,
        )

    message = str(exc_info.value)
    assert message.startswith(f"Could not connect to 127.0.0.1:{port}:")
    _single_line(message)


def test_framed_tls_failure_is_single_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 13: TLS handshake failures get their own single-line message.
    private_key, body = _csr_material()

    async def fake_open_connection(_host: str, _port: int):
        return link_client.asyncio.StreamReader(), _FakeWriter()

    async def fail_tls(_transport):
        raise TlsError("bad test handshake")

    monkeypatch.setattr(
        join_cli, "asyncio", _AsyncioShim(join_cli.asyncio, fake_open_connection)
    )
    monkeypatch.setattr(join_cli, "_open_pairing_session", fail_tls)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            _direct_request(port=1),
            body,
            private_key,
        )

    message = str(exc_info.value)
    assert message == "TLS handshake with 127.0.0.1:1 failed: bad test handshake"
    _single_line(message)


def test_framed_handshake_then_drop_is_single_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 13: post-handshake drops share the reset/closed taxonomy.
    private_key, body = _csr_material()
    _disable_direct_cert_validation(monkeypatch)

    async def fake_open_connection(_host: str, _port: int):
        return link_client.asyncio.StreamReader(), _FakeWriter()

    async def fake_open_pairing_session(_transport):
        return _FakeSession(StreamResetError("closed after handshake"))

    monkeypatch.setattr(
        join_cli, "asyncio", _AsyncioShim(join_cli.asyncio, fake_open_connection)
    )
    monkeypatch.setattr(join_cli, "_open_pairing_session", fake_open_pairing_session)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            _direct_request(port=1),
            body,
            private_key,
        )

    message = str(exc_info.value)
    assert message == "Pairing stream reset or closed before a response was received."
    _single_line(message)


def test_framed_connect_timeout_is_single_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 13: connection timeout maps to a timeout message without waiting 15s.
    private_key, body = _csr_material()

    async def hang_open_connection(_host: str, _port: int):
        await link_client.asyncio.sleep(3600)

    monkeypatch.setattr(
        join_cli, "asyncio", _AsyncioShim(join_cli.asyncio, hang_open_connection)
    )
    monkeypatch.setattr(join_cli, "_CONNECT_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            _direct_request(port=1),
            body,
            private_key,
        )

    message = str(exc_info.value)
    assert message == "Timed out connecting to 127.0.0.1:1."
    _single_line(message)


def test_duplicate_endpoints_prepare_once_and_exhaust_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, body = _csr_material()
    req = join_cli.DirectPairRequest(
        candidates=(
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.1"), 7657),
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.1"), 7657),
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.2"), 7657),
        ),
        path="/app/network/pair?token=one",
        ca_fingerprint_pin="ab" * 16,
    )
    attempts: list[join_cli.PairTarget] = []
    active = 0
    max_active = 0

    async def fail_prepare(
        target: join_cli.PairTarget,
        _pin: str,
    ) -> join_cli.TunnelSession:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        attempts.append(target)
        active -= 1
        raise ValueError(f"pre-request failure for {target.host}:{target.port}")

    monkeypatch.setattr(join_cli, "_open_ready_pairing_session", fail_prepare)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(req, body, private_key)

    assert attempts == [
        join_cli.PairTarget(
            host="10.0.0.1",
            port=7657,
            path="/app/network/pair?token=one",
        ),
        join_cli.PairTarget(
            host="10.0.0.2",
            port=7657,
            path="/app/network/pair?token=one",
        ),
    ]
    assert max_active == 1
    assert str(exc_info.value) == "pre-request failure for 10.0.0.2:7657"


def test_pre_request_failure_advances_then_ready_candidate_receives_sole_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    private_key, body = _csr_material("advance-phone")
    session = _FakeSession()
    req = join_cli.DirectPairRequest(
        candidates=(
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.1"), 7657),
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.2"), 7657),
        ),
        path="/app/network/pair?token=advance",
        ca_fingerprint_pin="ab" * 16,
    )
    attempts: list[join_cli.PairTarget] = []
    _disable_direct_cert_validation(monkeypatch)

    async def prepare(
        target: join_cli.PairTarget,
        _pin: str,
    ) -> _FakeSession:
        attempts.append(target)
        if len(attempts) == 1:
            raise ValueError("first candidate not ready")
        return session

    monkeypatch.setattr(join_cli, "_open_ready_pairing_session", prepare)

    response = join_cli._post_pair_framed(req, body, private_key)

    assert response.instance_id == "inst-1"
    assert [target.host for target in attempts] == ["10.0.0.1", "10.0.0.2"]
    assert len(session.requests) == 1
    assert session.closed is True


def test_request_invocation_is_terminal_and_closes_without_later_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, body = _csr_material("commit-phone")
    first = _FakeSession(OSError("write failed"))
    req = join_cli.DirectPairRequest(
        candidates=(
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.1"), 7657),
            join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.2"), 7657),
        ),
        path="/app/network/pair?token=commit",
        ca_fingerprint_pin="ab" * 16,
    )
    attempts: list[join_cli.PairTarget] = []

    async def prepare(
        target: join_cli.PairTarget,
        _pin: str,
    ) -> _FakeSession:
        attempts.append(target)
        return first

    monkeypatch.setattr(join_cli, "_open_ready_pairing_session", prepare)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(req, body, private_key)

    assert str(exc_info.value) == "Pairing request failed: write failed"
    assert [target.host for target in attempts] == ["10.0.0.1"]
    assert len(first.requests) == 1
    assert first.closed is True


def test_request_response_deadline_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key, body = _csr_material("timeout-phone")

    class SlowSession(_FakeSession):
        async def request(self, *_args: object, **_kwargs: object):
            self.requests.append((*_args,))
            await link_client.asyncio.sleep(3600)
            return await super().request(*_args, **_kwargs)

    session = SlowSession()
    attempts: list[join_cli.PairTarget] = []

    async def prepare(
        target: join_cli.PairTarget,
        _pin: str,
    ) -> SlowSession:
        attempts.append(target)
        return session

    monkeypatch.setattr(join_cli, "_open_ready_pairing_session", prepare)
    monkeypatch.setattr(join_cli, "_HTTP_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            join_cli.DirectPairRequest(
                candidates=(
                    join_cli.DirectPairCandidate(
                        ipaddress.IPv4Address("10.0.0.1"),
                        7657,
                    ),
                    join_cli.DirectPairCandidate(
                        ipaddress.IPv4Address("10.0.0.2"),
                        7657,
                    ),
                ),
                path="/app/network/pair?token=timeout",
                ca_fingerprint_pin="ab" * 16,
            ),
            body,
            private_key,
        )

    assert str(exc_info.value) == (
        "Timed out waiting for the pairing response from 10.0.0.1:7657."
    )
    assert [target.host for target in attempts] == ["10.0.0.1"]
    assert len(session.requests) == 1
    assert session.closed is True


@pytest.mark.parametrize("as_role", ["", "peer"])
def test_main_builds_one_material_set_across_candidate_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_role: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    nonce = "10000000000000000000000000000031"
    code = _build_pair_link_v05(
        ["10.0.0.1", "10.0.0.2"],
        7657,
        nonce,
        "ab" * 32,
    )
    build_calls: list[str] = []
    publish_calls: list[Path] = []
    sessions = [_FakeSession()]
    attempts: list[join_cli.PairTarget] = []
    real_build_csr = join_cli._build_csr
    _disable_direct_cert_validation(monkeypatch)

    def spy_build_csr(label: str) -> tuple[Any, bytes, str]:
        build_calls.append(label)
        return real_build_csr(label)

    async def prepare(
        target: join_cli.PairTarget,
        _pin: str,
    ) -> _FakeSession:
        attempts.append(target)
        if len(attempts) == 1:
            raise ValueError("first candidate not ready")
        return sessions[0]

    def record_publish(bundle_dir: Path, _files: dict[str, bytes]) -> None:
        publish_calls.append(bundle_dir)

    monkeypatch.setattr(join_cli, "_build_csr", spy_build_csr)
    monkeypatch.setattr(join_cli, "_open_ready_pairing_session", prepare)
    monkeypatch.setattr(join_cli, "_ca_fingerprint", lambda _chain: "sha256:fake")
    monkeypatch.setattr(join_cli, "_publish_bundle_atomic", record_publish)

    result = join_cli.main(
        _args(
            code=code,
            as_role=as_role or None,
            label="material-peer" if as_role == "peer" else "material-observer",
        )
    )

    assert result == 0
    assert build_calls == [
        "material-peer" if as_role == "peer" else "material-observer"
    ]
    assert [target.host for target in attempts] == ["10.0.0.1", "10.0.0.2"]
    assert len(sessions[0].requests) == 1
    assert len(publish_calls) == 1


def _assert_no_pair_secrets(
    message: str,
    *,
    nonce: str,
    pair_link: str,
    ca_fp: str,
    csr: str = "CSR-SECRET",
) -> None:
    fragment = pair_link.split("#", 1)[1]
    request_url = f"/app/network/pair?token={nonce}"
    assert nonce not in message
    assert pair_link not in message
    assert fragment not in message
    assert request_url not in message
    assert ca_fp not in message
    assert csr not in message
    _single_line(message)


def test_wrong_pin_diagnostic_omits_pair_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    nonce = "10000000000000000000000000000032"
    ca_fp = "00" * 32
    with pairing_harness(tmp_path, monkeypatch) as harness:
        pair_link = harness.pair_link(nonce, ca_fp=ca_fp)
        harness.seed_nonce(nonce, "laptop")
        result = join_cli.main(_args(code=pair_link))

    err = capsys.readouterr().err.strip()
    assert result == 1
    _assert_no_pair_secrets(err, nonce=nonce, pair_link=pair_link, ca_fp=ca_fp)


def test_malformed_response_diagnostic_omits_pair_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    async def handler(reader, writer) -> None:
        await reader.read()
        await writer.write(_http_response(b"{not-json"))
        await writer.close()

    nonce = "10000000000000000000000000000033"
    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        pair_link = harness.pair_link(nonce)
        result = join_cli.main(_args(code=pair_link))

    err = capsys.readouterr().err.strip()
    assert result == 1
    _assert_no_pair_secrets(
        err,
        nonce=nonce,
        pair_link=pair_link,
        ca_fp=harness.ca.fingerprint_sha256(),
    )


def test_malformed_home_diagnostic_omits_constructed_request_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nonce = "10000000000000000000000000000034"
    ca_fp = "ab" * 32
    pair_link = _build_pair_link("10.0.0.42", 7657, nonce, ca_fp)
    csr = "CSR-SECRET"
    monkeypatch.setattr(
        join_cli,
        "_build_csr",
        lambda _label: (object(), b"PRIVATE-SECRET", csr),
    )

    result = join_cli.main(_args(code=pair_link, home="https://receiver.example"))

    err = capsys.readouterr().err.strip()
    assert result == 1
    assert err == "Pair-link target missing explicit port."
    _assert_no_pair_secrets(
        err,
        nonce=nonce,
        pair_link=pair_link,
        ca_fp=ca_fp,
        csr=csr,
    )


def test_parse_pair_link_extracts_embedded_ca_pin() -> None:
    # The pair-link's last 16 bytes (the CA-fp prefix) must be parsed onto the
    # PairRequest, not discarded. This is the wiring the CSO review flagged.
    ca_fp = "ab" * 32  # 64 hex chars; only the first 16 bytes are embedded
    link = _build_pair_link("127.0.0.1", 7657, "f" * 32, ca_fp)

    request = join_cli._parse_pair_link(link, None)

    assert request.ca_fingerprint_pin == "ab" * 16


def test_lan_pair_link_hard_fails_on_ca_pin_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An attacker-substituted home (wrong CA) must fail the join, not warn.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    nonce = "1000000000000000000000000000000b"
    wrong_ca_fp = "00" * 32
    with pairing_harness(tmp_path, monkeypatch) as harness:
        harness.seed_nonce(nonce, "laptop")
        result = join_cli.main(_args(code=harness.pair_link(nonce, ca_fp=wrong_ca_fp)))

    err = capsys.readouterr().err.strip()
    assert result == 1
    assert err == "Pairing TLS peer did not match the pair-link."
    _single_line(err)
    # The credential bundle must not be written on a failed pin check.
    assert not (tmp_path / "config" / "solstone-observer" / "spl" / "laptop").exists()


def test_lan_pair_link_succeeds_with_matching_embedded_ca_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The happy path now exercises a real embedded pin (harness default = real
    # CA fp) plus the defense-in-depth live-peer binding against that CA.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    nonce = "1000000000000000000000000000000c"
    with pairing_harness(tmp_path, monkeypatch) as harness:
        harness.seed_nonce(nonce, "laptop")
        result = join_cli.main(_args(code=harness.pair_link(nonce)))

    assert result == 0
    assert (tmp_path / "config" / "solstone-observer" / "spl" / "laptop").is_dir()


def test_verify_leaf_signed_by_pinned_ca(tmp_path: Path) -> None:
    # Defense in depth: the live peer leaf must verify against the pinned CA.
    ca_a = load_or_generate_ca(tmp_path / "ca_a")
    ca_b = load_or_generate_ca(tmp_path / "ca_b")
    leaf_a, _key = issue_server_cert(ca_a)

    # Signed by the matching CA: no raise.
    join_cli._verify_leaf_signed_by_pinned_ca(leaf_a, ca_a.cert)

    # Signed by a different CA than the one pinned: fail closed.
    with pytest.raises(ValueError) as exc_info:
        join_cli._verify_leaf_signed_by_pinned_ca(leaf_a, ca_b.cert)
    assert "not signed by the pinned CA" in str(exc_info.value)


def test_ca_pin_matches_prefix_and_full_and_failclosed() -> None:
    full = "sha256:" + ("ab" * 32)
    # Full-length pin compares the whole digest (back-compat with the old API).
    assert ca_pin_matches(full, "ab" * 32)
    assert ca_pin_matches(full, "sha256:" + ("ab" * 32))
    # 16-byte (32-hex) prefix pin — the LAN pair-link form.
    assert ca_pin_matches(full, "ab" * 16)
    # Case-insensitive, prefix on either side.
    assert ca_pin_matches("AB" * 32, "sha256:" + ("ab" * 16))
    # Mismatch.
    assert not ca_pin_matches(full, "cd" * 16)
    # Fail closed: empty, odd-length, and over-long pins.
    assert not ca_pin_matches(full, "")
    assert not ca_pin_matches(full, "abc")
    assert not ca_pin_matches("ab", "abcd")

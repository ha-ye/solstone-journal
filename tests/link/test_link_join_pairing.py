# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import inspect
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.apps.link.routes import _build_pair_link
from solstone.convey.secure_listener.tls import issue_server_cert
from solstone.think.link import client as link_client
from solstone.think.link import join_cli
from solstone.think.link.ca import ca_pin_matches, load_or_generate_ca
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


def _csr_body(label: str = "laptop") -> dict[str, str]:
    _private_key_pem, csr_pem = join_cli._build_csr(label)
    return {"csr": csr_pem, "device_label": label}


def _pair_payload(
    harness: PairingHarness,
    *,
    instance_id: str = "inst-1",
) -> dict[str, Any]:
    ca_pem = harness.ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return {
        "client_cert": "-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n",
        "ca_chain": [ca_pem],
        "instance_id": instance_id,
        "home_label": "solstone",
        "home_attestation": "header.payload.signature",
        "fingerprint": "sha256:client",
        "local_endpoints": [{"host": "127.0.0.1", "port": 7657}],
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
    def write(self, _data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeSession:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.closed = False

    async def request(
        self, *_args: object, **_kwargs: object
    ) -> tuple[int, dict, bytes]:
        raise self._exc

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
    # Criterion 5: CA pin mismatch is checked after handshake and pair exchange.
    body = _csr_body("pin-phone")
    with pairing_harness(tmp_path, monkeypatch) as harness:
        harness.seed_nonce("10000000000000000000000000000002", "pin-phone")
        response = join_cli._post_pair_framed(
            harness.pair_url("10000000000000000000000000000002"),
            body,
            ca_fingerprint_pin=None,
        )
        correct = join_cli._ca_fingerprint(join_cli._join_chain(response.ca_chain))

        harness.seed_nonce("10000000000000000000000000000003", "pin-phone")
        with pytest.raises(ValueError) as exc_info:
            join_cli._post_pair_framed(
                harness.pair_url("10000000000000000000000000000003"),
                body,
                ca_fingerprint_pin="sha256:" + ("0" * 64),
            )

        harness.seed_nonce("10000000000000000000000000000004", "pin-phone")
        pinned = join_cli._post_pair_framed(
            harness.pair_url("10000000000000000000000000000004"),
            body,
            ca_fingerprint_pin=correct,
        )

    assert "CA fingerprint mismatch" in str(exc_info.value)
    assert pinned.instance_id


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
        await writer.write(_json_response(_pair_payload(state["harness"])))
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 8: sync wrapper returns a PairResponse after closing the transport.
    body = _csr_body("sync-phone")
    with pairing_harness(tmp_path, monkeypatch) as harness:
        harness.seed_nonce("10000000000000000000000000000006", "sync-phone")
        first = join_cli._post_pair_framed(
            harness.pair_url("10000000000000000000000000000006"),
            body,
        )
        assert harness.wait_until_idle()

        harness.seed_nonce("10000000000000000000000000000007", "sync-phone")
        second = join_cli._post_pair_framed(
            harness.pair_url("10000000000000000000000000000007"),
            body,
        )
        assert harness.wait_until_idle()

    assert isinstance(first, join_cli.PairResponse)
    assert isinstance(second, join_cli.PairResponse)
    assert not inspect.isawaitable(first)


def test_post_pair_framed_requires_explicit_port() -> None:
    # Criterion 9: pair-link framed targets must include an explicit port.
    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            "https://receiver/app/link/pair?token=x",
            _csr_body(),
        )

    assert "missing explicit port" in str(exc_info.value)
    _single_line(str(exc_info.value))


def test_post_pair_framed_reassembles_multiframe_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 10: multiple DATA frames are reassembled before response parsing.
    state: dict[str, PairingHarness] = {}

    async def handler(reader, writer) -> None:
        await reader.read()
        response = _json_response(_pair_payload(state["harness"]))
        await writer.write(response[:25])
        await writer.write(response[25:80])
        await writer.write(response[80:])
        await writer.close()

    with pairing_harness(tmp_path, monkeypatch, handle_stream=handler) as harness:
        state["harness"] = harness
        response = join_cli._post_pair_framed(
            harness.pair_url("10000000000000000000000000000008"),
            _csr_body("multi-phone"),
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
        await writer.reset()

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

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            f"https://127.0.0.1:{port}/app/link/pair?token=x",
            _csr_body(),
        )

    message = str(exc_info.value)
    assert message.startswith(f"Could not connect to 127.0.0.1:{port}:")
    _single_line(message)


def test_framed_tls_failure_is_single_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 13: TLS handshake failures get their own single-line message.
    async def fake_open_connection(_host: str, _port: int):
        return link_client.asyncio.StreamReader(), _FakeWriter()

    async def fail_tls(_transport):
        raise TlsError("bad test handshake")

    monkeypatch.setattr(join_cli.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(join_cli, "_open_pairing_session", fail_tls)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            "https://127.0.0.1:1/app/link/pair?token=x",
            _csr_body(),
        )

    message = str(exc_info.value)
    assert message == "TLS handshake with 127.0.0.1:1 failed: bad test handshake"
    _single_line(message)


def test_framed_handshake_then_drop_is_single_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 13: post-handshake drops share the reset/closed taxonomy.
    async def fake_open_connection(_host: str, _port: int):
        return link_client.asyncio.StreamReader(), _FakeWriter()

    async def fake_open_pairing_session(_transport):
        return _FakeSession(StreamResetError("closed after handshake"))

    monkeypatch.setattr(join_cli.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(join_cli, "_open_pairing_session", fake_open_pairing_session)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            "https://127.0.0.1:1/app/link/pair?token=x",
            _csr_body(),
        )

    message = str(exc_info.value)
    assert message == "Pairing stream reset or closed before a response was received."
    _single_line(message)


def test_framed_connect_timeout_is_single_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 13: connection timeout maps to a timeout message without waiting 15s.
    async def hang_open_connection(_host: str, _port: int):
        await link_client.asyncio.sleep(3600)

    monkeypatch.setattr(join_cli.asyncio, "open_connection", hang_open_connection)
    monkeypatch.setattr(join_cli, "_CONNECT_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(ValueError) as exc_info:
        join_cli._post_pair_framed(
            "https://127.0.0.1:1/app/link/pair?token=x",
            _csr_body(),
        )

    message = str(exc_info.value)
    assert message == "Timed out connecting to 127.0.0.1:1."
    _single_line(message)


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
    assert "CA fingerprint mismatch" in err
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

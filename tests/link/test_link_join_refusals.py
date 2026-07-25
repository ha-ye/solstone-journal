# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import ipaddress

import pytest

from solstone.apps.network.copy import PAIR_LINK_HOST, PAIR_LINK_PATH
from solstone.apps.network.crockford32 import encode as crockford_encode
from solstone.apps.network.routes import _build_pair_link, _build_pair_link_v05
from solstone.think.link import join_cli


def _args(code: str, *, home: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(home=home, code=code, as_role=None, label="laptop")


def _link_from_blob(blob: bytes) -> str:
    return f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#{crockford_encode(blob)}"


def _v04_blob(
    *,
    addr_type: int = 0x01,
    host: str = "10.0.0.42",
    port: int = 7657,
    nonce: str = "a1b2c3d4e5f607181122334455667788",
    ca_fp: str = "deadbeefcafebabe0123456789abcdef",
) -> bytes:
    return (
        b"\x04"
        + bytes([addr_type])
        + ipaddress.IPv4Address(host).packed
        + port.to_bytes(2, "big")
        + bytes.fromhex(nonce)
        + bytes.fromhex(ca_fp)[:16]
    )


def _v05_blob(
    candidates: list[str],
    *,
    addr_type: int = 0x01,
    count: int | None = None,
    port: int = 7657,
    nonce: str = "a1b2c3d4e5f607181122334455667788",
    ca_fp: str = "deadbeefcafebabe0123456789abcdef",
) -> bytes:
    encoded_count = len(candidates) if count is None else count
    return (
        b"\x05"
        + bytes([addr_type, encoded_count])
        + port.to_bytes(2, "big")
        + b"".join(ipaddress.IPv4Address(candidate).packed for candidate in candidates)
        + bytes.fromhex(nonce)
        + bytes.fromhex(ca_fp)[:16]
    )


def test_direct_v04_parse_returns_one_candidate_collection() -> None:
    nonce = "a1b2c3d4e5f607181122334455667788"
    pair_link = _build_pair_link(
        "10.0.0.42",
        7657,
        nonce,
        "deadbeefcafebabe0123456789abcdef",
    )

    request = join_cli._parse_pair_link(pair_link, None)

    assert isinstance(request, join_cli.DirectPairRequest)
    assert request.candidates == (
        join_cli.DirectPairCandidate(ipaddress.IPv4Address("10.0.0.42"), 7657),
    )
    assert request.path == f"/app/network/pair?token={nonce}"
    assert request.ca_fingerprint_pin == "deadbeefcafebabe0123456789abcdef"


def test_direct_v05_parse_returns_ordered_shared_semantics() -> None:
    nonce = "b1b2c3d4e5f607181122334455667788"
    pair_link = _build_pair_link_v05(
        ["10.0.0.42", "100.64.0.1"],
        7657,
        nonce,
        "cafebabedeadbeef0123456789abcdef",
    )

    request = join_cli._parse_pair_link(pair_link, None)

    assert isinstance(request, join_cli.DirectPairRequest)
    assert [candidate.host for candidate in request.candidates] == [
        "10.0.0.42",
        "100.64.0.1",
    ]
    assert {candidate.port for candidate in request.candidates} == {7657}
    assert request.path == f"/app/network/pair?token={nonce}"
    assert request.ca_fingerprint_pin == "cafebabedeadbeef0123456789abcdef"


@pytest.mark.parametrize(
    "blob",
    [
        _v04_blob(addr_type=0x02),
        _v04_blob()[:-1],
        _v04_blob() + b"\x00",
        _v05_blob(["10.0.0.1"], addr_type=0x02),
        _v05_blob([], count=0),
        _v05_blob(
            ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"],
            count=5,
        ),
        _v05_blob(["10.0.0.1"])[:-1],
        _v05_blob(["10.0.0.1"]) + b"\x00",
    ],
)
def test_direct_pair_link_structural_failures_refuse(blob: bytes) -> None:
    with pytest.raises(ValueError, match="Malformed pair-link"):
        join_cli._parse_pair_link(_link_from_blob(blob), None)


def test_v05_rfc1918_plus_cgnat_admitted_as_whole() -> None:
    pair_link = _build_pair_link_v05(
        ["192.168.1.2", "100.127.255.255"],
        7657,
        "a1b2c3d4e5f607181122334455667788",
        "deadbeefcafebabe0123456789abcdef",
    )

    request = join_cli._parse_pair_link(pair_link, None)

    assert isinstance(request, join_cli.DirectPairRequest)
    assert [candidate.host for candidate in request.candidates] == [
        "192.168.1.2",
        "100.127.255.255",
    ]


def test_v05_rfc1918_plus_test_net_refuses_as_whole() -> None:
    pair_link = _build_pair_link_v05(
        ["192.168.1.2", "203.0.113.9"],
        7657,
        "a1b2c3d4e5f607181122334455667788",
        "deadbeefcafebabe0123456789abcdef",
    )

    with pytest.raises(ValueError, match="outside the local network"):
        join_cli._parse_pair_link(pair_link, None)


def test_canonical_test_net_decodes_then_refuses_before_materials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nonce = "a1b2c3d4e5f607181122334455667788"
    pair_link = _build_pair_link(
        "192.0.2.42",
        7657,
        nonce,
        "deadbeefcafebabe0123456789abcdef",
    )
    material_calls: list[object] = []

    def fail_build_csr(*args: object, **kwargs: object) -> None:
        material_calls.append((args, kwargs))
        raise AssertionError("key generation should not run")

    monkeypatch.setattr(join_cli, "_build_csr", fail_build_csr)

    result = join_cli.main(_args(pair_link))

    err = capsys.readouterr().err.strip()
    assert result == 1
    assert "outside the local network" in err
    assert material_calls == []


def test_disallowed_embedded_set_cannot_be_rescued_by_home(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pair_link = _build_pair_link_v05(
        ["10.0.0.42", "203.0.113.9"],
        7657,
        "a1b2c3d4e5f607181122334455667788",
        "deadbeefcafebabe0123456789abcdef",
    )
    material_calls: list[object] = []
    monkeypatch.setattr(join_cli, "_build_csr", lambda *_args: material_calls.append(1))

    result = join_cli.main(_args(pair_link, home="https://receiver.example:7657"))

    assert result == 1
    assert "outside the local network" in capsys.readouterr().err
    assert material_calls == []


def test_admitted_embedded_set_with_home_dials_only_override() -> None:
    pair_link = _build_pair_link_v05(
        ["10.0.0.42", "100.64.0.1"],
        7657,
        "a1b2c3d4e5f607181122334455667788",
        "deadbeefcafebabe0123456789abcdef",
    )

    request = join_cli._parse_pair_link(pair_link, "https://receiver.example:9443")

    assert isinstance(request, join_cli.DirectPairRequest)
    assert join_cli._dial_targets(request) == (
        join_cli.PairTarget(
            host="receiver.example",
            port=9443,
            path="/app/network/pair?token=a1b2c3d4e5f607181122334455667788",
        ),
    )


def test_pair_code_error_names_pair_link_form() -> None:
    with pytest.raises(ValueError) as exc_info:
        join_cli._parse_pair_request("not-a-code", None)

    message = str(exc_info.value)
    assert "pair-link" in message


def test_malformed_pair_link_error_is_distinct() -> None:
    with pytest.raises(ValueError) as exc_info:
        join_cli._parse_pair_request(
            f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#!",
            None,
        )

    assert "Malformed pair-link" in str(exc_info.value)

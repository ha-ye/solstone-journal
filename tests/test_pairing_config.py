# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.think.pairing import config


def _write_config(journal: Path, payload: dict) -> None:
    config_path = journal / "config" / "journal.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _read_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text(encoding="utf-8"))


def test_pairing_config_defaults(journal_copy):
    payload = _read_config(journal_copy)
    payload.pop("pairing", None)
    payload["identity"] = {"name": "", "preferred": ""}
    _write_config(journal_copy, payload)

    assert config.get_home_address() is None


def test_pairing_home_address_reads_trimmed_value(journal_copy):
    payload = _read_config(journal_copy)
    payload["pairing"] = {
        "home_address": " 192.168.1.44:7657 ",
    }
    _write_config(journal_copy, payload)

    assert config.get_home_address() == "192.168.1.44:7657"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.44:7657", "192.168.1.44:7657"),
        (" 192.168.1.44:7657 ", "192.168.1.44:7657"),
    ],
)
def test_validate_home_address_accepts_ipv4_secure_port(
    raw: str,
    expected: str,
) -> None:
    assert config.validate_home_address(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "http://192.168.1.44:7657",
        "192.168.1.44",
        "http://192.168.1.44",
        "192.168.1.44:0",
        "192.168.1.44:5015",
        "192.168.1.44:65536",
        "192.168.1.44:notaport",
        "https://192.168.1.44:7657",
        "user@192.168.1.44:7657",
        "192.168.1.44:7657/path",
        "192.168.1.44:7657?x=1",
        "192.168.1.44:7657#frag",
        "127.0.0.1:7657",
        "0.0.0.0:7657",
        "169.254.1.1:7657",
        "224.0.0.1:7657",
        "[::1]:7657",
        "[fe80::1]:7657",
    ],
)
def test_validate_home_address_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(config.InvalidHomeAddress) as excinfo:
        config.validate_home_address(raw)

    assert str(excinfo.value) == config.HOME_ADDRESS_INVALID


@pytest.mark.parametrize("raw", ["mylab.local:7657", "home.local"])
def test_validate_home_address_rejects_hostname_with_private_link_message(
    raw: str,
) -> None:
    with pytest.raises(config.InvalidHomeAddress) as excinfo:
        config.validate_home_address(raw)

    assert str(excinfo.value) == config.HOME_ADDRESS_HOSTNAME_UNSUPPORTED


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.168.1.44", True),
        ("10.0.0.5", True),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("169.254.1.1", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("not-an-ip", False),
        (None, False),
    ],
)
def test_is_usable_ipv4(value: object, expected: bool) -> None:
    assert config.is_usable_ipv4(value) is expected


def test_home_address_round_trip(journal_copy) -> None:
    canonical = config.validate_home_address("192.168.1.44:7657")

    config.set_home_address(canonical)

    assert _read_config(journal_copy)["pairing"]["home_address"] == canonical
    assert config.get_home_address() == canonical

    config.clear_home_address()

    assert _read_config(journal_copy)["pairing"]["home_address"] is None
    assert config.get_home_address() is None


def test_validate_home_address_rejects_without_writing(journal_copy) -> None:
    before = _read_config(journal_copy)

    with pytest.raises(config.InvalidHomeAddress):
        config.validate_home_address("192.168.1.44:5015")

    assert _read_config(journal_copy) == before

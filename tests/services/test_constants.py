# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import solstone.think.services.constants as constants
from solstone.think.services.constants import (
    DEVICE_CODE_PREFIX,
    DEVICE_CODE_REGEX,
    DEVICE_CODE_TTL_MS,
    NONCE_ALPHABET,
    NONCE_LENGTH_CHARS,
    NONCE_REGEX,
)
from solstone.think.services.portal_client import mint_nonce


def test_nonce_constants_match_worker_contract() -> None:
    assert NONCE_ALPHABET == "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    assert NONCE_LENGTH_CHARS == 52
    assert NONCE_REGEX.pattern == r"^[A-Z2-7]{52}$"


def test_minted_nonces_match_regex_and_are_high_cardinality() -> None:
    samples = [mint_nonce() for _ in range(1000)]

    assert all(NONCE_REGEX.fullmatch(sample) for sample in samples)
    assert all(set(sample) <= set(NONCE_ALPHABET) for sample in samples)
    assert len(set(samples)) >= 990


def test_device_code_constants_match_worker_contract() -> None:
    assert DEVICE_CODE_PREFIX == "SCOUT"
    assert DEVICE_CODE_REGEX.pattern == (
        r"^SCOUT-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}-"
        r"[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{4}$"
    )
    assert DEVICE_CODE_TTL_MS == 900_000
    assert any(name.startswith("DEVICE_CODE_") for name in dir(constants))


def test_device_code_regex_rejects_ambiguous_chars() -> None:
    for char in "ILO01":
        assert not DEVICE_CODE_REGEX.fullmatch(f"SCOUT-{char}345-6789")

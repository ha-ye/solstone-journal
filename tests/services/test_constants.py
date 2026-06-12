# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.services.constants import (
    NONCE_ALPHABET,
    NONCE_LENGTH_CHARS,
    NONCE_REGEX,
    SERVICE_SCOUT,
    SERVICE_SPL,
    SUPPORTED_SERVICES,
)
from solstone.think.services.portal_client import mint_nonce


def test_nonce_constants_match_worker_contract() -> None:
    assert NONCE_ALPHABET == "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    assert NONCE_LENGTH_CHARS == 52
    assert NONCE_REGEX.pattern == r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{52}$"


def test_minted_nonces_match_regex_and_are_high_cardinality() -> None:
    samples = [mint_nonce() for _ in range(1000)]

    assert all(NONCE_REGEX.fullmatch(sample) for sample in samples)
    assert all(set(sample) <= set(NONCE_ALPHABET) for sample in samples)
    assert len(set(samples)) >= 990


def test_supported_services_are_explicit_allow_list() -> None:
    assert SERVICE_SCOUT == "scout"
    assert SERVICE_SPL == "spl"
    assert SUPPORTED_SERVICES == frozenset({"scout", "spl"})

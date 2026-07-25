# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ipaddress

import pytest

from solstone.think.link.direct_admission import is_direct_pair_candidate_allowed


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("9.255.255.255", False),
        ("10.0.0.0", True),
        ("10.255.255.255", True),
        ("11.0.0.0", False),
        ("172.15.255.255", False),
        ("172.16.0.0", True),
        ("172.31.255.255", True),
        ("172.32.0.0", False),
        ("192.167.255.255", False),
        ("192.168.0.0", True),
        ("192.168.255.255", True),
        ("192.169.0.0", False),
        ("169.253.255.255", False),
        ("169.254.0.0", True),
        ("169.254.255.255", True),
        ("169.255.0.0", False),
        ("100.63.255.255", False),
        ("100.64.0.0", True),
        ("100.127.255.255", True),
        ("100.128.0.0", False),
        ("126.255.255.255", False),
        ("127.0.0.0", True),
        ("127.255.255.255", True),
        ("128.0.0.0", False),
        ("0.0.0.0", False),
        ("255.255.255.255", False),
        ("224.0.0.1", False),
        ("198.18.0.1", False),
        ("192.0.2.1", False),
        ("198.51.100.20", False),
        ("203.0.113.30", False),
        ("8.8.8.8", False),
        ("1.1.1.1", False),
    ],
)
def test_direct_pair_candidate_admission_boundaries(
    address: str,
    allowed: bool,
) -> None:
    assert is_direct_pair_candidate_allowed(ipaddress.IPv4Address(address)) is allowed

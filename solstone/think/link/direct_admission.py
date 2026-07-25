# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Direct pair-link consumer address admission.

This is deliberately not shared with ``pairing.config.is_usable_ipv4`` or
``interface_watcher._classify``. Those modules decide what the home should
advertise from configured or observed interface state; this module decides what
the joining process will dial from an operator-pasted direct pair-link. The
authoritative allow-list is ``DIRECT_PAIR_ALLOWED_NETWORKS``.
"""

from __future__ import annotations

import ipaddress

DIRECT_PAIR_ALLOWED_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv4Network("127.0.0.0/8"),
)


def is_direct_pair_candidate_allowed(ipv4: ipaddress.IPv4Address) -> bool:
    """Return whether a decoded direct pair-link IPv4 candidate may be dialed."""

    return any(ipv4 in network for network in DIRECT_PAIR_ALLOWED_NETWORKS)


__all__ = ["DIRECT_PAIR_ALLOWED_NETWORKS", "is_direct_pair_candidate_allowed"]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.apps.network.home_candidates import resolve_pair_link_candidates
from solstone.think.link.local_endpoints import LocalEndpoint


def _endpoint(ip: str, scope: str = "lan") -> LocalEndpoint:
    return LocalEndpoint(ip=ip, port=7657, scope=scope)


def test_resolver_orders_non_vpn_before_vpn() -> None:
    assert resolve_pair_link_candidates(
        [
            _endpoint("100.64.0.5", "vpn"),
            _endpoint("192.168.1.10"),
            _endpoint("192.168.1.11"),
        ],
        None,
    ) == ["192.168.1.10", "192.168.1.11", "100.64.0.5"]


def test_resolver_promotes_route_only_within_existing_bucket() -> None:
    assert resolve_pair_link_candidates(
        [
            _endpoint("192.168.1.10"),
            _endpoint("100.64.0.5", "vpn"),
        ],
        "100.64.0.5",
    ) == ["192.168.1.10", "100.64.0.5"]


def test_resolver_does_not_inject_distinct_route_into_non_empty_watcher() -> None:
    assert resolve_pair_link_candidates(
        [
            _endpoint("192.168.1.10"),
            _endpoint("192.168.1.11"),
        ],
        "192.168.1.99",
    ) == ["192.168.1.10", "192.168.1.11"]


def test_resolver_uses_route_fallback_when_watcher_empty() -> None:
    assert resolve_pair_link_candidates([], "192.168.1.50") == ["192.168.1.50"]


def test_resolver_ignores_unusable_route() -> None:
    assert resolve_pair_link_candidates([], "127.0.0.1") == []


def test_resolver_dedupes_excludes_ipv6_and_caps() -> None:
    assert resolve_pair_link_candidates(
        [
            _endpoint("192.168.1.10"),
            _endpoint("fd00::1", "ula"),
            _endpoint("192.168.1.11"),
            _endpoint("192.168.1.10"),
            _endpoint("192.168.1.12"),
            _endpoint("192.168.1.13"),
            _endpoint("192.168.1.14"),
        ],
        "192.168.1.14",
    ) == ["192.168.1.14", "192.168.1.10", "192.168.1.11", "192.168.1.12"]

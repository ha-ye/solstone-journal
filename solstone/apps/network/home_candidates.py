# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure home-address candidate resolution for network pairing."""

from __future__ import annotations

from solstone.think.link.local_endpoints import LocalEndpoint
from solstone.think.pairing.config import is_usable_ipv4

VPN_SCOPES = {"vpn"}


def resolve_pair_link_candidates(
    endpoints: list[LocalEndpoint],
    route_ipv4: str | None,
) -> list[str]:
    """Return up to four ordered usable IPv4 candidates for direct pairing."""

    usable_route = (
        route_ipv4 if route_ipv4 is not None and is_usable_ipv4(route_ipv4) else None
    )
    filtered = [endpoint for endpoint in endpoints if is_usable_ipv4(endpoint.ip)]
    if not filtered:
        return [usable_route] if usable_route is not None else []

    non_vpn: list[str] = []
    vpn: list[str] = []
    for endpoint in filtered:
        (vpn if endpoint.scope in VPN_SCOPES else non_vpn).append(endpoint.ip)

    if usable_route is not None:
        for group in (non_vpn, vpn):
            if usable_route in group:
                group.remove(usable_route)
                group.insert(0, usable_route)
                break

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in (*non_vpn, *vpn):
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped[:4]


__all__ = ["VPN_SCOPES", "resolve_pair_link_candidates"]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""LAN interface endpoint watcher for link pairing surfaces."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Iterable

from .local_endpoints import LocalEndpoint

log = logging.getLogger("link.interface_watcher")

LINK_DIRECT_PORT = 7657
# Interfaces we never advertise, no matter the address (bridged/virtual/loopback).
# `tap*` is here: bridged Ethernet over VPN carrying RFC1918, not
# peer-reachable overlay.
_HARD_EXCLUDED_INTERFACE_PREFIXES = ("lo", "docker", "br-", "vbox", "vmnet", "tap")
# Overlay tunnels that may carry peer-reachable overlay addresses (macOS utun,
# generic tun, Linux tailscale0 and Tailscale-branded adapters). Admitted only
# when the address itself is CGNAT or ULA - name AND range, never name alone.
_OVERLAY_INTERFACE_PREFIXES = ("utun", "tun", "tailscale")
_LAN_V4_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_CGNAT_V4_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")  # RFC 6598
_ULA_V6_NETWORK = ipaddress.IPv6Network("fc00::/7")


class InterfaceWatcher:
    def __init__(self, *, poll_interval: float = 1.5) -> None:
        self._poll_interval = poll_interval
        self._endpoints: tuple[LocalEndpoint, ...] = ()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start watching interfaces.

        Linux netlink can replace this later; polling satisfies the current
        1-2s freshness requirement without brittle binary parsing.
        """
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._poll_loop(),
            name="link-interface-watcher",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._endpoints = ()

    def snapshot(self) -> list[LocalEndpoint]:
        return list(self._endpoints)

    def _update_snapshot(self, raw: Iterable[tuple[str, str]]) -> None:
        endpoints: list[LocalEndpoint] = []
        for ifname, ip_str in raw:
            normalized = _normalize_ip(ip_str)
            scope = _classify(ifname, normalized)
            if scope is None:
                continue
            endpoints.append(
                LocalEndpoint(ip=normalized, port=LINK_DIRECT_PORT, scope=scope)
            )
        next_snapshot = tuple(sorted(endpoints, key=lambda ep: (ep.scope, ep.ip)))
        if next_snapshot == self._endpoints:
            return
        self._endpoints = next_snapshot
        scope_counts: dict[str, int] = {}
        for ep in next_snapshot:
            scope_counts[ep.scope] = scope_counts.get(ep.scope, 0) + 1
        log.info(
            "local endpoints changed count=%d scopes=%s",
            len(next_snapshot),
            scope_counts,
        )

    async def _poll_loop(self) -> None:
        import psutil

        while True:
            try:
                raw: set[tuple[str, str]] = set()
                for ifname, addrs in psutil.net_if_addrs().items():
                    if _is_hard_excluded(ifname):
                        continue
                    for addr in addrs:
                        if addr.family not in (socket.AF_INET, socket.AF_INET6):
                            continue
                        raw.add((ifname, _normalize_ip(addr.address)))
                self._update_snapshot(raw)
            except Exception:
                log.exception("interface polling failed")
            await asyncio.sleep(self._poll_interval)


def _is_hard_excluded(name: str) -> bool:
    return name.startswith(_HARD_EXCLUDED_INTERFACE_PREFIXES)


def _is_overlay_interface(name: str) -> bool:
    return name.startswith(_OVERLAY_INTERFACE_PREFIXES)


def _normalize_ip(ip_str: str) -> str:
    return ip_str.split("%", 1)[0].split("/", 1)[0]


def _classify(ifname: str, ip_str: str) -> str | None:
    if _is_hard_excluded(ifname):
        return None
    try:
        ip_addr = ipaddress.ip_address(_normalize_ip(ip_str))
    except ValueError:
        return None
    if ip_addr.is_loopback or ip_addr.is_link_local or ip_addr.is_multicast:
        return None
    overlay = _is_overlay_interface(ifname)
    if isinstance(ip_addr, ipaddress.IPv4Address):
        if any(ip_addr in network for network in _LAN_V4_NETWORKS):
            # RFC1918 on an overlay interface is the tunnel's own private space,
            # not peer-reachable overlay; drop. On an ordinary NIC it's the LAN.
            return None if overlay else "lan"
        if overlay and ip_addr in _CGNAT_V4_NETWORK:
            return "vpn"
        # CGNAT on an ordinary NIC is the host's ISP-CGNAT WAN address, not
        # peer-reachable - drop (falls through to None).
        return None
    # IPv6 - ULA is name-agnostic: preserves today's `en0 + fd00::1 -> ula`.
    if ip_addr in _ULA_V6_NETWORK:
        return "ula"
    return None


_WATCHER: InterfaceWatcher | None = None


def set_interface_watcher(watcher: InterfaceWatcher | None) -> None:
    global _WATCHER
    _WATCHER = watcher


def get_interface_watcher() -> InterfaceWatcher | None:
    return _WATCHER


__all__ = [
    "InterfaceWatcher",
    "LINK_DIRECT_PORT",
    "_classify",
    "_is_hard_excluded",
    "get_interface_watcher",
    "set_interface_watcher",
]

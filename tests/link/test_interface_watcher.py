# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.link.interface_watcher import (
    InterfaceWatcher,
    _classify,
    _is_hard_excluded,
)


def test_classify_lan_ipv4() -> None:
    assert _classify("en0", "10.0.0.1") == "lan"
    assert _classify("en0", "172.16.5.5") == "lan"
    assert _classify("en0", "172.31.255.254") == "lan"
    assert _classify("en0", "192.168.1.1") == "lan"


def test_classify_ula_ipv6() -> None:
    assert _classify("en0", "fc00::1") == "ula"
    assert _classify("en0", "fd00::1") == "ula"


def test_classify_excludes_non_lan_addresses() -> None:
    assert _classify("en0", "8.8.8.8") is None
    assert _classify("en0", "172.32.0.1") is None
    assert _classify("en0", "127.0.0.1") is None
    assert _classify("en0", "169.254.1.1") is None
    assert _classify("en0", "fe80::1") is None
    assert _classify("en0", "fe80::1%eth0") is None
    assert _classify("en0", "::1") is None
    assert _classify("en0", "ff02::1") is None
    assert _classify("en0", "2001:db8::1") is None


def test_is_hard_excluded_interface() -> None:
    for name in (
        "lo",
        "lo0",
        "docker0",
        "br-abc",
        "vboxnet0",
        "vmnet8",
        "tap0",
        "tap5",
    ):
        assert _is_hard_excluded(name)
    for name in (
        "eth0",
        "en0",
        "wlan0",
        "wlp3s0",
        "enp0s31f6",
        "tun0",
        "utun3",
        "tailscale0",
    ):
        assert not _is_hard_excluded(name)


def test_joint_classification_matrix() -> None:
    cases = [
        ("en0", "10.0.0.1", "lan"),
        ("eth0", "192.168.1.5", "lan"),
        ("wlan0", "172.16.5.5", "lan"),
        ("en0", "fd00::1", "ula"),
        ("utun7", "fd00::1", "ula"),
        ("tailscale0", "100.64.0.5", "vpn"),
        ("utun7", "100.64.0.5", "vpn"),
        ("tun0", "100.100.1.2", "vpn"),
        ("utun7", "fd7a:115c:a1e0::1", "ula"),
        ("eth0", "100.64.0.5", None),
        ("en0", "100.64.0.5", None),
        ("wlan0", "100.64.0.5", None),
        ("docker0", "100.64.0.5", None),
        ("br-abc", "100.64.0.5", None),
        ("vboxnet0", "100.64.0.5", None),
        ("vmnet1", "100.64.0.5", None),
        ("tap0", "100.64.0.5", None),
        ("utun7", "10.0.0.1", None),
        ("tun0", "192.168.1.5", None),
    ]
    for ifname, ip, expected in cases:
        assert _classify(ifname, ip) == expected


def test_update_snapshot_filters_and_sorts() -> None:
    watcher = InterfaceWatcher()

    watcher._update_snapshot(
        [
            ("eth0", "192.168.1.10"),
            ("docker0", "172.17.0.1"),
            ("en0", "fd00::1"),
            ("lo", "127.0.0.1"),
            ("wlan0", "10.0.0.2"),
            ("eth1", "8.8.8.8"),
            ("en1", "fe80::1%en1"),
        ]
    )

    endpoints = watcher.snapshot()
    assert [(ep.scope, ep.ip, ep.port) for ep in endpoints] == [
        ("lan", "10.0.0.2", 7657),
        ("lan", "192.168.1.10", 7657),
        ("ula", "fd00::1", 7657),
    ]
    assert endpoints == sorted(endpoints, key=lambda ep: (ep.scope, ep.ip))


def test_update_snapshot_classifies_overlay_jointly() -> None:
    watcher = InterfaceWatcher()

    watcher._update_snapshot(
        [
            ("tailscale0", "100.64.0.7"),
            ("utun5", "10.0.0.9"),
            ("en0", "192.168.1.10"),
            ("tap0", "100.64.0.9"),
        ]
    )

    assert [(ep.scope, ep.ip, ep.port) for ep in watcher.snapshot()] == [
        ("lan", "192.168.1.10", 7657),
        ("vpn", "100.64.0.7", 7657),
    ]

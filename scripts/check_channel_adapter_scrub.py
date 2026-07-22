#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Reject release channel adapter host/reach leaks in tracked files."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parts(*pieces: str) -> str:
    return "".join(pieces)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    tier: str
    component: str
    value: str
    detail: str

    def format(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.tier} {self.component}: "
            f"{self.value} ({self.detail})"
        )


@dataclass(frozen=True)
class ScanStats:
    skipped_nul_binary: int = 0
    skipped_decode: int = 0
    skipped_io: int = 0


@dataclass(frozen=True)
class ScanResult:
    findings: tuple[Finding, ...]
    stats: ScanStats


TIER1_VALUES = (_parts("pr", "o5", "e"),)

TIER3_TERMS = (
    "fedora",
    "dgx",
    "jer",
    "nvidia-smi",
    "solstone-macos",
    "sol-signing",
    "sol-pbc-notary",
)

OCTET_RE = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9][0-9]?|0)"
IPV4_RE = re.compile(
    rf"(?<![A-Za-z0-9_.]){OCTET_RE}(?:\.{OCTET_RE}){{3}}(?![A-Za-z0-9_.])"
)
USER_HOST_RE = re.compile(
    rf"(?<![A-Za-z0-9._%+-])"
    rf"[A-Za-z0-9._-]+@"
    rf"(?:{OCTET_RE}(?:\.{OCTET_RE}){{3}}|"
    rf"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.local)"
    rf"(?![A-Za-z0-9_.:-])"
)

SHELL_PORT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.-])(?:{_parts('s', 'sh')}|{_parts('s', 'cp')})"
    rf"(?![A-Za-z0-9_.-]).*(?:^|[\s\"'])-[pP][\s\"']+[0-9]+"
)
ARGV_PORT_RE = re.compile(
    rf"['\"](?:{_parts('s', 'sh')}|{_parts('s', 'cp')})['\"].*"
    rf"['\"]-[pP]['\"]\s*,\s*['\"][0-9]+['\"]"
)

SSH_ARGV_RE = re.compile(
    rf"['\"](?:{_parts('s', 'sh')}|{_parts('s', 'cp')})['\"].*__TERM__"
)
SSH_SHELL_RE = re.compile(
    rf"(?<![A-Za-z0-9_.-])(?:{_parts('s', 'sh')}|{_parts('s', 'cp')})"
    rf"(?![A-Za-z0-9_.-]).*__TERM__"
)
USER_AT_TERM_RE = re.compile(
    rf"(?:^|[^A-Za-z0-9._-])__TERM__@"
    rf"(?:{OCTET_RE}(?:\.{OCTET_RE}){{3}}|[A-Za-z0-9-]+(?:\.local)?)"
    rf"(?![A-Za-z0-9_.:-])"
)
TERM_AT_HOST_RE = re.compile(
    r"@(?:[A-Za-z0-9-]+\.)*__TERM__(?:\.local)?(?![A-Za-z0-9_.:-])"
)
CONFIG_KEY_RE = re.compile(
    r"(?i)\b[A-Za-z0-9_]*(?:host|hostname|remote|reach|target|ssh|scp|"
    r"tmux_window|unlock_workdir|remote_work_prefix|build_window|macos_dir|workdir)"
    r"[A-Za-z0-9_]*\s*[:=]\s*['\"][^'\"]*__TERM__"
)
REMOTE_CALL_RE = re.compile(r"\b(?:ssh_run|scp_to|scp_from)\b.*__TERM__")

TIER3_COMPONENTS = (
    ("ssh-argv", SSH_ARGV_RE),
    ("ssh-shell", SSH_SHELL_RE),
    ("user-host", USER_AT_TERM_RE),
    ("host-user", TERM_AT_HOST_RE),
    ("config-value", CONFIG_KEY_RE),
    ("remote-call", REMOTE_CALL_RE),
)


DOCUMENTED_IP_LITERAL_EXCLUSIONS = {
    # RFC 6890: unspecified address used only for local bind/listener examples.
    ipaddress.IPv4Address("0.0.0.0"),
    # RFC 6890: loopback addresses.
    ipaddress.IPv4Network("127.0.0.0/8"),
    # RFC 1918: private-use network fixtures.
    ipaddress.IPv4Network("10.0.0.0/8"),
    # RFC 1918: private-use network fixtures.
    ipaddress.IPv4Network("172.16.0.0/12"),
    # RFC 1918: private-use network fixtures.
    ipaddress.IPv4Network("192.168.0.0/16"),
    # RFC 6598: shared address space fixtures.
    ipaddress.IPv4Network("100.64.0.0/10"),
    # RFC 3927: link-local address fixtures.
    ipaddress.IPv4Network("169.254.0.0/16"),
    # RFC 5771: multicast negative fixtures.
    ipaddress.IPv4Network("224.0.0.0/4"),
    # RFC 919/RFC 922: limited broadcast.
    ipaddress.IPv4Address("255.255.255.255"),
    # RFC 5737: documentation range.
    ipaddress.IPv4Network("192.0.2.0/24"),
    # RFC 5737: documentation range.
    ipaddress.IPv4Network("198.51.100.0/24"),
    # RFC 5737: documentation range.
    ipaddress.IPv4Network("203.0.113.0/24"),
}

DOCUMENTED_IP_VALUE_EXCLUSIONS = {
    # Python package version in the checked-in lockfile.
    ipaddress.IPv4Address("1.2.0.1"),
    # HTTP forwarding fixture.
    ipaddress.IPv4Address("1.2.3.4"),
    # Minified SVG path coordinate text.
    ipaddress.IPv4Address("2.95.6.6"),
    # Minified SVG path coordinate text.
    ipaddress.IPv4Address("3.5.7.7"),
    # Minified SVG path coordinate text.
    ipaddress.IPv4Address("4.5.8.8"),
    # Python package version in the checked-in lockfile.
    ipaddress.IPv4Address("4.13.0.92"),
    # Public DNS address used in routing diagnostics.
    ipaddress.IPv4Address("8.8.8.8"),
    # Python package version in the checked-in lockfile.
    ipaddress.IPv4Address("9.21.1.3"),
    # Python package version in the checked-in lockfile.
    ipaddress.IPv4Address("11.4.1.4"),
    # Python package version in the checked-in lockfile.
    ipaddress.IPv4Address("12.9.2.10"),
    # Package version fixture for CUDA runtime repacking.
    ipaddress.IPv4Address("13.5.1.27"),
    # Public-side boundary fixture for private-network classification.
    ipaddress.IPv4Address("172.32.0.1"),
}


def _ip_is_excluded(value: str) -> bool:
    address = ipaddress.IPv4Address(value)
    for excluded in DOCUMENTED_IP_LITERAL_EXCLUSIONS:
        if isinstance(excluded, ipaddress.IPv4Network):
            if address in excluded:
                return True
        elif address == excluded:
            return True
    return address in DOCUMENTED_IP_VALUE_EXCLUSIONS


def _pattern_for_term(pattern: re.Pattern[str], term: str) -> re.Pattern[str]:
    return re.compile(
        pattern.pattern.replace("__TERM__", re.escape(term)), pattern.flags
    )


def scan_line(path: str, line_number: int, line: str) -> list[Finding]:
    findings: list[Finding] = []
    for value in TIER1_VALUES:
        if value in line:
            findings.append(
                Finding(path, line_number, "Tier-1", "literal", value, "banned value")
            )

    for match in IPV4_RE.finditer(line):
        value = match.group(0)
        if _ip_is_excluded(value):
            continue
        findings.append(
            Finding(
                path,
                line_number,
                "Tier-2",
                "ip-literal",
                value,
                "if this is a version string or coordinate rather than a host "
                "address, add it to the documented IP literal exclusion list "
                "with a one-line justification",
            )
        )

    for match in USER_HOST_RE.finditer(line):
        findings.append(
            Finding(
                path,
                line_number,
                "Tier-2",
                "user-host",
                match.group(0),
                "replace reachable user/host literals with operator config",
            )
        )

    for pattern in (SHELL_PORT_RE, ARGV_PORT_RE):
        if pattern.search(line):
            findings.append(
                Finding(
                    path,
                    line_number,
                    "Tier-2",
                    "ssh-scp-port",
                    "-p/-P",
                    "move SSH/SCP port values into operator config",
                )
            )
            break

    for term in TIER3_TERMS:
        if term not in line:
            continue
        for component, pattern in TIER3_COMPONENTS:
            if _pattern_for_term(pattern, term).search(line):
                findings.append(
                    Finding(
                        path,
                        line_number,
                        "Tier-3",
                        component,
                        term,
                        "move reach or host-specific construction into operator config",
                    )
                )
                break

    return findings


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        findings.extend(scan_line(path, line_number, line))
    return findings


def tracked_paths(root: Path = ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def scan_paths(root: Path, paths: Iterable[str]) -> ScanResult:
    findings: list[Finding] = []
    skipped_nul_binary = 0
    skipped_decode = 0
    skipped_io = 0
    for relative_path in paths:
        path = root / relative_path
        try:
            data = path.read_bytes()
        except OSError:
            skipped_io += 1
            continue
        if b"\0" in data:
            skipped_nul_binary += 1
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped_decode += 1
            continue
        findings.extend(scan_text(relative_path, text))
    return ScanResult(
        tuple(findings),
        ScanStats(
            skipped_nul_binary=skipped_nul_binary,
            skipped_decode=skipped_decode,
            skipped_io=skipped_io,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    _ = argv
    result = scan_paths(ROOT, tracked_paths(ROOT))
    if result.findings:
        sys.stderr.write("Channel adapter scrub found host/reach literals:\n")
        for finding in result.findings:
            sys.stderr.write(f"  {finding.format()}\n")
        stats = result.stats
        sys.stderr.write(
            "Skipped tracked files: "
            f"NUL-binary={stats.skipped_nul_binary}, "
            f"decode={stats.skipped_decode}, io={stats.skipped_io}\n"
        )
        return 1
    stats = result.stats
    print(
        "Channel adapter scrub passed "
        f"(skipped NUL-binary={stats.skipped_nul_binary}, "
        f"decode={stats.skipped_decode}, io={stats.skipped_io})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Closed inventory for the native sol Python compatibility dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

COMPAT_MODULE = "solstone.think.sol_compat_cli"
SENTINEL = "SOLSTONE_NATIVE_COMPAT_ACTIVE"
SENTINEL_ARMED = "armed"
SENTINEL_ACTIVE = "active"
ARGV0_MARKER_PREFIX = "__solstone_native_argv0="
RECURSION_ERROR = (
    "sol: compatibility dispatch recursion detected. "
    "Reinstall solstone and solstone-core."
)
UNSUPPORTED_ERROR = "Unsupported native sol command."
EXIT_USAGE = 64
EXIT_SOFTWARE = 70
PUBLIC_BINARIES = frozenset({"sol", "solstone"})

TOP_LEVEL_COMPAT_MODULES = {
    "notify": "solstone.think.notify_cli",
    "doctor": "solstone.think.doctor",
    "check": "solstone.think.check",
    "contract": "solstone.think.contract_cli",
    "skills": "solstone.think.skills_cli",
    "link": "solstone.think.link",
}

JOURNAL_CALL_PREFIX = ("call", "journal")
JOURNAL_CALL_MODULE = "solstone.think.tools.call"


@dataclass(frozen=True)
class CompatTarget:
    module: str
    kind: Literal["main", "typer-app"]
    argv: list[str]


def marker_for_public_argv0(public_argv0: str) -> str:
    return f"{ARGV0_MARKER_PREFIX}{public_argv0}"


def parse_marker(value: str) -> str | None:
    if not value.startswith(ARGV0_MARKER_PREFIX):
        return None
    public_argv0 = value[len(ARGV0_MARKER_PREFIX) :]
    if public_argv0 not in PUBLIC_BINARIES:
        return None
    return public_argv0


def resolve_target(public_argv0: str, args: list[str]) -> CompatTarget | None:
    if not args:
        return None
    command = args[0]
    if command in TOP_LEVEL_COMPAT_MODULES:
        return CompatTarget(
            module=TOP_LEVEL_COMPAT_MODULES[command],
            kind="main",
            argv=[f"{public_argv0} {command}", *args[1:]],
        )
    if tuple(args[:2]) == JOURNAL_CALL_PREFIX:
        return CompatTarget(
            module=JOURNAL_CALL_MODULE,
            kind="typer-app",
            argv=[f"{public_argv0} call journal", *args[2:]],
        )
    return None

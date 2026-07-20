#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Check the generated observer-client OpenAPI contract bundle."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from solstone.convey.contract.observer_bundle import (
    BUNDLE_REL_DIR,
    stale_bundle_paths,
)
from solstone.convey.contract.observer_bundle_compatibility import (
    check_bundle_compatibility,
)
from solstone.convey.contract.observer_bundle_verification import (
    check_consumer_audit_coverage,
    verify_committed_bundle,
)

ROOT = Path(__file__).resolve().parent.parent


def _staleness_failures() -> list[str]:
    stale = stale_bundle_paths(ROOT)
    if not stale:
        return []
    paths = ", ".join(str(path) for path in stale)
    return [
        "observer client contract bundle staleness failed: "
        f"{paths}. Recovery: run make openapi."
    ]


def _manifest_failures() -> list[str]:
    verify_committed_bundle(ROOT)
    return []


def _compatibility_failures() -> list[str]:
    return check_bundle_compatibility(ROOT)


def _coverage_failures() -> list[str]:
    return check_consumer_audit_coverage(ROOT)


def _run_gate(name: str, fn: Callable[[], list[str]]) -> bool:
    try:
        failures = fn()
    except Exception as exc:
        failures = [
            f"observer client contract {name} failed: {exc}. "
            "Recovery: run make openapi, repair the bundle, or apply the "
            "required SemVer bump."
        ]
    if not failures:
        print(f"observer-client-contract {name}: pass")
        return False
    for failure in failures:
        print(failure, file=sys.stderr)
    return True


def main() -> int:
    failed = False
    gates: tuple[tuple[str, Callable[[], list[str]]], ...] = (
        ("staleness", _staleness_failures),
        ("manifest", _manifest_failures),
        ("compatibility", _compatibility_failures),
        ("windows-linux-coverage", _coverage_failures),
    )
    for name, fn in gates:
        failed = _run_gate(name, fn) or failed
    if failed:
        print(
            f"observer-client-contract: failed for {BUNDLE_REL_DIR}.",
            file=sys.stderr,
        )
        return 1
    print(f"observer-client-contract: pass for {BUNDLE_REL_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

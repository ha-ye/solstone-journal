#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build generated fixtures consumed by the Rust core workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from solstone.convey.contract.assemble import CALLOSUM_REGISTRY
from solstone.think.cogitate_contract import (
    COGITATE_ACCESS_TIERS,
    COGITATE_READ_TOOL_NAMES,
    COGITATE_RUNTIME_PREAMBLE,
    FUTURE_ACCESS_TIERS,
    TALENT_FINALIZATION_MODES,
    capabilities_for_access_tier,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "core" / "fixtures"
CALLOSUM_ARTIFACT_PATH = FIXTURE_DIR / "callosum_registry.json"
COGITATE_ARTIFACT_PATH = FIXTURE_DIR / "cogitate_contract.json"


def build_callosum_registry_fixture() -> dict[str, Any]:
    return {
        "fixture": "solstone-callosum-registry",
        "fixture_version": 1,
        "generated_by": "make core-fixtures",
        "registry": {
            tract: list(CALLOSUM_REGISTRY[tract]) for tract in sorted(CALLOSUM_REGISTRY)
        },
    }


def build_cogitate_contract_fixture() -> dict[str, Any]:
    preamble_bytes = COGITATE_RUNTIME_PREAMBLE.encode("utf-8")
    return {
        "fixture": "solstone-cogitate-contract",
        "fixture_version": 1,
        "generated_by": "make core-fixtures",
        "access_tiers": list(COGITATE_ACCESS_TIERS),
        "capabilities": {
            tier: {
                "sol": capabilities_for_access_tier(tier).sol,
                "reads": capabilities_for_access_tier(tier).reads,
                "submit": capabilities_for_access_tier(tier).submit,
            }
            for tier in COGITATE_ACCESS_TIERS
        },
        "future_access_tiers": list(FUTURE_ACCESS_TIERS),
        "read_tools": list(COGITATE_READ_TOOL_NAMES),
        "finalization_modes": list(TALENT_FINALIZATION_MODES),
        "runtime_preamble": {
            "digest": hashlib.sha256(preamble_bytes).hexdigest(),
            "algorithm": "sha256",
            "encoding": "utf-8",
            "byte_length": len(preamble_bytes),
        },
    }


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def expected_outputs() -> dict[Path, str]:
    return {
        CALLOSUM_ARTIFACT_PATH: render_json(build_callosum_registry_fixture()),
        COGITATE_ARTIFACT_PATH: render_json(build_cogitate_contract_fixture()),
    }


def write_outputs() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in expected_outputs().items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


def check_outputs() -> int:
    stale: list[str] = []
    for path, expected in expected_outputs().items():
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        if current != expected:
            stale.append(str(path.relative_to(ROOT)))

    if stale:
        paths = ", ".join(stale)
        print(
            f"Core generated fixtures are stale: {paths}. Run: make core-fixtures",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check generated fixtures without writing files.",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check_outputs()
    write_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

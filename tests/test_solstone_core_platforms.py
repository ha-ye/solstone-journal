# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.markers import Marker

from solstone.think.probe import (
    SOLSTONE_CORE_COVERED_PLATFORMS,
    SOLSTONE_CORE_PLATFORM_MARKERS,
    is_solstone_core_covered_platform,
)

ROOT = Path(__file__).resolve().parents[1]


def _core_pin_markers() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    host = data["project"]["optional-dependencies"]["journal-host"]
    return [
        dep.split(";", 1)[1].strip()
        for dep in host
        if dep.startswith("solstone-core==")
    ]


def test_core_pin_markers_match_probe_covered_platforms() -> None:
    marker_texts = _core_pin_markers()
    assert sorted(marker_texts) == sorted(SOLSTONE_CORE_PLATFORM_MARKERS)
    markers = [Marker(text) for text in marker_texts]
    platform_tuples = [
        *SOLSTONE_CORE_COVERED_PLATFORMS,
        ("linux", "riscv64"),
        ("darwin", "x86_64"),
    ]

    for system, machine in platform_tuples:
        marker_matches = [
            marker.evaluate(
                {
                    "sys_platform": system,
                    "platform_machine": machine,
                }
            )
            for marker in markers
        ]
        assert sum(marker_matches) == int(
            is_solstone_core_covered_platform(system, machine)
        )

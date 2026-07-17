# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.markers import Marker

from solstone.think.probe import (
    SOLSTONE_CORE_COVERED_PLATFORMS,
    SOLSTONE_CORE_PLATFORM_MARKERS,
    is_solstone_core_covered_platform,
)

ROOT = Path(__file__).resolve().parents[1]
MARKER_PLATFORM_RE = re.compile(
    r"sys_platform == '(?P<system>[^']+)' and platform_machine == '(?P<machine>[^']+)'"
)


def _core_pin_markers() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    host = data["project"]["optional-dependencies"]["journal-host"]
    return [
        dep.split(";", 1)[1].strip()
        for dep in host
        if dep.startswith("solstone-core==")
    ]


def _marker_platform_tuple(marker_text: str) -> tuple[str, str]:
    match = MARKER_PLATFORM_RE.fullmatch(marker_text)
    assert match is not None
    return (match.group("system"), match.group("machine"))


def test_core_pin_markers_match_probe_covered_platforms() -> None:
    marker_texts = _core_pin_markers()
    assert sorted(marker_texts) == sorted(SOLSTONE_CORE_PLATFORM_MARKERS)
    markers = [Marker(text) for text in marker_texts]
    platform_tuples = sorted(
        {
            *SOLSTONE_CORE_COVERED_PLATFORMS,
            *(_marker_platform_tuple(text) for text in marker_texts),
            ("linux", "riscv64"),
            ("darwin", "x86_64"),
        }
    )

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

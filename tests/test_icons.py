# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from solstone.apps import AppRegistry
from solstone.convey.icons import (
    APP_LUCIDE_MAP,
    emoji_to_lucide,
    lucide_svg,
    lucide_svg_for_emoji,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_lucide_svg_hit_and_miss() -> None:
    assert "<svg" in (lucide_svg("house") or "")
    assert lucide_svg("not-a-lucide-icon") is None


def test_emoji_to_lucide_required_cases() -> None:
    assert emoji_to_lucide("📚") == "library"
    assert emoji_to_lucide("🤝") == "handshake"
    assert emoji_to_lucide("⚙️") == "settings"
    assert emoji_to_lucide("⚙") == "settings"
    assert emoji_to_lucide("⚙️") == emoji_to_lucide("⚙")
    assert emoji_to_lucide("🪮", default="fallback") == "fallback"
    assert emoji_to_lucide("") is None


def test_emoji_to_lucide_preserves_raw_zwj_key() -> None:
    assert emoji_to_lucide("⛓‍💥") == "bone-fracture"


def test_lucide_svg_for_emoji_hit_and_miss() -> None:
    assert "<svg" in (lucide_svg_for_emoji("📚") or "")
    assert lucide_svg_for_emoji("🪮") is None


def test_lucide_loading_is_package_relative(tmp_path: Path) -> None:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{pythonpath}" if pythonpath else str(REPO_ROOT)
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from solstone.convey.icons import lucide_svg; "
                "raise SystemExit(0 if lucide_svg('house') else 1)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
    )

    assert result.returncode == 0


def test_app_registry_is_covered_by_lucide_map() -> None:
    registry = AppRegistry()
    registry.discover()

    assert set(registry.apps) <= set(APP_LUCIDE_MAP)


def test_app_lucide_map_values_exist_in_vendored_lucide_data() -> None:
    lucide_path = REPO_ROOT / "solstone" / "convey" / "static" / "icons" / "lucide.json"
    lucide_data = json.loads(lucide_path.read_text())

    assert set(APP_LUCIDE_MAP.values()) <= set(lucide_data)

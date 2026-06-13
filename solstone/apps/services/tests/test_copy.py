# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for services app copy discipline."""

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.apps.services.copy import services_copy_payload, services_copy_values


def test_services_copy_verbatim_strings():
    payload = services_copy_payload()

    assert payload["heading"] == "your services"
    assert (
        payload["umbrella"]
        == "solstone runs on your machine. these services are optional — turn them on when they help, turn them off whenever you want. nothing here is required to use solstone."
    )
    assert payload["promise"] == "your journal is always private, only yours."
    rows = {row["id"]: row for row in payload["services"]}
    assert rows["scout"]["label"] == "solstone scout"
    assert rows["spl"]["label"] == "solstone private link"
    assert rows["spb"]["label"] == "solstone backup"
    assert rows["spn"]["label"] == "solstone private notifications"
    assert (
        rows["scout"]["description"]
        == "join solstone scout — we'll set you up with a Gemini key on your machine and bring you into the alpha cohort"
    )
    assert rows["scout"]["manage_affordance"] == "manage on the web →"
    assert (
        rows["spl"]["description"]
        == "reach your journal from your other devices, privately"
    )
    assert rows["spl"]["manage_affordance"] == "manage in link →"
    assert (
        rows["spb"]["description"]
        == "keep an encrypted copy of your journal somewhere safe — only you can read it"
    )
    assert rows["spb"]["manage_affordance"] == "manage in backup →"
    assert rows["spb"]["manage_href"] == "/app/backup"
    assert rows["spb"]["coming_soon"] is False
    assert (
        rows["spn"]["description"]
        == "let sol reach you on your other devices when there's something worth a look"
    )
    assert payload["coming_soon_label"] == "coming soon"


def test_no_literal_copy_in_templates_or_static():
    root = Path("solstone/apps/services")
    structural_values = {
        "scout",
        "spl",
        "spb",
        "spn",
        "enable",
        "disable",
        "refresh",
        "starting",
        "waiting",
        "enabled",
        "pending",
        "revoked",
        "error",
        "busy",
    }
    hits: list[tuple[Path, str]] = []
    for path in [root / "workspace.html", root / "static" / "services.js"]:
        text = path.read_text(encoding="utf-8")
        for value in services_copy_values():
            if not value or value in structural_values:
                continue
            literal_patterns = (
                re.compile(rf">\s*{re.escape(value)}\s*<"),
                re.compile(rf"(?<!=)['\"`]{re.escape(value)}['\"`]"),
            )
            if any(pattern.search(text) for pattern in literal_patterns):
                hits.append((path, value))

    assert hits == []


def test_services_copy_json_round_trips_from_rendered_page(services_env):
    env = services_env()

    response = env.client.get("/app/services/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(r"const SERVICES_COPY = (\{.*\});", html)
    assert match, "SERVICES_COPY assignment not found"
    assert json.loads(match.group(1)) == services_copy_payload()


def test_all_copy_constants_referenced_by_render_surface():
    html = Path("solstone/apps/services/workspace.html").read_text(encoding="utf-8")
    static = Path("solstone/apps/services/static/services.js").read_text(
        encoding="utf-8"
    )
    surface = html + "\n" + static

    missing = [
        key
        for key in (
            "heading",
            "umbrella",
            "coming_soon_label",
            "state_labels",
            "promise",
        )
        if key not in surface
    ]

    assert missing == []

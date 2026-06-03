# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for curation owner-facing copy discipline."""

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.apps.curation.copy import (
    curation_copy_payload,
    curation_copy_values,
)


def test_no_literal_copy_in_templates():
    """Templates reference CUR_COPY constants; prose values are never inlined."""

    root = Path("solstone/apps/curation")

    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for value in curation_copy_values():
            literal_patterns = (
                re.compile(rf">\s*{re.escape(value)}\s*<"),
                re.compile(rf"(?<!=)['\"`]{re.escape(value)}['\"`]"),
            )
            if any(pattern.search(text) for pattern in literal_patterns):
                hits.append((path, value))

    assert hits == []


def test_all_copy_constants_referenced_by_render_surface():
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("solstone/apps/curation").rglob("*.html")
    )

    missing = [name for name in curation_copy_payload() if name not in html]

    assert missing == []


def test_curation_index_injects_copy(curation_env):
    env = curation_env()

    resp = env.client.get("/app/curation/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    match = re.search(r"const CUR_COPY = (\{.*\});", html)
    assert match, "CUR_COPY assignment not found in rendered page"
    assert json.loads(match.group(1)) == curation_copy_payload()

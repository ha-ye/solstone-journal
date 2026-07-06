# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for entities owner-facing copy discipline."""

from __future__ import annotations

import re
from pathlib import Path

from solstone.apps.entities.copy import entities_copy_payload, entities_copy_values


def test_no_literal_copy_in_templates():
    """Templates reference ENT_COPY constants; prose values are never inlined."""

    root = Path("solstone/apps/entities")

    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for value in entities_copy_values():
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
        for path in Path("solstone/apps/entities").rglob("*.html")
    )

    missing = [name for name in entities_copy_payload() if name not in html]

    assert missing == []


def test_entities_index_injects_copy(client):
    resp = client.get("/app/entities/api/state")
    assert resp.status_code == 200
    assert resp.get_json() == {"entities_copy": entities_copy_payload()}


def test_entities_index_serves_spa_shell(client):
    resp = client.get("/app/entities/")

    assert resp.status_code == 200
    assert b'data-solstone-shell="spa"' in resp.data


def test_entities_state_path_resolves(client):
    adapter = client.application.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/entities/api/state", method="GET")

    assert endpoint == "app:entities.api_state"


def test_entities_state_error_returns_envelope(client, monkeypatch):
    from solstone.apps.entities import routes

    def raise_copy_payload():
        raise RuntimeError("copy failed")

    monkeypatch.setattr(routes, "entities_copy_payload", raise_copy_payload)
    resp = client.get("/app/entities/api/state")

    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload["reason_code"] == "entity_operation_failed"
    assert payload["detail"] == "Failed to load entities state."

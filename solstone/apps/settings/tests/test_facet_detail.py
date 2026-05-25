# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from html import unescape
from pathlib import Path

from solstone.apps.settings import copy as settings_copy
from solstone.convey import create_app


def _write_facet(
    journal: Path,
    slug: str,
    *,
    title: str,
    emoji: str = "TF",
    color: str = "#123456",
    muted: bool = False,
) -> None:
    facet_dir = journal / "facets" / slug
    facet_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "title": title,
        "description": f"{title} test facet",
        "emoji": emoji,
        "color": color,
    }
    if muted:
        payload["muted"] = True
    (facet_dir / "facet.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _settings_client(settings_env):
    journal_path, _config = settings_env()
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    config.setdefault("convey", {})["trust_localhost"] = True
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return journal_path, app.test_client()


def test_facet_detail_route_renders_existing_facet(settings_env):
    journal, client = _settings_client(settings_env)
    _write_facet(journal, "test-facet", title="Test Facet")

    response = client.get("/app/settings/facets/test-facet")

    assert response.status_code == 200
    html = unescape(response.get_data(as_text=True))
    assert settings_copy.FACET_DETAIL_SUCCESS_HEADING.format(title="Test Facet") in html
    assert "TF" in html
    assert "#123456" in html
    assert settings_copy.FACET_DETAIL_VALUE_FRAMING.format(title="Test Facet") in html
    assert settings_copy.FACET_DETAIL_PRIMARY_CTA.format(title="Test Facet") in html
    assert settings_copy.FACET_DETAIL_SECONDARY_CTA in html
    assert settings_copy.FACET_DETAIL_TERTIARY_ESCAPE in html
    assert 'href="/app/entities/"' in html
    assert 'href="/app/settings#facets"' in html
    assert 'href="/app/settings"' in html


def test_facet_detail_route_404s_missing_facet(settings_env):
    _journal, client = _settings_client(settings_env)

    response = client.get("/app/settings/facets/nonexistent")

    assert response.status_code == 404


def test_facet_detail_steady_state(settings_env):
    journal, client = _settings_client(settings_env)
    _write_facet(journal, "test-facet", title="Test Facet")

    first = client.get("/app/settings/facets/test-facet")
    second = client.get("/app/settings/facets/test-facet")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_data(as_text=True) == second.get_data(as_text=True)


def test_settings_facets_api_returns_all_facets(settings_env):
    journal, client = _settings_client(settings_env)
    _write_facet(journal, "active-facet", title="Active Facet")
    _write_facet(journal, "muted-facet", title="Muted Facet", muted=True)

    response = client.get("/app/settings/api/facets")

    assert response.status_code == 200
    facets = response.get_json()["facets"]
    by_name = {facet["name"]: facet for facet in facets}
    assert set(by_name) == {"active-facet", "muted-facet"}
    assert by_name["active-facet"] == {
        "name": "active-facet",
        "title": "Active Facet",
        "color": "#123456",
        "emoji": "TF",
        "muted": False,
    }
    assert by_name["muted-facet"]["muted"] is True

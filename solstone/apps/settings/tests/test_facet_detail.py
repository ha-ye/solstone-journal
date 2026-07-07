# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.apps.settings import copy as settings_copy
from solstone.convey import create_app
from solstone.convey.icons import lucide_svg


def _write_facet(
    journal: Path,
    slug: str,
    *,
    title: str,
    emoji: str = "TF",
    icon: str = "",
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
    if icon:
        payload["icon"] = icon
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
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return journal_path, app.test_client()


def test_facet_detail_route_renders_existing_facet(settings_env):
    journal, client = _settings_client(settings_env)
    _write_facet(journal, "test-facet", title="Test Facet")

    response = client.get("/app/settings/facets/test-facet")

    assert response.status_code == 200
    assert 'data-solstone-shell="spa"' in response.get_data(as_text=True)

    state = client.get("/app/settings/api/state")
    assert state.status_code == 200
    state_copy = state.get_json()["settings_copy"]
    assert (
        state_copy["FACET_DETAIL_SUCCESS_HEADING"]
        == settings_copy.FACET_DETAIL_SUCCESS_HEADING
    )
    assert (
        state_copy["FACET_DETAIL_VALUE_FRAMING"]
        == settings_copy.FACET_DETAIL_VALUE_FRAMING
    )
    assert (
        state_copy["FACET_DETAIL_PRIMARY_CTA"] == settings_copy.FACET_DETAIL_PRIMARY_CTA
    )
    assert (
        state_copy["FACET_DETAIL_SECONDARY_CTA"]
        == settings_copy.FACET_DETAIL_SECONDARY_CTA
    )
    assert (
        state_copy["FACET_DETAIL_TERTIARY_ESCAPE"]
        == settings_copy.FACET_DETAIL_TERTIARY_ESCAPE
    )

    facet = client.get("/app/settings/api/facet/test-facet")
    assert facet.status_code == 200
    payload = facet.get_json()
    assert payload["facet"] == "test-facet"
    assert payload["config"]["title"] == "Test Facet"
    assert payload["config"]["description"] == "Test Facet test facet"
    assert payload["config"]["emoji"] == "TF"
    assert payload["config"]["color"] == "#123456"


def test_facet_detail_route_404s_missing_facet(settings_env):
    _journal, client = _settings_client(settings_env)

    response = client.get("/app/settings/facets/nonexistent")

    assert response.status_code == 200
    assert 'data-solstone-shell="spa"' in response.get_data(as_text=True)

    api_response = client.get("/app/settings/api/facet/nonexistent")

    assert api_response.status_code == 404


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
        "icon": "",
        "icon_svg": None,
        "muted": False,
    }
    assert by_name["muted-facet"]["muted"] is True


def test_settings_facets_api_returns_icon_override_svg(settings_env):
    journal, client = _settings_client(settings_env)
    _write_facet(journal, "icon-facet", title="Icon Facet", emoji="📚", icon="brain")

    response = client.get("/app/settings/api/facets")

    assert response.status_code == 200
    facet = response.get_json()["facets"][0]
    assert facet["icon"] == "brain"
    assert facet["icon_svg"] == lucide_svg("brain")


def test_settings_index_has_hidden_guard():
    """The SPA swaps views by toggling the `hidden` attribute on
    #settings-index-view. `.settings-wrap` sets display:flex, which ties the UA
    [hidden] rule on specificity and wins by source order — so without an
    explicit `.settings-wrap[hidden]{display:none}` guard the index stays visible
    above the facet detail (the 2026-07-06 facet-detail-below-fold regression).
    Guard against its removal.
    """
    workspace = (Path(__file__).resolve().parents[1] / "workspace.html").read_text(
        encoding="utf-8"
    )
    assert ".settings-wrap[hidden]" in workspace
    guard = workspace[workspace.index(".settings-wrap[hidden]") :]
    guard = guard[: guard.index("}")]
    assert "display: none" in guard

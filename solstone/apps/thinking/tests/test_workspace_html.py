# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.apps.thinking import copy as thinking_copy
from solstone.convey import create_app

APP_JSON = Path(__file__).resolve().parents[1] / "app.json"
WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"
STATIC = Path(__file__).resolve().parents[1] / "static" / "thinking.js"


def test_workspace_renders_each_lane(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True

    client = app.test_client()
    response = client.get("/app/thinking/", follow_redirects=True)

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data

    workspace_response = client.get("/app/thinking/workspace")
    assert workspace_response.status_code == 200
    html = workspace_response.get_data(as_text=True)
    assert 'id="thinkingHeading"' in html
    assert "/app/thinking/static/thinking.js" in html
    assert "window.THINKING =" not in html
    assert "window.THINKING_COPY =" not in html

    state_response = client.get("/app/thinking/api/state")
    assert state_response.status_code == 200
    payload = state_response.get_json()
    assert set(payload) == {"providers", "keys", "copy"}
    assert payload["copy"] == thinking_copy.thinking_copy_payload()

    assert 'id="providers"' in html
    assert 'id="lane-local"' in html
    assert 'id="lane-confidential"' in html
    assert 'id="lane-byo"' in html
    assert 'id="scoutEnable"' in html
    assert 'id="scoutCheck"' in html
    assert 'id="scoutRefresh"' in html
    assert 'id="scoutDisable"' in html
    assert 'id="scoutLaneOperation"' in html
    assert 'id="scoutLaneOperationLink"' in html
    for view in (
        "main",
        "byo-setup",
        "local-setup",
        "lane-switch",
    ):
        assert f'data-view="{view}"' in html
    assert 'data-view="confidential-setup"' not in html
    assert 'data-open-view="confidential-setup"' not in html
    assert 'id="confidentialLaneOperation"' in html
    assert "set up confidential" in html
    assert 'data-open-view="byo-setup"' in html
    assert 'data-open-view="local-setup"' in html
    assert "data-switch-lane" in html
    for control_id in (
        "field-generate-provider",
        "field-cogitate-provider",
        "field-generate-tier",
        "field-cogitate-tier",
        "field-google-backend",
        "vertexSave",
        "vertexClear",
        "vertexCredsInput",
        "localEndpointUrl",
        "localEndpointModel",
        "localEndpointCredential",
        "localEndpointSave",
        "localEndpointClear",
    ):
        assert f'id="{control_id}"' in html
    assert "<details" in html
    assert "Choose how sol thinks" not in html
    assert "window.THINKING =" not in html
    assert "window.THINKING_COPY =" not in html
    assert "/app/thinking/static/thinking.js" in html


def test_thinking_literal_paths_resolve(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))

    adapter = app.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/thinking/static/thinking.js", method="GET")
    assert endpoint == "app:thinking.static"

    endpoint, _args = adapter.match("/app/thinking/api/state", method="GET")
    assert endpoint == "app:thinking.api_state"


def test_thinking_state_degrades_when_initial_payload_fails(
    settings_env,
    monkeypatch,
):
    from solstone.apps.thinking import routes

    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )

    def raise_config():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(routes, "get_journal_config", raise_config)
    app = create_app(str(journal_path))
    response = app.test_client().get("/app/thinking/api/state")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["providers"] == {}
    assert payload["keys"] == {}
    assert payload["copy"] == thinking_copy.thinking_copy_payload()


def test_scout_consent_static_behavior_is_wired() -> None:
    js = STATIC.read_text(encoding="utf-8")

    assert "window.open(url, '_blank', 'noopener')" in js
    assert "scoutLaneOperationLink" in js
    assert "operation.portal_url || ''" in js
    assert "!!actions.enable && !operationActive" in js


def test_confidential_consent_static_behavior_is_wired() -> None:
    js = STATIC.read_text(encoding="utf-8")

    assert "api/confidential/enable" in js
    assert "api/confidential/disable" in js
    assert "openConsentTab(start?.operation)" in js
    assert "confidential_operation" in js
    assert "switchLane('confidential')" not in js
    assert "confidential-setup" not in js


def test_copy_payload_round_trips_apostrophes() -> None:
    payload = thinking_copy.thinking_copy_payload()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert decoded == payload


def test_thinking_copy_avoids_forbidden_terms() -> None:
    def owner_surface_text(path: Path) -> str:
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(
            line
            for line in lines
            if "SPDX-License-Identifier" not in line
            and "Copyright (c) 2026 sol pbc" not in line
        )

    combined = "\n".join(thinking_copy.thinking_copy_values())
    combined += "\n" + json.loads(APP_JSON.read_text(encoding="utf-8"))["label"]
    combined += "\n" + owner_surface_text(WORKSPACE)
    combined += "\n" + owner_surface_text(STATIC)

    for term in (
        "account",
        "account_id",
        "sign in",
        "log in",
        "subscribe",
        "upgrade",
        "capture",
        "watch",
        "record",
        "monitor",
        "track",
        "collect",
    ):
        assert re.search(rf"\b{re.escape(term)}\b", combined, re.IGNORECASE) is None

    for phrase in ("sol pbc", "this machine", "this device"):
        assert re.search(rf"\b{re.escape(phrase)}\b", combined, re.IGNORECASE) is None


def test_thinking_copy_avoids_banned_absolute_claims() -> None:
    def owner_surface_text(path: Path) -> str:
        lines = path.read_text(encoding="utf-8").splitlines()
        return "\n".join(
            line
            for line in lines
            if "SPDX-License-Identifier" not in line
            and "Copyright (c) 2026 sol pbc" not in line
        )

    combined = "\n".join(thinking_copy.thinking_copy_values())
    combined += "\n" + json.loads(APP_JSON.read_text(encoding="utf-8"))["label"]
    combined += "\n" + owner_surface_text(WORKSPACE)
    combined += "\n" + owner_surface_text(STATIC)

    phrase = "never " + "sees"
    assert re.search(rf"\b{re.escape(phrase)}\b", combined, re.IGNORECASE) is None

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
    assert 'id="confidentialLaneOperation"' not in html
    assert 'id="confidentialLaneStatus"' not in html
    assert 'id="confidentialLaneMore"' in html
    assert 'id="lane-detail-confidential"' in html
    assert "set up confidential" not in html
    assert 'data-open-view="byo-setup"' in html
    assert 'data-open-view="local-setup"' in html
    assert "data-switch-lane" in html
    assert html.count('id="byoLaneStatus"') == 1
    assert 'id="byoKeyStatus"' in html
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


def test_confidential_teaser_static_behavior_is_wired() -> None:
    html = WORKSPACE.read_text(encoding="utf-8")
    js = STATIC.read_text(encoding="utf-8")
    card = re.search(r'<article class="lane" id="lane-confidential"([^>]*)>', html)

    assert card is not None
    assert 'class="laneslot" id="confidentialLaneSlot"' in html
    assert 'id="confidentialLaneMore"' in html
    assert 'aria-controls="lane-detail-confidential"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="lane-detail-confidential"' in html
    assert "data-lane" not in card.group(1)
    assert 'role="button"' not in card.group(1)
    assert "tabindex" not in card.group(1)
    assert "api/confidential" not in js
    assert "function openConsentTab(operation)" in js
    assert "confidentialProvenancePresent" in js
    assert "renderConfidentialDetailPanel" in js
    assert "switchLane('confidential')" not in js
    assert "confidential-setup" not in js


def test_copy_payload_round_trips_apostrophes() -> None:
    payload = thinking_copy.thinking_copy_payload()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert decoded == payload


def test_thinking_copy_lanes_use_init_deck() -> None:
    lanes = thinking_copy.LANES
    by_id = {lane["id"]: lane for lane in lanes}

    assert [lane["id"] for lane in lanes] == ["local", "confidential", "byo"]
    for lane in lanes:
        assert lane.get("sub")
        assert lane.get("description")
    assert by_id["byo"]["label"] == "your own AI engine"
    assert by_id["confidential"]["description"] == "coming — scouts get it first."
    assert by_id["confidential"]["tag"] == "not open yet"
    assert [lane["id"] for lane in lanes if "tag" in lane] == ["confidential"]


def test_thinking_deck_copy_constants() -> None:
    assert thinking_copy.ACTIVE_LANE_LABELS == {
        "none": "not thinking yet",
        "local": "local",
        "confidential": "confidential processing",
        "byo": "your own AI engine",
        "advanced": "advanced split",
    }
    assert thinking_copy.GLANCE == {
        "lane_label": "sol is thinking with",
        "local": {
            "value": "a model on your device",
            "detail": "runs right on this computer — nothing leaves for sol to think",
        },
        "byo_key": {
            "value": "your own key · {provider}",
            "detail": "a key you added — stays in your journal, never shared",
        },
        "byo_endpoint": {
            "value": "your own endpoint",
            "detail": "sol thinks at the endpoint you set — your server, your rules",
        },
        "byo_scout": {
            "value": "scout · we cover it",
            "detail": (
                "covered through the scout program while you're in alpha — stays in "
                "your journal"
            ),
        },
        "none": {
            "value": "not thinking yet",
            "detail": (
                "sol is keeping your journal — but it can't answer you until you "
                "choose how it thinks below."
            ),
        },
    }
    assert thinking_copy.BYO_SETUP == {
        "intro": (
            "bring your own AI engine. sol pbc is never in the path — it stays in "
            "your journal."
        ),
        "chooser_key": "a key",
        "chooser_endpoint": "your own endpoint",
        "key_heading": "pick your provider",
        "key_sub": (
            "all three work the same in solstone. choose the one you have a key for."
        ),
        "get_key": "get a key ↗",
        "paste_title": "paste your {provider} key",
        "key_hint": (
            "it stays in your journal — sol pbc never sets it up or sees it. paste "
            "it once; sol uses it from here."
        ),
        "terms": (
            "your questions are processed by {provider}, stored only briefly for "
            "processing, and never used for training."
        ),
        "terms_link": "terms ↗",
        "endpoint_heading": "point sol at your own endpoint",
        "endpoint_sub": "any OpenAI-compatible endpoint — your server, your rules.",
        "endpoint_honesty": (
            "sol checks the endpoint works before it relies on it. if it can't "
            "reach it, sol tells you — it never quietly falls back to anyone else."
        ),
        "scout_heading": "in the scout program?",
        "scout_sub": (
            "be an early tester for solstone — we'll cover your thinking, using Gemini."
        ),
        "scout_terms_link": "scout program terms ↗",
        "scout_provenance": (
            "covered through the scout program — the key stays in your journal."
        ),
    }
    assert thinking_copy.LANE_SWITCH == {
        "heading": "switch how sol thinks?",
        "current_label": "now",
        "target_label": "switch to",
        "confirm": "switch",
        "cancel": "keep using {current}",
        "to_local_note": (
            "sol will think right on this computer. your {current} setup stays saved "
            "— switch back anytime."
        ),
        "to_byo_note": "sol will think with your own engine. {setup} is still here.",
        "setup_key": "a saved key",
        "setup_endpoint": "your endpoint",
        "setup_scout": "scout",
    }
    assert thinking_copy.LOCAL_INSTALL == {
        "phases": {
            "resolving": "resolving",
            "downloading": "downloading",
            "verifying": "verifying",
            "installing": "installing",
        },
        "pill_inflight": "setting up",
        "pill_failed": "couldn't finish",
        "retry": "try again",
        "install": "install local model",
        "notice_inflight": (
            "local thinking will stay in your journal once setup finishes."
        ),
    }
    assert thinking_copy.CONFIDENTIAL_MORE_LABEL == "how it works →"


def test_thinking_copy_payload_carries_confidential_lane_detail() -> None:
    payload = thinking_copy.thinking_copy_payload()

    assert payload["confidential"]["lane_detail"] == dict(
        thinking_copy.CONFIDENTIAL_LANE_DETAIL
    )
    assert (
        payload["confidential"]["more_label"] == thinking_copy.CONFIDENTIAL_MORE_LABEL
    )


def test_thinking_copy_payload_shape_carries_deck_blocks() -> None:
    payload = thinking_copy.thinking_copy_payload()

    assert payload["active_lane_labels"] == dict(thinking_copy.ACTIVE_LANE_LABELS)
    assert payload["glance"] == dict(thinking_copy.GLANCE)
    assert payload["byo_setup"] == dict(thinking_copy.BYO_SETUP)
    assert payload["lane_switch"] == dict(thinking_copy.LANE_SWITCH)
    assert payload["local_install"] == {
        **thinking_copy.LOCAL_INSTALL,
        "phases": dict(thinking_copy.LOCAL_INSTALL["phases"]),
    }
    assert "byo" not in payload


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

    for phrase in ("this machine", "this device"):
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

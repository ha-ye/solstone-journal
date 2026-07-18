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
    config["setup"] = {"completed_at": 1700000000000}
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
        "confidential-setup",
        "local-setup",
        "lane-switch",
    ):
        assert f'data-view="{view}"' in html
    assert 'data-open-view="confidential-setup"' in html
    assert 'id="confidentialLaneOperation"' in html
    assert 'id="confidentialLaneOperationLink"' in html
    assert (
        re.search(
            r'<div\b(?=[^>]*\bclass="notice")(?=[^>]*\bid="confidentialNotice")'
            r"(?=[^>]*\bhidden\b)[^>]*>",
            html,
        )
        is not None
    )
    assert 'id="confidentialLaneStatus"' in html
    assert 'id="confidentialEnable"' in html
    assert 'id="confidentialRecheck"' in html
    assert 'id="confidentialDisable"' in html
    assert 'id="confidentialAudioRow"' in html
    assert 'for="confidentialAudioToggle"' in html
    assert 'type="checkbox" id="confidentialAudioToggle"' in html
    assert 'id="confidentialAudioDescription"' in html
    assert 'id="confidentialAudioNote"' in html
    assert 'id="confidentialAudioDeferral"' in html
    assert 'id="confidentialLaneMore"' in html
    assert 'id="lane-detail-confidential"' in html
    assert 'data-open-view="byo-setup"' in html
    assert 'data-open-view="local-setup"' in html
    assert "data-switch-lane" in html
    assert html.count('id="byoLaneStatus"') == 1
    assert 'id="byoKeyStatus"' in html
    for control_id in (
        "localEndpointUrl",
        "localEndpointModel",
        "localEndpointCredential",
        "localEndpointSave",
        "localEndpointClear",
    ):
        assert f'id="{control_id}"' in html
    assert "advanced provider controls" not in html
    assert "Vertex" not in html
    assert "Choose how sol thinks" not in html
    assert "window.THINKING =" not in html
    assert "window.THINKING_COPY =" not in html
    assert "/app/thinking/static/thinking.js" in html


def test_workspace_loading_copy_is_folded():
    workspace = WORKSPACE.read_text(encoding="utf-8")
    static = STATIC.read_text(encoding="utf-8")

    assert "loading thinking settings…" in workspace
    assert "loading thinking settings…" in static
    assert "loading thinking settings..." not in workspace
    assert "loading thinking settings..." not in static


def test_thinking_literal_paths_resolve(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
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
    config["setup"] = {"completed_at": 1700000000000}
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


def test_confidential_live_static_behavior_is_wired() -> None:
    html = WORKSPACE.read_text(encoding="utf-8")
    js = STATIC.read_text(encoding="utf-8")
    card = re.search(r'<article class="lane" id="lane-confidential"([^>]*)>', html)

    assert card is not None
    assert 'class="laneslot" id="confidentialLaneSlot"' in html
    assert 'id="confidentialLaneMore"' in html
    assert 'aria-controls="lane-detail-confidential"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="lane-detail-confidential"' in html
    assert 'data-lane="confidential"' in card.group(1)
    assert 'role="button"' in card.group(1)
    assert 'tabindex="0"' in card.group(1)
    assert "api/confidential/enable" in js
    assert "api/confidential/recheck" in js
    assert "api/confidential/disable" in js
    assert "function openConsentTab(operation)" in js
    assert "confidentialProvenancePresent" in js
    assert "renderConfidentialDetailPanel" in js
    assert "function pollConfidentialUntilTerminal(" in js
    assert "if (lane === 'confidential')" in js
    assert "showView('confidential-setup');" in js
    assert "confidential-setup" in js


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
    assert (
        by_id["confidential"]["description"]
        == "sol pbc runs the model on confidential GPUs."
    )
    assert "tag" not in by_id["confidential"]
    assert [lane["id"] for lane in lanes if "tag" in lane] == []


def test_thinking_deck_copy_constants() -> None:
    assert thinking_copy.ACTIVE_LANE_LABELS == {
        "none": "not thinking yet",
        "local": "local",
        "confidential": "confidential processing",
        "byo": "your own AI engine",
    }
    assert thinking_copy.GLANCE == {
        "lane_label": "sol is thinking with",
        "local": {
            "value": "a model on your device",
            "detail": "runs right on this computer — nothing leaves for sol to think",
        },
        "byo_key": {
            "value": "your own key · {provider}",
            "detail": (
                "thinking with {model} — a key you added, stays in your journal, "
                "never shared"
            ),
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
        "confidential_checking": {
            "label": "sol is waiting on",
            "value": "confidential processing",
            "detail": "checking the hardware…",
        },
        "confidential_verified": {
            "label": "sol is thinking with",
            "value": "confidential processing",
            "detail": "{legs} · {substrate} · checked {checked}",
        },
        "confidential_blocked": {
            "label": "sol is holding",
            "value": "confidential processing",
            "detail": "{message}",
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
        "endpoint_sub": "any OpenAI-compatible URL. your server, your rules.",
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
        "paste_cta": "check this key →",
        "checking_key": "checking your key with {provider}…",
        "key_ok_strip": "your {provider} key works — checked {when}",
        "check_again": "check again",
        "use_different_key": "use a different key",
        "key_failed": (
            "this key didn't work — {reason}. paste a different key, or fix it "
            "with {provider} and check again."
        ),
        "reason_rejected": "{provider} didn't accept it",
        "reason_quota": "{provider} says it's out of quota right now",
        "reason_network": "couldn't reach {provider} — check your connection",
        "reason_unknown": "{provider} couldn't be checked",
        "model_heading": "pick the model your key uses",
        "model_sub": (
            "three sizes from {provider} — or name one yourself. you can change "
            "this anytime."
        ),
        "tier_blurb_top": (
            "the most capable — for the heaviest thinking, at the highest cost on "
            "your key."
        ),
        "tier_blurb_mid": "capable and quick. the middle of the range.",
        "tier_blurb_lite": (
            "light and quick — solstone tunes sol's thinking for small models, so "
            "this one does the job well. it's also the least expensive on your key."
        ),
        "tier_tag_suggested": "suggested",
        "tier_tag_current": "current",
        "custom_toggle": "or name a specific model",
        "custom_label": "model id",
        "custom_check": "check it",
        "custom_checking": "asking {provider} about {model}…",
        "custom_ok": "✓ {model} answered — you can use it",
        "custom_not_found": '{provider} doesn\'t offer "{model}" to this key.',
        "custom_cost_note": (
            "a model we don't recognize still works — sol just may not be able to "
            "show what it costs per use yet."
        ),
        "model_save": "think with {label}",
        "model_saving": "checking {model} with your key…",
        "probe_failed_save": ("your key works, but {model} didn't answer — {reason}."),
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
    assert not hasattr(thinking_copy, "CONFIDENTIAL_TRUST_EGRESS")
    assert thinking_copy.CONFIDENTIAL_LANE_EGRESS == (
        "when it's on, the thinking leaves your device — text, images, and "
        "(with the audio switch on, its default) your audio for "
        "transcription. your journal itself never leaves."
    )
    assert thinking_copy.CONFIDENTIAL_SETUP_EGRESS_AUDIO_ON == (
        "what leaves your device: the text and images sol needs a model to "
        "work through, and your audio for transcription. your journal itself "
        "never leaves."
    )
    assert thinking_copy.CONFIDENTIAL_SETUP_EGRESS_AUDIO_OFF == (
        "what leaves your device: the text and images sol needs a model to "
        "work through. your audio stays on your device — speech becomes "
        "text there."
    )
    assert thinking_copy.CONFIDENTIAL_AUDIO == {
        "label": "transcribe audio on the service",
        "on": (
            "your audio is transcribed on the service — sent over the verified "
            "channel, processed, and not kept. on while confidential processing "
            "is in use."
        ),
        "off": "speech becomes text on your device. your audio doesn't leave.",
        "note": "turn it off any time — it takes effect on the next thing you say.",
        "deferral": (
            "transcription is waiting — nothing is sent until your journal "
            "verifies the service. your audio stays on your device and "
            "transcribes once the check passes."
        ),
    }
    assert thinking_copy.CONFIDENTIAL_TRUST_BEATS == {
        "heading": "confidential processing",
        "sub": "operated by sol pbc",
        "egress_audio_on": (
            "what leaves your device: the text and images sol needs a model to "
            "work through, and your audio for transcription. your journal itself "
            "never leaves."
        ),
        "egress_audio_off": (
            "what leaves your device: the text and images sol needs a model to "
            "work through. your audio stays on your device — speech "
            "becomes text there."
        ),
        "claims": "no content is retained · no human reviews it · nothing is used to train",
        "attestation": (
            "your journal must verify the service before anything is sent — if "
            "it can't verify, it doesn't send."
        ),
        "substrate": (
            "sol pbc runs the model itself on confidential GPUs in Microsoft "
            "Azure. the hardware boundary keeps the cloud host excluded from "
            "what's processed — no third-party AI provider is in the path."
        ),
    }
    assert thinking_copy.CONFIDENTIAL_LANE_DETAIL == {
        "heading": thinking_copy.CONFIDENTIAL_TRUST_HEADING,
        "sub": thinking_copy.CONFIDENTIAL_TRUST_SUB,
        "mechanism": thinking_copy.CONFIDENTIAL_TRUST_SUBSTRATE,
        "egress": thinking_copy.CONFIDENTIAL_LANE_EGRESS,
        "claims": thinking_copy.CONFIDENTIAL_TRUST_CLAIMS,
        "attestation": thinking_copy.CONFIDENTIAL_TRUST_FAIL_CLOSED,
        "early_access": "confidential processing is coming — scouts get it first.",
    }
    assert thinking_copy.CONFIDENTIAL_ATTESTATION_STATES == {
        "off": "",
        "verifying": "checking the hardware…",
        "verified": "{legs} · {substrate} · checked {checked}",
        "failed": "couldn't verify the service — sol isn't sending.",
        "stale": "your journal needs to re-check the service before sending.",
        "unreachable": "can't reach confidential processing right now — sol isn't sending.",
    }
    assert thinking_copy.CONFIDENTIAL_OPERATION_STATES == {
        "starting": "opening your browser to confirm…",
        "waiting": "finish turning it on in your browser",
        "early_access": "confidential processing is coming — scouts get it first.",
        "repair_needed": "couldn't verify the service — sol isn't sending.",
    }
    assert thinking_copy.CONFIDENTIAL_ACTIONS == {
        "off": "turn on confidential processing →",
        "enabled": "turn off",
        "recheck": "check again",
    }


def test_thinking_copy_payload_carries_confidential_lane_detail() -> None:
    payload = thinking_copy.thinking_copy_payload()

    assert payload["confidential"]["lane_detail"] == dict(
        thinking_copy.CONFIDENTIAL_LANE_DETAIL
    )
    assert (
        payload["confidential"]["more_label"] == thinking_copy.CONFIDENTIAL_MORE_LABEL
    )
    assert payload["confidential"]["setup"] == {
        "trust_beats": dict(thinking_copy.CONFIDENTIAL_SETUP["trust_beats"])
    }
    assert payload["confidential"]["audio"] == dict(thinking_copy.CONFIDENTIAL_AUDIO)
    assert payload["confidential"]["attestation_states"] == dict(
        thinking_copy.CONFIDENTIAL_ATTESTATION_STATES
    )
    assert payload["confidential"]["operation_states"] == dict(
        thinking_copy.CONFIDENTIAL_OPERATION_STATES
    )
    assert payload["confidential"]["actions"] == dict(
        thinking_copy.CONFIDENTIAL_ACTIONS
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


def test_thinking_state_serves_byo_model_copy_bytes(settings_env) -> None:
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True

    response = app.test_client().get("/app/thinking/api/state")

    assert response.status_code == 200
    served_copy = response.get_json()["copy"]
    assert served_copy["glance"]["byo_key"]["detail"] == (
        "thinking with {model} — a key you added, stays in your journal, never shared"
    )
    assert {
        key: served_copy["byo_setup"][key]
        for key in (
            "paste_cta",
            "checking_key",
            "key_ok_strip",
            "check_again",
            "use_different_key",
            "key_failed",
            "reason_rejected",
            "reason_quota",
            "reason_network",
            "reason_unknown",
            "model_heading",
            "model_sub",
            "tier_blurb_top",
            "tier_blurb_mid",
            "tier_blurb_lite",
            "tier_tag_suggested",
            "tier_tag_current",
            "custom_toggle",
            "custom_label",
            "custom_check",
            "custom_checking",
            "custom_ok",
            "custom_not_found",
            "custom_cost_note",
            "model_save",
            "model_saving",
            "probe_failed_save",
        )
    } == {
        "paste_cta": "check this key →",
        "checking_key": "checking your key with {provider}…",
        "key_ok_strip": "your {provider} key works — checked {when}",
        "check_again": "check again",
        "use_different_key": "use a different key",
        "key_failed": (
            "this key didn't work — {reason}. paste a different key, or fix it "
            "with {provider} and check again."
        ),
        "reason_rejected": "{provider} didn't accept it",
        "reason_quota": "{provider} says it's out of quota right now",
        "reason_network": "couldn't reach {provider} — check your connection",
        "reason_unknown": "{provider} couldn't be checked",
        "model_heading": "pick the model your key uses",
        "model_sub": (
            "three sizes from {provider} — or name one yourself. you can change "
            "this anytime."
        ),
        "tier_blurb_top": (
            "the most capable — for the heaviest thinking, at the highest cost on "
            "your key."
        ),
        "tier_blurb_mid": "capable and quick. the middle of the range.",
        "tier_blurb_lite": (
            "light and quick — solstone tunes sol's thinking for small models, so "
            "this one does the job well. it's also the least expensive on your key."
        ),
        "tier_tag_suggested": "suggested",
        "tier_tag_current": "current",
        "custom_toggle": "or name a specific model",
        "custom_label": "model id",
        "custom_check": "check it",
        "custom_checking": "asking {provider} about {model}…",
        "custom_ok": "✓ {model} answered — you can use it",
        "custom_not_found": '{provider} doesn\'t offer "{model}" to this key.',
        "custom_cost_note": (
            "a model we don't recognize still works — sol just may not be able to "
            "show what it costs per use yet."
        ),
        "model_save": "think with {label}",
        "model_saving": "checking {model} with your key…",
        "probe_failed_save": ("your key works, but {model} didn't answer — {reason}."),
    }


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

    for phrase in (
        "this machine",
        "this device",
        "sealed",
        "sealed engine",
        "not sol pbc's to read",
        "only you can read it",
        "checks the hardware before it sends",
        "verified ✓",
    ):
        pattern = (
            rf"\b{re.escape(phrase)}\b" if phrase[-1].isalnum() else re.escape(phrase)
        )
        assert re.search(pattern, combined, re.IGNORECASE) is None


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

    for phrase in (
        "never " + "sees",
        "recordings stay here",
        "audio never leaves",
        "never your audio",
        "speech becomes text on your device first",
    ):
        assert re.search(rf"\b{re.escape(phrase)}\b", combined, re.IGNORECASE) is None

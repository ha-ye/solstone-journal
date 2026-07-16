# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for the rendered link reach shell."""

from __future__ import annotations

import html
import re

from solstone.apps.network import copy


def _normalized_body(body: str) -> str:
    return (
        html.unescape(body)
        .replace('\\"', '"')
        .replace("\\u0027", "'")
        .replace("\\u00b7", "·")
        .replace("\\u2014", "—")
        .replace("\\u2192", "→")
        .replace("\\u25b8", "▸")
        .replace("\\u2026", "…")
    )


def _link_state(env) -> dict[str, object]:
    response = env.client.get("/app/network/api/state")
    assert response.status_code == 200
    return response.get_json()


def test_workspace_renders_reach_shell_copy_and_static_guards(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    body_text = _normalized_body(body)

    for gone in (
        "reach your solstone from anywhere",
        "blind by construction",
        "reachable from the internet",
        "typeof data.enrolled !== 'boolean'",
        # no unconditional relay claim in the header — false in direct posture
        "sol pbc carries the connection — but can never see inside it",
    ):
        assert gone not in body_text

    state = _link_state(env)
    payload = state["link_copy"]
    assert payload["STATUS_SENTENCES"] == copy.STATUS_SENTENCES
    for name in (
        "BRANDLOCK_LINE",
        "REACH_SELECTOR_TITLE",
        "REACH_SELECTOR_HINT",
        "MODE_BYO_NAME",
        "MODE_BYO_DESC",
        "MODE_BYO_DISCLOSURE",
        "MODE_HOSTED_NAME",
        "MODE_HOSTED_DESC",
        "MODE_HOSTED_DISCLOSURE",
        "MODE_BYO_BODY_NOTE",
        "MODE_HOSTED_SETUP_NOTE",
        "MODE_HOSTED_SETUP_CTA",
        "APP_ONOFF_LABEL",
        "APP_ONOFF_SUB_BYO",
        "APP_ONOFF_SUB_HOSTED",
        "REACH_HOST_ADDRESS_DISCLOSURE",
        "REACH_HOST_ADDRESS_PLACEHOLDER",
        "REACH_HOST_ADDRESS_APPLY_LABEL",
        "REACH_HOST_ADDRESS_CLEAR_LABEL",
    ):
        assert payload[name] == getattr(copy, name)
        assert name in body
    assert '<p class="link-brandlock">' in body
    assert "background: #E8923A; color: #1A1A1A" in body
    assert "#B06A1A" in body
    assert "#E8923A" in body
    selector_start = body.index('<section id="link-reach-selector"')
    selector_end = body.index('<div id="link-private-link-operation"', selector_start)
    selector = body[selector_start:selector_end]
    assert 'id="link-seg-byo"' in selector
    assert 'id="link-seg-hosted"' in selector
    assert 'role="radiogroup"' in selector
    assert 'role="radio"' in selector
    byo_start = selector.index('id="link-mode-byo-body"')
    hosted_setup_start = selector.index('id="link-mode-hosted-setup"')
    hosted_active_start = selector.index('id="link-mode-hosted-active"')
    byo_body = selector[byo_start:hosted_setup_start]
    hosted_setup_body = selector[hosted_setup_start:hosted_active_start]
    assert 'id="link-private-link-setup"' in hosted_setup_body
    assert "https://services.solstone.app/" not in byo_body
    for expected in (
        'id="link-home-candidates-picker"',
        'id="link-home-candidates-list"',
        'id="link-home-candidates-problem"',
        'id="link-host-address-override"',
        'id="link-host-address-input"',
        'id="link-host-address-apply"',
        'id="link-host-address-clear"',
        'id="link-host-address-error"',
        'id="link-private-link-operation"',
        "'/app/network/host-address'",
        "'/app/network/private-link/enable'",
        "'/app/network/api/private-link'",
        "'/app/network/private-link/disable'",
    ):
        assert expected in body
    assert "let viewedMode = null;" in body
    assert "let lastPosture = null;" in body
    assert "let reachRevealed = false;" in body
    assert "renderHomeCandidates(data || {});" in body
    assert "appOnOff.hidden = reachability !== 'online';" in body
    for removed_export in (
        "REACH_HOST_ADDRESS_DISCLOSURE:",
        "REACH_HOST_ADDRESS_PLACEHOLDER:",
        "REACH_HOST_ADDRESS_APPLY_LABEL:",
        "REACH_HOST_ADDRESS_CLEAR_LABEL:",
        "REACH_SPL_ACTIVE_BODY:",
        "REACH_SPL_TRUST_LINE:",
        "REACH_SPL_MANAGE_LABEL:",
        "REACH_SPL_CONNECTING_NOTE:",
        "CHECK_AGAIN_LABEL:",
    ):
        assert removed_export not in body

    assert (
        '<a href="https://services.solstone.app/" target="_blank" '
        'rel="noopener noreferrer" data-copy="REACH_SPL_MANAGE_LABEL"></a>' in body
    )
    for color in ("#1e7b42", "#b88400", "#a53a1f"):
        assert color in body
    assert "SurfaceState.replaceLoading('link-status-panel'" in body
    assert 'id="link-pair-btn"' in body
    assert 'data-copy="DEVICE_PAIR_CTA"' in body
    assert payload["DEVICE_PAIR_CTA"] == copy.DEVICE_PAIR_CTA

    for forbidden in (
        "'/posture'",
        '"/posture"',
        "posture-set",
        "'/config'",
        '"/config"',
    ):
        assert forbidden not in body


def test_workspace_renders_hosted_mode_and_states(link_env) -> None:
    env = link_env(
        posture="spl",
        service_token="svc-token",
    )
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    state = _link_state(env)

    assert state["posture"] == "spl"
    assert 'id="link-reach-selector"' in body
    assert re.search(r'id="link-seg-byo"[^>]+class="link-seg is-selected"', body)
    assert re.search(r'id="link-seg-byo"[^>]+aria-checked="true"', body)
    assert re.search(r'id="link-seg-hosted"[^>]+aria-checked="false"', body)
    assert re.search(r'<div id="link-mode-byo-body"[^>]*>', body)
    assert re.search(r'<div id="link-mode-hosted-setup"[^>]+hidden', body)
    assert re.search(r'<div id="link-mode-hosted-active"[^>]+hidden', body)
    for name in (
        "REACH_SPL_ACTIVE_BODY",
        "REACH_SPL_TRUST_LINE",
        "REACH_SPL_MANAGE_LABEL",
        "PRIVATE_LINK_DISABLE_CTA",
    ):
        assert state["link_copy"][name] == getattr(copy, name)
        assert f'data-copy="{name}"' in body
    hosted_start = body.index('<div id="link-mode-hosted-active"')
    hosted_end = body.index("</div>", hosted_start)
    hosted_body = body[hosted_start:hosted_end]
    assert 'data-copy="REACH_SPL_MANAGE_LABEL"' in hosted_body
    assert 'id="link-private-link-disable"' in hosted_body

    assert 'id="link-spl-connecting-note"' in body
    assert 'data-copy="REACH_SPL_CONNECTING_NOTE"' in body
    assert 'id="link-spl-check-again"' in body
    assert 'data-copy="CHECK_AGAIN_LABEL"' in body
    assert "splCheckAgain?.addEventListener('click', () => {" in body
    assert "refreshPrivateLinkStatus();" in body


def test_workspace_home_candidate_picker_markup_stays_in_byo_body(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)

    byo_start = body.index('id="link-mode-byo-body"')
    hosted_setup_start = body.index('id="link-mode-hosted-setup"', byo_start)
    hosted_active_start = body.index('id="link-mode-hosted-active"', hosted_setup_start)
    byo_body = body[byo_start:hosted_setup_start]
    hosted_body = body[hosted_setup_start:hosted_active_start]

    picker_idx = byo_body.index('id="link-home-candidates-picker"')
    override_idx = byo_body.index('id="link-host-address-override"')
    assert picker_idx < override_idx
    assert 'id="link-home-candidates-list"' in byo_body
    assert 'id="link-home-candidates-problem"' in byo_body
    assert "data-refresh-fail:REACH_HOME_CANDIDATES_REFRESH_FAIL" in body
    assert "data-unavailable:REACH_HOME_CANDIDATES_UNAVAILABLE" in body
    assert 'data-copy="REACH_HOME_CANDIDATES_LABEL"' in body
    payload = _link_state(env)["link_copy"]
    assert payload["REACH_HOME_CANDIDATES_LABEL"] == copy.REACH_HOME_CANDIDATES_LABEL
    assert (
        payload["REACH_HOME_CANDIDATES_REFRESH_FAIL"]
        == copy.REACH_HOME_CANDIDATES_REFRESH_FAIL
    )
    assert (
        payload["REACH_HOME_CANDIDATES_UNAVAILABLE"]
        == copy.REACH_HOME_CANDIDATES_UNAVAILABLE
    )
    assert 'id="link-home-candidates-picker"' not in hosted_body


def test_workspace_home_candidate_picker_js_paths(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)

    render_start = body.index("function renderHomeCandidates")
    render_end = body.index("function renderSplState", render_start)
    render_body = body[render_start:render_end]
    assert "data?.home_candidates_state === 'unavailable'" in render_body
    assert "homeCandidatesProblem.textContent =" in render_body
    assert "homeCandidatesList.hidden = true;" in render_body
    assert (
        "Array.isArray(data?.home_candidates) ? data.home_candidates : []"
        in render_body
    )
    assert "if (candidates.length < 2)" in render_body
    assert "radio.type = 'radio';" in render_body
    assert "radio.name = 'link-home-candidate';" in render_body
    assert "radio.checked = Boolean(candidate.selected);" in render_body
    assert "selectHomeCandidate(address);" in render_body

    select_start = body.index("async function selectHomeCandidate")
    select_end = body.index("function renderHomeCandidates", select_start)
    select_body = body[select_start:select_end]
    assert "setHomeCandidateRadiosDisabled(true);" in select_body
    assert "const refreshed = await submitHostAddress(address);" in select_body
    assert "if (!refreshed)" in select_body
    assert (
        "showHomeCandidateWriteError(homeCandidatesPicker?.dataset.refreshFail || '');"
        in select_body
    )
    assert "restoreHomeCandidatesFromStatus();" in select_body

    error_start = body.index("function showHomeCandidateWriteError")
    error_end = body.index("function restoreHomeCandidatesFromStatus", error_start)
    error_body = body[error_start:error_end]
    assert "setHostAddressError(message || '');" in error_body
    assert "if (hostAddressOverride) hostAddressOverride.open = true;" in error_body

    submit_start = body.index("async function submitHostAddress")
    submit_end = body.index("async function applyHostAddressOverride", submit_start)
    submit_body = body[submit_start:submit_end]
    assert "'/app/network/host-address'" in submit_body
    assert "JSON.stringify({ home_address: address })" in submit_body
    assert "setHostAddressError('');" in submit_body
    assert "return await refreshStatus();" in submit_body

    refresh_start = body.index("async function refreshStatus")
    refresh_end = body.index("function privateLinkSleep", refresh_start)
    refresh_body = body[refresh_start:refresh_end]
    assert "applyStatus(data || {});" in refresh_body
    assert "return true;" in refresh_body
    assert "return false;" in refresh_body


def test_workspace_keeps_spl_trust_line_out_of_header_and_direct_card(
    link_env,
) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)

    header = body[body.index("<header") : body.index("</header>")]
    byo_start = body.index('<div id="link-mode-byo-body"')
    hosted_setup_start = body.index('<div id="link-mode-hosted-setup"', byo_start)
    byo_body = body[byo_start:hosted_setup_start]
    hosted_start = body.index('<div id="link-mode-hosted-active"', hosted_setup_start)
    hosted_end = body.index("</div>", hosted_start)
    hosted_body = body[hosted_start:hosted_end]

    assert 'data-copy="REACH_SPL_TRUST_LINE"' not in header
    assert 'data-copy="REACH_SPL_TRUST_LINE"' not in byo_body
    assert 'data-copy="REACH_SPL_TRUST_LINE"' in hosted_body


def test_workspace_maps_spl_status_without_red_offline_dot(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)

    select_start = body.index("function selectStatusSentenceKey")
    select_end = body.index("function setStatusSentence", select_start)
    select_body = body[select_start:select_end]
    assert (
        "if (reachability === 'lan-unreachable') return 'lan_unreachable';"
        in select_body
    )
    assert "if (posture === 'spl')" in select_body
    assert "if (reachability === 'offline') return 'spl_offline';" in select_body
    assert select_body.index("if (posture === 'spl')") < select_body.index(
        "if (reachability === 'offline') return 'offline';"
    )

    status_start = body.index("function setStatusSentence")
    status_end = body.index("function renderVpnCandidates", status_start)
    status_body = body[status_start:status_end]
    assert "['direct_online', 'direct_online_vpn', 'spl_online']" in status_body
    assert "['offline', 'lan_unreachable']" in status_body
    assert "spl_offline" not in status_body

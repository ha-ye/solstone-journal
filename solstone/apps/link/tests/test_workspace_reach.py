# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for the rendered link reach shell."""

from __future__ import annotations

import html
import re

from solstone.apps.link import copy


def _normalized_body(body: str) -> str:
    return (
        html.unescape(body)
        .replace('\\"', '"')
        .replace("\\u0027", "'")
        .replace("\\u00b7", "·")
        .replace("\\u2014", "—")
        .replace("\\u2192", "→")
        .replace("\\u25b8", "▸")
    )


def test_workspace_renders_reach_shell_copy_and_static_guards(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/link/")

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

    for value in copy.STATUS_SENTENCES.values():
        assert value in body_text
    for value in (
        copy.BRANDLOCK_LINE,
        copy.REACH_SELECTOR_TITLE,
        copy.REACH_SELECTOR_HINT,
        copy.MODE_BYO_NAME,
        copy.MODE_BYO_DESC,
        copy.MODE_BYO_DISCLOSURE,
        copy.MODE_HOSTED_NAME,
        copy.MODE_HOSTED_DESC,
        copy.MODE_HOSTED_DISCLOSURE,
        copy.MODE_BYO_BODY_NOTE,
        copy.MODE_HOSTED_SETUP_NOTE,
        copy.MODE_HOSTED_SETUP_CTA,
        copy.APP_ONOFF_LABEL,
        copy.APP_ONOFF_SUB_BYO,
        copy.APP_ONOFF_SUB_HOSTED,
    ):
        assert value in body_text
    assert copy.REACH_HOST_ADDRESS_DISCLOSURE in body_text
    assert copy.REACH_HOST_ADDRESS_PLACEHOLDER in body_text
    assert copy.REACH_HOST_ADDRESS_APPLY_LABEL in body_text
    assert copy.REACH_HOST_ADDRESS_CLEAR_LABEL in body_text
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
        'id="link-host-address-override"',
        'id="link-host-address-input"',
        'id="link-host-address-apply"',
        'id="link-host-address-clear"',
        'id="link-host-address-error"',
        'id="link-private-link-operation"',
        "'/app/link/host-address'",
        "'/app/link/private-link/enable'",
        "'/app/link/api/private-link'",
        "'/app/link/private-link/disable'",
    ):
        assert expected in body
    assert "let viewedMode = null;" in body
    assert "let lastPosture = null;" in body
    assert "let reachRevealed = false;" in body
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

    assert re.search(
        r'<a href="https://services\.solstone\.app/" '
        r'target="_blank" rel="noopener noreferrer">[^<]+</a>',
        body,
    )
    for color in ("#1e7b42", "#b88400", "#a53a1f"):
        assert color in body
    assert "SurfaceState.replaceLoading('link-status-panel'" in body
    assert 'id="link-pair-btn"' in body
    assert "pair a device" in body_text

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
        totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
    )
    response = env.client.get("/app/link/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    body_text = _normalized_body(body)

    assert 'id="link-reach-selector"' in body
    assert re.search(r'id="link-seg-hosted"[^>]+class="link-seg is-selected"', body)
    assert re.search(r'id="link-seg-hosted"[^>]+aria-checked="true"', body)
    assert re.search(r'id="link-seg-byo"[^>]+aria-checked="false"', body)
    assert re.search(r'<div id="link-mode-byo-body"[^>]+hidden', body)
    assert re.search(r'<div id="link-mode-hosted-setup"[^>]+hidden', body)
    assert re.search(r'<div id="link-mode-hosted-active"[^>]*>', body)
    for value in (
        copy.REACH_SPL_ACTIVE_BODY,
        copy.REACH_SPL_TRUST_LINE,
        copy.REACH_SPL_MANAGE_LABEL,
        copy.PRIVATE_LINK_DISABLE_CTA,
    ):
        assert value in body_text
    hosted_start = body_text.index('<div id="link-mode-hosted-active"')
    hosted_end = body_text.index("</div>", hosted_start)
    hosted_body = body_text[hosted_start:hosted_end]
    assert re.search(
        r'<a href="https://services\.solstone\.app/" '
        r'target="_blank" rel="noopener noreferrer">'
        + re.escape(copy.REACH_SPL_MANAGE_LABEL)
        + r"</a>",
        hosted_body,
    )
    assert 'id="link-private-link-disable"' in hosted_body

    assert 'id="link-spl-connecting-note"' in body
    assert copy.REACH_SPL_CONNECTING_NOTE in body_text
    assert 'id="link-spl-check-again"' in body
    assert f"[ {copy.CHECK_AGAIN_LABEL} ]" in body_text
    assert "splCheckAgain?.addEventListener('click', () => {" in body
    assert "refreshPrivateLinkStatus();" in body


def test_workspace_keeps_spl_trust_line_out_of_header_and_direct_card(
    link_env,
) -> None:
    env = link_env()
    response = env.client.get("/app/link/")

    assert response.status_code == 200
    body_text = _normalized_body(response.get_data(as_text=True))

    header = body_text[body_text.index("<header") : body_text.index("</header>")]
    byo_start = body_text.index('<div id="link-mode-byo-body"')
    hosted_setup_start = body_text.index('<div id="link-mode-hosted-setup"', byo_start)
    byo_body = body_text[byo_start:hosted_setup_start]
    hosted_start = body_text.index(
        '<div id="link-mode-hosted-active"', hosted_setup_start
    )
    hosted_end = body_text.index("</div>", hosted_start)
    hosted_body = body_text[hosted_start:hosted_end]

    assert copy.REACH_SPL_TRUST_LINE not in header
    assert copy.REACH_SPL_TRUST_LINE not in byo_body
    assert copy.REACH_SPL_TRUST_LINE in hosted_body


def test_workspace_maps_spl_status_without_red_offline_dot(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/link/")

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

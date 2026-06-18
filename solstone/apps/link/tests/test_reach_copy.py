# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for locked link reach-shell copy constants."""

from __future__ import annotations

import re

from solstone.apps.link import copy

U2_COPY_VALUES = [
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
    copy.REACH_HOME_ADDRESS_LABEL,
    copy.REACH_HOST_ADDRESS_DISCLOSURE,
    copy.REACH_HOST_ADDRESS_PLACEHOLDER,
    copy.REACH_HOST_ADDRESS_APPLY_LABEL,
    copy.REACH_HOST_ADDRESS_CLEAR_LABEL,
    copy.REACH_VPN_CANDIDATE_LABEL,
    copy.REACH_VPN_USE_THIS,
    copy.REACH_SPL_ACTIVE_BODY,
    copy.REACH_SPL_TRUST_LINE,
    copy.REACH_SPL_MANAGE_LABEL,
    copy.REACH_SPL_CONNECTING_NOTE,
    copy.CHECK_AGAIN_LABEL,
    copy.PRIVATE_LINK_DISABLE_CTA,
    copy.PRIVATE_LINK_SETTING_UP,
    copy.PRIVATE_LINK_PORTAL_CTA,
    copy.PRIVATE_LINK_SETUP_SUCCESS,
    copy.PRIVATE_LINK_SETUP_FAILED,
    copy.PRIVATE_LINK_DISABLE_SUCCESS,
    copy.PRIVATE_LINK_DISABLE_FAILED,
    copy.PRIVATE_LINK_NEEDS_REPAIR,
    copy.PRIVATE_LINK_RETRY_CTA,
    *copy.STATUS_SENTENCES.values(),
]


def test_reach_shell_spec_fixed_copy_is_locked() -> None:
    assert copy.STATUS_SENTENCES == {
        "direct_online": "your solstone is reachable on your network.",
        "direct_online_vpn": "your solstone is reachable on your network and over your VPN.",
        "reconnecting": "reconnecting to your solstone...",
        "offline": "can't reach your solstone right now.",
        "lan_unreachable": "your solstone is running, but devices can't reach it to pair yet.",
        "spl_online": "your solstone is reachable from anywhere.",
        "spl_finishing_setup": "finishing setup with solstone private link...",
        "spl_offline": (
            "your solstone isn't reaching the network right now — devices can't "
            "connect from away. on your home wifi they still work."
        ),
        "checking": "checking your solstone...",
    }


def test_reach_shell_corrected_copy_is_locked() -> None:
    assert copy.BRANDLOCK_LINE == "your journal is always private, only yours."
    assert copy.REACH_SELECTOR_TITLE == "how your devices reach your journal"
    assert copy.REACH_SELECTOR_HINT == (
        "your choice — switch anytime. either way, what syncs is end-to-end "
        "encrypted and only your devices can read it."
    )
    assert copy.MODE_BYO_NAME == "your own"
    assert copy.MODE_BYO_DESC == (
        "your devices reach your journal over your own network — same wifi, or "
        "your own VPN. the default."
    )
    assert copy.MODE_BYO_DISCLOSURE == "sol pbc is never in the path"
    assert copy.MODE_HOSTED_NAME == "solstone hosted"
    assert copy.MODE_HOSTED_DESC == (
        "reach your journal from anywhere, through a relay sol pbc runs for you."
    )
    assert copy.MODE_HOSTED_DISCLOSURE == "operated by sol pbc"
    assert copy.MODE_BYO_BODY_NOTE == (
        "your journal stays on this device. your other devices connect straight "
        "to it — nothing routes through sol pbc."
    )
    assert copy.MODE_HOSTED_SETUP_NOTE == (
        "your journal stays on this device; the relay only passes along "
        "encrypted traffic it can't read."
    )
    assert copy.MODE_HOSTED_SETUP_CTA == "set up the relay →"
    assert copy.APP_ONOFF_LABEL == "link"
    assert copy.APP_ONOFF_SUB_BYO == "on — reachable over your own network"
    assert copy.APP_ONOFF_SUB_HOSTED == "on — reachable from anywhere"
    assert copy.REACH_HOST_ADDRESS_DISCLOSURE == "▸ use a different address"
    assert copy.REACH_HOST_ADDRESS_PLACEHOLDER == "192.168.1.44:7657"
    assert copy.REACH_HOST_ADDRESS_APPLY_LABEL == "apply"
    assert copy.REACH_HOST_ADDRESS_CLEAR_LABEL == "clear"
    assert (
        copy.REACH_SPL_ACTIVE_BODY
        == "your devices reach home over the internet, wherever you are."
    )
    assert copy.REACH_SPL_TRUST_LINE == (
        "the connection is end-to-end encrypted — sol pbc and cloudflare can see "
        "that your device and home met, and nothing inside."
    )
    assert (
        copy.REACH_SPL_MANAGE_LABEL
        == "manage solstone private link at services.solstone.app →"
    )
    assert (
        copy.REACH_SPL_CONNECTING_NOTE
        == "your home is connecting. this is usually quick."
    )
    assert copy.CHECK_AGAIN_LABEL == "check again"
    assert copy.PRIVATE_LINK_DISABLE_CTA == "turn off solstone private link"
    assert copy.PRIVATE_LINK_SETTING_UP == "setting up solstone private link…"
    assert copy.PRIVATE_LINK_PORTAL_CTA == "continue to approve →"
    assert (
        copy.PRIVATE_LINK_SETUP_SUCCESS
        == "solstone private link is on. your devices can reach home from anywhere."
    )
    assert (
        copy.PRIVATE_LINK_SETUP_FAILED
        == "couldn't finish setting up solstone private link."
    )
    assert (
        copy.PRIVATE_LINK_DISABLE_SUCCESS
        == "solstone private link is off. devices connect directly again."
    )
    assert (
        copy.PRIVATE_LINK_DISABLE_FAILED
        == "couldn't turn off solstone private link — it's still on. try again."
    )
    assert (
        copy.PRIVATE_LINK_NEEDS_REPAIR
        == "solstone private link needs setting up again."
    )
    assert copy.PRIVATE_LINK_RETRY_CTA == "try again"


def test_reach_shell_copy_stays_in_bounds() -> None:
    banned_terms = (
        "sign in",
        "account",
        "subscribe",
        "upgrade",
        "your services",
        "sol private link",
        "price",
        "$",
        "billing",
        "subscription",
        "invoice",
        "plan",
        "phone",
    )
    acronym_re = re.compile(r"\b(dl|pl|spl)\b")

    for value in [
        *U2_COPY_VALUES,
    ]:
        lowered = value.lower()
        for term in banned_terms:
            assert term not in lowered, value
        assert not acronym_re.search(lowered), value

    spl_values = [
        copy.REACH_SPL_MANAGE_LABEL,
        copy.PRIVATE_LINK_DISABLE_CTA,
    ]
    assert all("solstone private link" in value for value in spl_values)

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for locked link pairing copy constants."""

from __future__ import annotations

from solstone.apps.link import copy


def test_copy_constants_are_locked() -> None:
    assert copy.PAIR_LINK_HOST == "go.solstone.app"
    assert copy.PAIR_LINK_PATH == "/p"
    assert copy.MODAL_TITLE == "pair a device"
    assert copy.STEP_1 == "open the camera on the device you're adding"
    assert copy.STEP_2 == "point it at this code"
    assert copy.STEP_3 == "tap the link to open solstone"
    assert copy.PAIR_NETWORK_LINE == (
        "this device needs to be on your network (or your VPN) to pair. "
        "expires in 5:00."
    )
    assert (
        copy.PAIR_ROTATE_NOTE
        == "this code refreshes on its own — keep this page open while you pair."
    )
    assert copy.DETAILS_DISCLOSURE == "verify this is really your home"
    assert copy.CA_FP_LABEL == "fingerprint"
    assert copy.CA_FP_NOTE == (
        "the device checks this when it scans, so no one on your wifi can "
        "impersonate home."
    )
    assert copy.DEVICE_LABEL_FIELD_LABEL == "name this device"
    assert copy.DEVICE_LABEL_PLACEHOLDER == "e.g. my iPhone"
    assert copy.DEVICE_LABEL_DEFAULT_FORMAT == "device — added {month} {day}"
    assert copy.EXPIRED_BUTTON == "this code expired — show a new one"
    assert copy.WINDOW_CLOSED_BUTTON == "pairing window closed — open a new one"
    assert copy.SUCCESS_HEADING == '"{label}" is now paired with your solstone'
    assert copy.SUCCESS_SUBHEAD == "{short_fp} · paired just now"
    assert copy.SUCCESS_DONE == "done"
    assert copy.PAIR_ERROR_BODY == (
        "can't start pairing — your solstone isn't reachable on a network address yet."
    )
    assert copy.SUCCESS_VERIFY_NOTE == (
        "check the device you just paired — this fingerprint should match what it "
        "shows. didn't do this?"
    )
    assert copy.SUCCESS_VERIFY_NOTE_ANYWHERE == (
        "this device can now reach home from anywhere. check it now — "
        "this fingerprint should match what it shows. didn't do this?"
    )
    assert copy.SUCCESS_REMOVE_LABEL == "that wasn't me — remove"
    assert copy.HERO_TITLE == "let's connect a device"
    assert copy.HERO_BODY == (
        "your journal lives here, on this device. to read it from your phone or "
        "laptop, that device needs a way to reach it. right now it can be reached "
        "on your home network."
    )
    assert copy.HERO_HOW_REACH_LABEL == "how reach works ▸"
    assert copy.RECENT_NETWORK_LABEL_ANYWHERE == "from anywhere"

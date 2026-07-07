# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for rendered link pair-flow copy."""

from __future__ import annotations

import re

from solstone.apps.network import copy

MODAL_COPY_NAMES = (
    "MODAL_TITLE",
    "STEP_1",
    "STEP_2",
    "STEP_3",
    "PAIR_NETWORK_LINE",
    "DEVICE_LABEL_FIELD_LABEL",
    "DETAILS_DISCLOSURE",
    "CA_FP_LABEL",
    "CA_FP_NOTE",
    "DEVICE_LABEL_PLACEHOLDER",
    "EXPIRED_BUTTON",
    "PAIR_ERROR_BODY",
    "SUCCESS_HEADING",
    "SUCCESS_SUBHEAD",
    "SUCCESS_DONE",
    "SUCCESS_VERIFY_NOTE",
    "SUCCESS_REMOVE_LABEL",
)


def _link_copy(env) -> dict[str, object]:
    response = env.client.get("/app/network/api/state")
    assert response.status_code == 200
    return response.get_json()["link_copy"]


def test_workspace_renders_pair_flow_copy_and_qr_script(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    payload = _link_copy(env)

    for name in MODAL_COPY_NAMES:
        assert payload[name] == getattr(copy, name)

    assert "QR rendering lib not bundled yet" not in body
    assert "link-pair-generate" not in body
    assert "Waiting for phone" not in body
    assert "data.pair_url" not in body
    assert "pair_url" not in body
    assert 'src="/static/pairing-qr.js"' in body
    assert 'src="/app/network/static/network.js"' in body
    assert re.search(r'<div id="link-pair-success"[^>]{0,200}\bhidden\b', body)

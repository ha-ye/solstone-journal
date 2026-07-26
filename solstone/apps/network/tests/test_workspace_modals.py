# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for link workspace modal visibility."""

from __future__ import annotations

import re


def _rule_body(body: str, start_marker: str) -> str:
    start = body.index(start_marker)
    end = body.index("}", start)
    return body[start:end]


def test_workspace_modals_are_hidden_by_attribute_and_css(link_env):
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="link-pair-modal"' in body
    assert re.search(r'<div id="link-pair-modal"[^>]{0,200}\bhidden\b', body)
    assert 'id="link-unpair-modal"' in body
    assert re.search(r'<div id="link-unpair-modal"[^>]{0,200}\bhidden\b', body)
    assert ".link-modal[hidden]" in body
    assert body.count(".link-modal-box {") == 2

    modal_rule = _rule_body(body, ".link-modal { position: fixed;")
    padding_start = modal_rule.index("padding:")
    padding_end = modal_rule.index(";", padding_start)
    padding_declaration = modal_rule[padding_start:padding_end]
    assert "--facet-bar-height" in padding_declaration
    assert "--app-bar-height" in padding_declaration

    full_modal_box_rule = _rule_body(body, ".link-modal-box { background:")
    assert "max-height: 100%;" in full_modal_box_rule
    assert "overflow-y: auto;" in full_modal_box_rule

    assert ".link-modal-box { max-width: 620px; }" in body

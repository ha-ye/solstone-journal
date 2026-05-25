# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from solstone.apps.home.needs_you import (
    NeedsYouItem,
    _normalize_route_payload,
    classify_needs_you,
)


def test_classify_needs_you_locked_shape_and_order():
    attention = {"placeholder_text": "Pipeline needs review"}
    pulse_needs = ["Review the launch checklist"]
    todos = [{"text": "Send the partner update"}]

    items = classify_needs_you(attention, pulse_needs, todos)

    assert [item.text for item in items] == [
        "Pipeline needs review",
        "Review the launch checklist",
        "Send the partner update",
    ]
    assert [field.name for field in fields(NeedsYouItem)] == [
        "text",
        "kind",
        "payload",
    ]
    for item in items:
        data = item.to_dict()
        assert list(data) == ["text", "kind", "payload"]
        assert data["kind"] in ["chat", "confirm", "route"]


def test_classify_needs_you_warns_and_omits_malformed(caplog):
    caplog.set_level("WARNING", logger="solstone.apps.home.needs_you")

    items = classify_needs_you(
        None,
        [None, ""],
        [{"missing_text_field": 1}],
    )

    assert items == []
    assert any(
        "omitting malformed needs-you" in record.message for record in caplog.records
    )


def test_classify_needs_you_route_same_origin_only(caplog):
    caplog.set_level("WARNING", logger="solstone.apps.home.needs_you")

    route_items = classify_needs_you(
        None,
        [
            {
                "text": "Open the settings page",
                "kind": "route",
                "payload": {"href": "/app/settings"},
            }
        ],
        [],
    )

    assert route_items == [
        NeedsYouItem(
            text="Open the settings page",
            kind="route",
            payload={"href": "/app/settings"},
        )
    ]
    assert _normalize_route_payload({"href": "/app/foo"}) == {"href": "/app/foo"}
    assert _normalize_route_payload({"href": "//evil.com/foo"}) is None
    assert _normalize_route_payload({"href": "https://evil.com"}) is None
    assert any("off-origin href" in record.message for record in caplog.records)


def test_classify_needs_you_folds_confirm_to_chat():
    items = classify_needs_you(
        None,
        [{"text": "Confirm the next step", "kind": "confirm", "payload": {}}],
        [],
    )

    assert items == [
        NeedsYouItem(
            text="Confirm the next step",
            kind="chat",
            payload={"prompt": "let's dig into Confirm the next step"},
        )
    ]


def test_classify_needs_you_todos_default_to_chat_with_context_prompt():
    items = classify_needs_you(None, [], [{"text": "Draft the launch note"}])

    assert items == [
        NeedsYouItem(
            text="Draft the launch note",
            kind="chat",
            payload={"prompt": "what's the context on: Draft the launch note"},
        )
    ]


def test_unknown_kind_renders_inert():
    workspace = (
        Path(__file__).resolve().parents[1]
        / "solstone"
        / "apps"
        / "home"
        / "workspace.html"
    ).read_text(encoding="utf-8")

    dispatch_start = workspace.index("function dispatchNeedsYouItem(item)")
    init_start = workspace.index("function initHome()", dispatch_start)
    dispatch_body = workspace[dispatch_start:init_start]

    assert "if (item.kind === 'chat')" in dispatch_body
    assert "if (item.kind === 'route')" in dispatch_body
    assert "if (item.kind === 'confirm')" in dispatch_body
    assert "unsupported confirm needs-you item" in dispatch_body
    assert "else" not in dispatch_body

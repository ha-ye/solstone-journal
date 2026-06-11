# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from solstone.apps.home.needs_you import (
    DISABLED_EMPTY_PROMPT_REASON,
    DISABLED_INVALID_ROUTE_REASON,
    NeedsYouItem,
    _chat_item,
    _normalize_route_payload,
    classify_needs_you,
)


def test_classify_needs_you_locked_shape_and_order():
    attention = {"placeholder_text": "Pipeline needs review"}
    pulse_needs = ["Review the launch checklist"]
    items = classify_needs_you(attention, pulse_needs)

    assert [item.text for item in items] == [
        "Pipeline needs review",
        "Review the launch checklist",
    ]
    assert [field.name for field in fields(NeedsYouItem)] == [
        "text",
        "kind",
        "payload",
        "disabled",
        "reason",
    ]
    for item in items:
        data = item.to_dict()
        assert list(data) == ["text", "kind", "payload", "disabled", "reason"]
        assert data["kind"] in ["chat", "confirm", "route"]
        assert data["disabled"] is False
        assert data["reason"] == ""


def test_classify_needs_you_warns_and_omits_malformed(caplog):
    caplog.set_level("WARNING", logger="solstone.apps.home.needs_you")

    items = classify_needs_you(
        None,
        [None, ""],
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


def test_classify_needs_you_invalid_route_returns_disabled_item():
    items = classify_needs_you(
        None,
        [
            {
                "text": "Open the offsite link",
                "kind": "route",
                "payload": {"href": "https://evil.com"},
            }
        ],
    )

    assert items == [
        NeedsYouItem(
            text="Open the offsite link",
            kind="route",
            payload={},
            disabled=True,
            reason=DISABLED_INVALID_ROUTE_REASON,
        )
    ]


def test_chat_item_with_empty_prompt_returns_disabled_item():
    assert _chat_item("Review this", " ") == NeedsYouItem(
        text="Review this",
        kind="chat",
        payload={},
        disabled=True,
        reason=DISABLED_EMPTY_PROMPT_REASON,
    )


def test_classify_needs_you_folds_confirm_to_chat():
    items = classify_needs_you(
        None,
        [{"text": "Confirm the next step", "kind": "confirm", "payload": {}}],
    )

    assert items == [
        NeedsYouItem(
            text="Confirm the next step",
            kind="chat",
            payload={"prompt": "let's dig into Confirm the next step"},
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


def test_disabled_items_render_noninteractive():
    workspace = (
        Path(__file__).resolve().parents[1]
        / "solstone"
        / "apps"
        / "home"
        / "workspace.html"
    ).read_text(encoding="utf-8")

    assert "{% if item.disabled %}" in workspace
    assert "pulse-needs-item-disabled" in workspace
    assert "pulse-needs-reason" in workspace
    disabled_branch_start = workspace.index("{% if item.disabled %}")
    disabled_branch_end = workspace.index("{% else %}", disabled_branch_start)
    disabled_branch = workspace[disabled_branch_start:disabled_branch_end]
    assert 'role="button"' not in disabled_branch
    assert "tabindex" not in disabled_branch
    assert "data-needs-you-item" not in disabled_branch
    assert "if (item.disabled) return;" in workspace

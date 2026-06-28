# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from pathlib import Path

from solstone.apps.home.needs_you import (
    DISABLED_EMPTY_PROMPT_REASON,
    DISABLED_INVALID_ROUTE_REASON,
    NeedsYouItem,
    _chat_item,
    _normalize_route_payload,
    classify_needs_you,
    format_degraded_capture_line,
)


def _june_22_ms() -> float:
    return datetime(2026, 6, 22, 12, 0, 0).timestamp() * 1000


def _degraded_capture(
    *,
    name: str = "fedora",
    active_count=79,
    first_ts=None,
    include_rejection: bool = True,
) -> dict:
    observer = {
        "name": name,
        "status": "degraded",
    }
    if include_rejection:
        observer["ingest_rejection"] = {
            "reason_code": "ingest_contract_invalid",
            "active_count": active_count,
            "first_ts": _june_22_ms() if first_ts is None else first_ts,
            "latest_ts": _june_22_ms(),
            "summary": "/tmp/private/screen.jsonl:2: value is invalid",
            "stream": "fedora",
            "version": "0.3.1",
            "segment": "20260622/120000_300",
        }
    return {"status": "degraded", "observers": [observer]}


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


def test_format_degraded_capture_line_single_named_full():
    line = format_degraded_capture_line(_degraded_capture())

    assert line == "fedora isn't reaching your journal — 79 rejected since jun 22"
    assert "segment" not in line
    assert "screen.jsonl" not in line
    assert "/tmp/private" not in line


def test_format_degraded_capture_line_multiple_combines_first_and_count():
    capture = _degraded_capture()
    capture["observers"].append(
        {
            "name": "phone",
            "status": "degraded",
            "ingest_rejection": {
                "reason_code": "ingest_contract_invalid",
                "active_count": 2,
                "first_ts": _june_22_ms(),
                "latest_ts": _june_22_ms(),
                "summary": "screen.jsonl:2: value is invalid",
                "stream": "phone",
                "version": None,
            },
        }
    )

    assert (
        format_degraded_capture_line(capture)
        == "fedora isn't reaching your journal — 79 rejected since jun 22, and 1 more"
    )


def test_format_degraded_capture_line_named_without_required_count_or_date():
    missing_ts = _degraded_capture()
    del missing_ts["observers"][0]["ingest_rejection"]["first_ts"]
    assert (
        format_degraded_capture_line(missing_ts) == "fedora isn't reaching your journal"
    )
    assert (
        format_degraded_capture_line(_degraded_capture(active_count=None))
        == "fedora isn't reaching your journal"
    )


def test_format_degraded_capture_line_fallbacks_and_non_degraded():
    assert (
        format_degraded_capture_line(_degraded_capture(include_rejection=False))
        == "an observer isn't reaching your journal"
    )
    assert (
        format_degraded_capture_line({"status": "degraded", "observers": []})
        == "an observer isn't reaching your journal"
    )
    assert format_degraded_capture_line({"status": "active", "observers": []}) is None


def test_classify_needs_you_no_longer_emits_capture_route():
    items = classify_needs_you({"placeholder_text": "x"}, ["y"])

    assert all(item.payload != {"href": "/app/health"} for item in items)


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

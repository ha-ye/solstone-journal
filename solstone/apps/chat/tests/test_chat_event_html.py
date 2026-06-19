# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

CHAT_EVENT = Path("solstone/apps/chat/_chat_event.html")
WORKSPACE = Path("solstone/apps/chat/workspace.html")
APP_TEMPLATE = Path("solstone/convey/templates/app.html")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _js_function_block(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    brace_start = source.index("{", start)
    depth = 0
    for index in range(brace_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} block not found")


def test_chat_event_partial_renders_queued_card_and_dispatch_origin():
    source = _read(CHAT_EVENT)

    assert '{% elif ev.kind == "talent_queued" %}' in source
    assert "chat-talent-card--queued" in source
    assert "chat_copy.CHAT_TALENT_QUEUED_LABEL" in source
    assert "{% if ev.task %}" in source
    assert "chat-talent-card-task" in source
    assert 'chat_copy.talent_label_for(status="queued"' not in source

    assert "{% if ev.origin %}" in source
    assert 'class="chat-dispatch-origin"' in source
    assert 'data-dispatch-logical-id="{{ ev.origin.logical_use_id }}"' in source
    assert "chat_copy.CHAT_DISPATCH_ORIGIN_PREFIX" in source
    assert "ev.origin.ask" in source

    assert "sol_message_origins.get(loop.index0)" in source
    assert "chat-origin-tag" in source
    assert "chat-dispatch-origin" in source


def test_workspace_live_rendering_keeps_dispatch_origin_and_queued_paths_distinct():
    source = _read(WORKSPACE)

    assert "'talent_queued'," in source
    assert "function buildQueuedTalentCard(event)" in source
    assert "window.solChatCopy.CHAT_TALENT_QUEUED_LABEL" in source
    assert "talentLabel(event.name, 'queued')" not in source
    assert "target.dataset.talentStatus === 'queued'" in source

    assert "function removeQueuedTalentCard(useId)" in source
    assert 'data-talent-status="queued"' in source
    assert (
        "['talent_spawned', 'talent_finished', 'talent_errored'].includes(kind)"
        in source
    )

    assert (
        "dispatchOrigin: kind === 'sol_message' ? (msg.origin || null) : null" in source
    )
    assert "function renderDispatchOriginTag(dispatchOrigin)" in source
    assert "event.dispatchOrigin" in source
    assert "renderDispatchOriginTag(event.dispatchOrigin)" in source
    assert "renderOriginTag(event.dispatchOrigin)" not in source
    assert "renderOriginTag(event.origin)" in source
    assert 'data-use-id="{{ ev.use_id }}"' in source
    assert "item.dataset.useId = event.use_id" in source


def test_app_bar_jobs_indicator_and_composer_state_are_source_wired():
    source = _read(APP_TEMPLATE)

    pending_block = _js_function_block(source, "setPendingState")
    assert "pendingSend = !!active;" in pending_block
    assert "input.disabled" not in pending_block
    assert "sendBtn.disabled" not in pending_block

    disable_block = _js_function_block(source, "disableComposer")
    assert "pendingSend = true;" in disable_block
    assert "input.disabled = true;" in disable_block
    assert "sendBtn.disabled = true;" in disable_block
    assert "disableComposer();" in source

    assert "setPendingState(true);" in source
    assert "setPendingState(false);" in source
    assert "if (!input || !sendBtn || pendingSend) return;" in source
    assert "if (pendingSend) return;" in source

    assert "const queuedJobs = new Map();" in source
    assert "function renderJobsIndicator()" in source
    assert "runningCount + queuedJobs.size" in source
    assert "window.solChatCopy.CHAT_JOBS_INDICATOR_SINGULAR" in source
    assert "window.solChatCopy.CHAT_JOBS_INDICATOR_PLURAL_FORMAT" in source
    assert "eventName === 'talent_queued'" in source
    assert "queuedJobs.set(queuedUseId" in source
    assert source.count("queuedJobs.delete(String(msg.use_id || ''));") == 3
    assert "data.queued_talents" in source

    assert "function setQueueDepth" not in source
    assert "eventName === 'chat_queue_depth'" not in source

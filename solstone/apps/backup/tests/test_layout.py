# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Layout-source tests for the backup app."""

from __future__ import annotations

import re
from pathlib import Path

_MEDIA_OPEN = re.compile(r"@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)\s*\{")
_CSS_RULE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}", re.DOTALL)
_LEFT_CLEARANCE = re.compile(
    r"\b(?:padding-left|margin-left)\s*:\s*[^;]*--menu-bar-width[^;]*;",
    re.DOTALL,
)
_BOTTOM_CLEARANCE = re.compile(
    r"\b(?:padding-bottom|margin-bottom)\s*:\s*[^;]*--app-bar-height[^;]*;",
    re.DOTALL,
)


def _backup_css() -> str:
    return Path("solstone/apps/backup/static/backup.css").read_text(encoding="utf-8")


def _backup_js() -> str:
    return Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")


def _media_spans(css: str) -> list[tuple[int, int, int, str]]:
    spans: list[tuple[int, int, int, str]] = []
    for match in _MEDIA_OPEN.finditer(css):
        depth = 1
        index = match.end()
        while index < len(css) and depth > 0:
            char = css[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        if depth != 0:
            raise AssertionError("unterminated @media block in backup.css")
        spans.append(
            (match.start(), index, int(match.group(1)), css[match.end() : index - 1])
        )
    return spans


def _narrow_media_blocks(css: str) -> list[str]:
    return [body for _start, _end, width, body in _media_spans(css) if width <= 768]


def _selector_root_tokens(selector: str) -> set[str]:
    tokens: set[str] = set()
    if re.search(r"(?<![\w-])\.backup-shell(?![\w-])", selector):
        tokens.add("backup-shell")
    if re.search(r"\[data-backup-root(?:[\]\s=~|^$*])", selector):
        tokens.add("data-backup-root")
    return tokens


def _clearance_tokens(blocks: list[str], declaration: re.Pattern[str]) -> set[str]:
    tokens: set[str] = set()
    for block in blocks:
        for match in _CSS_RULE.finditer(block):
            selector_tokens = _selector_root_tokens(match.group("selector"))
            if selector_tokens and declaration.search(match.group("body")):
                tokens.update(selector_tokens)
    return tokens


def _class_token_present(html: str, class_name: str) -> bool:
    return any(
        class_name in class_attr.split()
        for class_attr in re.findall(r'class="([^"]*)"', html)
    )


def _root_token_present(html: str, token: str) -> bool:
    if token.startswith("data-"):
        return bool(re.search(rf"\s{re.escape(token)}(?:[=\s>]|$)", html))
    return _class_token_present(html, token)


def _rendered_backup_html(backup_env) -> str:
    response = backup_env().client.get("/app/backup/workspace")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_narrow_rules_bound_to_rendered_surface(backup_env) -> None:
    css = _backup_css()
    html = _rendered_backup_html(backup_env)
    blocks = _narrow_media_blocks(css)

    left_tokens = _clearance_tokens(blocks, _LEFT_CLEARANCE)
    assert left_tokens, "narrow Backup root rule must reserve menu-bar width"

    bottom_tokens = _clearance_tokens(blocks, _BOTTOM_CLEARANCE)
    assert bottom_tokens, "narrow Backup root rule must reserve app-bar height"

    for token in left_tokens | bottom_tokens:
        assert _root_token_present(html, token), f"{token} selector not rendered"


def test_backup_panels_and_states_render(backup_env) -> None:
    html = _rendered_backup_html(backup_env)

    assert '<link rel="stylesheet" href="/app/backup/static/backup.css">' in html
    assert _class_token_present(html, "backup-shell")
    for name in (
        "intro",
        "educate",
        "display",
        "confirm",
        "destination",
        "management",
        "restore",
    ):
        assert f'data-backup-panel="{name}"' in html
    for marker in (
        "data-empty-state",
        "data-loading-state",
        "data-enabling-state",
        "data-error-state",
        "data-operation-banner",
        "data-operation-phase",
        "data-recovery-grid",
        "data-confirm-input",
        "data-destination-form",
        "data-last-backup",
        "data-last-prune",
        "data-storage-placeholder",
        "data-snapshot-placeholder",
        "data-retention-form",
        "data-restore-form",
        "data-restore-status",
        "data-offload-section",
        "data-offload-state",
        "data-offload-readiness",
        "data-offload-enable-form",
        "data-offload-budget-input",
        "data-offload-floor-input",
        "data-offload-config-status",
        "data-offload-summary",
        "data-offload-device-free",
        "data-offload-device-total",
        "data-offload-raw-bytes",
        "data-offload-backup-only-bytes",
        "data-offload-last-run",
        "data-offload-last-verify",
        "data-offload-last-restore",
        "data-offload-days",
        "data-offload-day-template",
        "data-offload-day-value",
        "data-offload-day-raw-bytes",
        "data-offload-day-backup-only-bytes",
        "data-offload-day-restore",
        "data-offload-disable",
        "data-offload-unavailable",
        "data-offload-stall-reason",
        "data-teardown-gate",
        "data-teardown-stakes",
        "data-teardown-input",
        "data-teardown-status",
    ):
        assert marker in html
    for hook in (
        'data-copy="brand_lock"',
        'data-copy="intro.title"',
        'data-copy-list="intro.bullets"',
        'data-copy="destination.modes.hosted.cta"',
        'data-copy-href="hosted.manage_url"',
        'data-copy-aria-label="management.status_labels.destination"',
        "data-retention-grid",
        'data-copy="offload.title"',
        'data-copy="offload.stakes"',
        'data-copy="offload.disable_note"',
        'data-copy="management.teardown_confirm_prompt"',
        'data-copy="management.teardown_restore_first_action"',
        'data-action="teardown-open"',
        'data-action="teardown-confirm"',
        'data-action="teardown-restore-first"',
    ):
        assert hook in html

    css = _backup_css()
    narrow_css = "\n".join(_narrow_media_blocks(css))
    normalized = re.sub(r"\s*:\s*", ":", narrow_css.lower())
    for forbidden in (
        "display:none",
        "text-overflow:ellipsis",
        "visibility:hidden",
        "font-size:0",
    ):
        assert forbidden not in normalized


def test_offload_js_source_contracts() -> None:
    js = _backup_js()

    assert "const BYTES_PER_GB = 1000000000;" in js
    assert "return Math.round(parsed * BYTES_PER_GB);" in js
    assert "budget_bytes: gbToBytes(budgetField.value)" in js
    assert "floor_bytes: gbToBytes(floorField.value)" in js
    assert "budget_bytes: budgetField.value" not in js
    assert "floor_bytes: floorField.value" not in js
    assert "await startOperation('/app/backup/offload/restore', { day });" in js
    assert re.findall(r"postJson\('(/app/backup/offload/disable)'", js) == [
        "/app/backup/offload/disable"
    ]
    assert "delete next.operation;" in js
    assert "applyPayload(await postJson('/app/backup/offload" not in js
    assert "kind === 'offload_restore'" in js
    assert "applyCopy(clone, copy);" in js

    offload_error = js[
        js.index("function offloadActionError(err)") : js.index(
            "function maybeOpenPortal"
        )
    ]
    assert "operationLabels[reason]" in offload_error
    assert "offloadCopy.action_error" in offload_error
    assert "destinationLabels" not in offload_error
    assert "error_intro" not in offload_error
    offload_catch = js[
        js.index("if (action && action.startsWith('offload-'))") : js.index(
            "} else {\n          showError('[data-operation-error]'"
        )
    ]
    assert "offloadActionError(err)" in offload_catch
    assert "showError" not in offload_catch

    budget_gb = 37
    floor_gb = 23
    assert budget_gb != floor_gb


def test_offload_js_validates_payload_shape_before_ready_state() -> None:
    js = _backup_js()

    assert "function validOffloadPayload(payload)" in js
    assert "payload.offload &&" in js
    assert "typeof payload.offload === 'object'" in js
    assert "!Array.isArray(payload.offload)" in js
    assert "Array.isArray(payload.days)" in js
    assert "malformed backup offload status payload" in js
    guard_call = "if (!validOffloadPayload(payload))"
    assert guard_call in js
    assert js.index(guard_call) < js.index("offloadState = { status: 'ready'")


def test_teardown_js_source_contracts() -> None:
    js = _backup_js()

    assert "function backupOnlyTotalsForTeardown()" in js
    assert "if (offloadState.status !== 'ready') return null;" in js
    assert "const backupOnly = payload.backup_only;" in js
    assert "typeof backupOnly !== 'object'" in js
    assert "Array.isArray(backupOnly)" in js
    backup_only_totals = js[
        js.index("function backupOnlyTotalsForTeardown()") : js.index(
            "function renderTeardownGate"
        )
    ]
    assert "backupOnly.degraded !== false" in backup_only_totals
    assert "const days = backupOnly.total_days;" in js
    assert "const bytes = backupOnly.total_bytes;" in js
    assert "typeof days !== 'number'" in js
    assert "typeof bytes !== 'number'" in js
    assert "!Number.isFinite(days)" in js
    assert "!Number.isFinite(bytes)" in js
    assert "return { days, size: formatBytes(bytes) };" in js
    assert "if (totals.days > 0)" not in js
    assert re.findall(r"startOperation\('(/app/backup/teardown)'", js) == [
        "/app/backup/teardown"
    ]
    assert "await startOperation('/app/backup/offload/restore', { all: true });" in js
    assert (
        "button.disabled = teardownInputValue() !== teardownConfirmPhrase();" not in js
    )

    confirm_satisfied = js[
        js.index("function teardownConfirmSatisfied()") : js.index(
            "function updateTeardownConfirmState"
        )
    ]
    assert "const phrase = teardownConfirmPhrase();" in confirm_satisfied
    assert "phrase !== ''" in confirm_satisfied
    assert "teardownInputValue() === phrase" in confirm_satisfied

    render_gate = js[
        js.index("function renderTeardownGate(totals)") : js.index(
            "function showTeardownGate"
        )
    ]
    assert "if (totals === null)" in render_gate
    assert "managementCopy.teardown_gate_unavailable_lead" in render_gate
    assert "managementCopy.teardown_gate_lead" in render_gate

    open_gate = js[
        js.index("async function openTeardownGate()") : js.index(
            "function offloadConfigBody"
        )
    ]
    assert "await refreshOffloadStatus();" in open_gate
    assert "const totals = backupOnlyTotalsForTeardown();" in open_gate
    assert "renderTeardownGate(null);" in open_gate
    assert "renderTeardownGate(totals);" in open_gate
    assert "renderOffloadUnavailable();" in open_gate

    confirm_action = js[
        js.index("if (action === 'teardown-confirm')") : js.index(
            "if (action === 'teardown-restore-first')"
        )
    ]
    guard = "if (!teardownConfirmSatisfied()) return;"
    disarm = "disarmTeardownConfirm();"
    target = "await startOperation('/app/backup/teardown');"
    assert guard in confirm_action
    assert disarm in confirm_action
    assert target in confirm_action
    guard_position = js.index(guard)
    disarm_position = js.index(disarm, guard_position)
    target_position = js.index(target)
    assert guard_position < disarm_position < target_position

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Observable trust-core behavior in the Suggestions workspace."""

from __future__ import annotations

from pathlib import Path

WORKSPACE = Path("solstone/apps/curation/workspace.html")


def _workspace() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def test_ambiguities_render_origin_candidates_and_one_tap_resolution() -> None:
    html = _workspace()

    assert "payload.ambiguity_items || []" in html
    assert "item?.evidence?.origins || []" in html
    assert "item?.evidence?.ranked_candidates || []" in html
    assert 'data-action="ambiguity-resolve"' in html
    assert "/app/entities/api/ambiguities/${encodeURIComponent" in html
    assert ".then(() => removeItem(item))" in html
    assert "showError(item, err.serverMessage || err.message)" in html


def test_merge_outcomes_persist_with_independent_retryable_undo() -> None:
    html = _workspace()

    assert "data-curation-outcomes" in html
    assert "appendUndoOutcome(result.undo" in html
    assert "appendUndoOutcome(data.undo" in html
    assert "data-action = 'merge-undo'" not in html
    assert "button.dataset.action = 'merge-undo'" in html
    assert (
        "/app/entities/api/merge/${encodeURIComponent(button.dataset.mergeId)}/undo"
        in html
    )
    assert "button.remove();" in html
    assert "setBusy(button, false);" in html
    assert "await refreshCurationState();" in html
    assert "renderState(await window.apiJson('/app/curation/api/state'), true)" in html


def test_removing_final_item_does_not_reload_away_undo_outcomes() -> None:
    html = _workspace()
    start = html.index("function removeItem(item)")
    end = html.index("function appendUndoOutcome", start)

    assert "window.location.reload" not in html[start:end]
    assert "refreshToolbar();" in html[start:end]


def test_repair_required_disables_replay_and_shows_remediation() -> None:
    html = _workspace()

    assert "err?.payload?.operation_state === 'repair_required'" in html
    assert "err.payload.safe_remediation" in html
    assert "item?.querySelectorAll('button, input')" in html
    assert "if (!repairRequired(err)) setBusy(button, false);" in html

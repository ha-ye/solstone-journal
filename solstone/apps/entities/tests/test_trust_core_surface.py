# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Observable trust-core behavior in the Entities workspace."""

from __future__ import annotations

from pathlib import Path

WORKSPACE = Path("solstone/apps/entities/workspace.html")


def _workspace() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def test_detail_merge_previews_before_confirmed_commit() -> None:
    html = _workspace()

    assert 'id="journal-detail-merge-target"' in html
    assert "candidate.id !== entity.id && candidate.blocked !== true" in html
    assert "previewJournalEntityMerge(entity.id, select.value)" in html
    assert (
        "body: JSON.stringify({source_slug: sourceId, target_slug: targetId})" in html
    )
    assert "renderJournalEntityMergePreview" in html
    assert "window.confirm(prompt)" in html
    assert "commit: true" in html


def test_detail_surfaces_suggestions_history_restore_and_recorded_undo() -> None:
    html = _workspace()

    assert "/app/entities/api/merge-candidates?status=open" in html
    assert "/history`)" in html
    assert "row.source_slug === entityId || row.target_slug === entityId" in html
    assert "event.kind === 'merge' && event.merge_state === 'open'" in html
    assert "event.identity_before" in html
    assert "event.identity_after" in html
    assert "ENT_COPY.ENT_TRUST_HISTORY_CHANGE" in html
    assert "event.restore_available && event.version_id" in html
    assert "restoreJournalEntityVersion(entityId, event.version_id" in html
    assert "/restore`" in html
    assert "/merge/${encodeURIComponent(mergeId)}/undo" in html


def test_merge_undo_survives_detail_refresh_and_failure_keeps_action() -> None:
    html = _workspace()

    assert "let pendingJournalMergeUndo = null;" in html
    assert "pendingJournalMergeUndo = result.undo || null;" in html
    assert "showJournalMergeUndo(pendingJournalMergeUndo);" in html
    assert "button.disabled = false;" in html
    assert "pendingJournalMergeUndo = null;" in html


def test_repair_required_surfaces_remediation_without_reenabling_action() -> None:
    html = _workspace()

    assert "err?.payload?.operation_state === 'repair_required'" in html
    assert "err.payload.safe_remediation" in html
    assert "if (!entityTrustRepairRequired(err)) button.disabled = false;" in html

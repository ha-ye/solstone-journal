# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODAL_LAYER_JS = REPO_ROOT / "solstone" / "convey" / "static" / "modal_layer.js"
HARNESS_JS = REPO_ROOT / "tests" / "js" / "modal_layer_harness.js"


@pytest.mark.parametrize(
    "case_name",
    [
        "visibility_shapes",
        "activation_deactivation",
        "inert_restoration",
        "initial_focus_skips_invalid",
        "tab_wraps_forward_at_last_candidate",
        "tab_wraps_reverse_at_first_candidate",
        "tab_from_outside_enters_dialog",
        "tab_without_candidates_focuses_dialog",
        "authored_dialog_tabindex_preserved",
        "interior_tab_stays_native",
        "boundary_tab_blocks_downstream_trap",
        "outside_focus_redirect",
        "opener_restoration_after_sync_focus",
        "repeated_workspace_mounted_idempotent",
        "workspace_removal_restores_state",
        "positioned_dialog_does_not_mark_positioned_ancestor",
        "detached_inerted_element_restores_before_reattach",
    ],
)
def test_modal_layer_behavior(case_name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    result = subprocess.run(
        [node, str(HARNESS_JS), str(MODAL_LAYER_JS), case_name],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr

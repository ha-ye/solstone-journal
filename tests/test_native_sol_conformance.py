# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from copy import deepcopy

from scripts import check_native_sol_conformance as conformance


def test_native_sol_conformance_self_test_detects_manifest_route_mismatch() -> None:
    manifest = deepcopy(conformance.load_manifest())
    for entry in manifest["entries"]:
        if entry["operation_id"] == "activities.list":
            entry["route"] = "/app/activities/api/day/{day}/wrong"
            break

    errors = conformance.check_conformance(manifest=manifest)

    assert any("activities.list" in error and "route" in error for error in errors)

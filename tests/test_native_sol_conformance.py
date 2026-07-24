# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from scripts import check_native_sol_conformance as conformance
from scripts.build_native_sol_inventory import AuthorityEntry


def test_native_sol_conformance_self_test_detects_authority_route_mismatch() -> None:
    authorities = [
        AuthorityEntry(
            authority=conformance.REPO_ROOT
            / "solstone/apps/activities/native/authority.toml",
            source=conformance.REPO_ROOT / "solstone/apps/activities/native/command.rs",
            module="solstone_apps_activities_native_command_rs",
            surface="sol-call",
            path=("activities", "list"),
            kind="command",
            help="List activity records for one day or an inclusive day range.",
            params=[],
            operation_id="activities.list",
            entry_type="http",
            method="GET",
            route="/app/activities/api/day/{day}/wrong",
            contract_operation_id="activities.list",
            handler="list",
        )
    ]

    errors = conformance.check_conformance(authorities=authorities)

    assert any("activities.list" in error and "route" in error for error in errors)

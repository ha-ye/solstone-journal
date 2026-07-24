# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import json
from pathlib import Path

import scripts.build_native_sol_inventory as inventory


def write_authority(root: Path, app: str, operation_id: str) -> None:
    native = root / "solstone" / "apps" / app / "native"
    native.mkdir(parents=True)
    (native / "command.rs").write_text(
        "// SPDX-License-Identifier: AGPL-3.0-only\n"
        "// Copyright (c) 2026 sol pbc\n\n"
        "pub fn fixture_handler() {}\n"
    )
    (native / "authority.toml").write_text(
        'schema = "native-sol-authority-v1"\n'
        'source = "command.rs"\n\n'
        "[[entries]]\n"
        f'path = ["{app}", "ping"]\n'
        'kind = "command"\n'
        'help = "Synthetic native sol inventory fixture."\n'
        "params = []\n"
        f'operation_id = "{operation_id}"\n'
        'entry_type = "local"\n'
        'handler = "fixture_handler"\n'
    )


def test_inventory_discovery_uses_real_adjacency_without_central_list(
    tmp_path: Path,
) -> None:
    write_authority(tmp_path, "fakeapp", "fakeapp.ping")
    write_authority(tmp_path, "_private", "private.ping")

    entries = inventory.discover(tmp_path)

    assert [entry.path for entry in entries] == [("fakeapp", "ping")]
    rendered = inventory.render(
        entries,
        tmp_path
        / "core"
        / "crates"
        / "solstone-core-sol-client"
        / "src"
        / "generated"
        / "inventory.rs",
    )
    assert 'path: &["fakeapp", "ping"]' in rendered
    assert "fakeapp.ping" in rendered
    assert "_private" not in rendered


def entry(
    tmp_path: Path,
    *,
    path: tuple[str, ...],
    operation_id: str,
    entry_type: str,
    method: str | None = None,
    route: str | None = None,
    contract_operation_id: str | None = None,
) -> inventory.AuthorityEntry:
    return inventory.AuthorityEntry(
        authority=tmp_path / "authority.toml",
        source=tmp_path / "command.rs",
        module="fixture",
        surface="sol-call",
        path=path,
        kind="command",
        help=f"Fixture {'.'.join(path)}.",
        params=[],
        operation_id=operation_id,
        entry_type=entry_type,
        method=method,
        route=route,
        contract_operation_id=contract_operation_id,
        handler="fixture_handler",
    )


def write_oracle(tmp_path: Path, paths: list[tuple[str, ...]]) -> Path:
    oracle = tmp_path / "oracle.json"
    oracle.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "path": list(path),
                        "kind": "command",
                        "help": f"Fixture {'.'.join(path)}.",
                        "params": [],
                    }
                    for path in paths
                ]
            }
        )
    )
    return oracle


def test_complete_partition_accepts_synthetic_final_shape(tmp_path: Path) -> None:
    oracle = write_oracle(
        tmp_path,
        [
            ("body", "status"),
            ("identity",),
            ("navigate",),
            ("link", "observer-pause"),
            ("journal", "search"),
        ],
    )
    entries = [
        entry(
            tmp_path,
            path=("body", "status"),
            operation_id="body.status",
            entry_type="http",
            method="GET",
            route="/app/body/api/status",
            contract_operation_id="body.status",
        ),
        entry(
            tmp_path,
            path=("identity",),
            operation_id="moved.identity",
            entry_type="moved-stub",
        ),
        entry(
            tmp_path,
            path=("navigate",),
            operation_id="moved.navigate",
            entry_type="moved-stub",
        ),
        entry(
            tmp_path,
            path=("link", "observer-pause"),
            operation_id="link.observer_pause",
            entry_type="local",
        ),
    ]

    errors = inventory.check_complete_partition(
        entries,
        oracle,
        expected_http_total=1,
        expected_journal_total=1,
        expected_stub_counts={"moved-stub": 2, "local": 1},
        expected_http_group_counts={"body": 1},
    )

    assert errors == []


def test_complete_partition_rejects_non_journal_uncovered_paths(tmp_path: Path) -> None:
    oracle = write_oracle(
        tmp_path,
        [
            ("body", "status"),
            ("entities", "list"),
            ("journal", "search"),
        ],
    )
    entries = [
        entry(
            tmp_path,
            path=("body", "status"),
            operation_id="body.status",
            entry_type="http",
            method="GET",
            route="/app/body/api/status",
            contract_operation_id="body.status",
        )
    ]

    errors = inventory.check_complete_partition(
        entries,
        oracle,
        expected_http_total=1,
        expected_journal_total=1,
        expected_stub_counts={},
        expected_http_group_counts={"body": 1},
    )

    assert any("uncovered oracle path count" in error for error in errors)
    assert any("uncovered non-journal oracle paths" in error for error in errors)

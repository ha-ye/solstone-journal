# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

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

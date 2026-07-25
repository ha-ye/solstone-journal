# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.core_compile_inputs import (
    CoreCompileInputError,
    discover_core_compile_inputs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_real_tree_discovers_only_shipping_root_contract() -> None:
    assets = discover_core_compile_inputs(REPO_ROOT)

    assert len(assets) == 1
    asset = assets[0]
    assert (
        asset.source_file
        == REPO_ROOT / "core/crates/solstone-core-sol-client-cli/src/help.rs"
    )
    assert asset.line == 8
    assert asset.sdist_path == "core/fixtures/native-sol/root-contract-v1.json"
    excluded_sources = {
        REPO_ROOT / "core/crates/solstone-core-indexer-store/src/db.rs",
        REPO_ROOT / "core/crates/solstone-core-indexer/src/chunker.rs",
        REPO_ROOT / "core/crates/solstone-core-sol-client-cli/tests/parity.rs",
        REPO_ROOT
        / "core/crates/solstone-core-sol-client-cli/src/bin/resolve_parity_leaves.rs",
    }
    assert asset.source_file not in excluded_sources


def test_target_conditional_module_is_discovered_on_non_matching_host(
    tmp_path: Path,
) -> None:
    _write_workspace(
        tmp_path,
        helper_lib='#[cfg(target_os = "definitely_not_solstone")] mod gated;\n',
        extra={
            "core/crates/solstone-core-helper/src/gated.rs": (
                'const ASSET: &str = include_str!("asset.txt");\n'
            ),
            "core/crates/solstone-core-helper/src/asset.txt": "asset\n",
        },
    )

    assets = discover_core_compile_inputs(tmp_path)

    assert [asset.sdist_path for asset in assets] == [
        "core/crates/solstone-core-helper/src/asset.txt"
    ]


def test_cfg_test_regions_are_removed_before_include_scan(tmp_path: Path) -> None:
    _write_workspace(
        tmp_path,
        helper_lib=(
            "#[cfg(test)]\n"
            "mod tests {\n"
            '    const TEST_ONLY: &str = r#"{"path":"missing.txt"}"#;\n'
            '    const IGNORED: &str = include_str!("missing.txt");\n'
            "}\n"
            'const ASSET: &str = include_str!("asset.txt");\n'
        ),
        extra={"core/crates/solstone-core-helper/src/asset.txt": "asset\n"},
    )

    assets = discover_core_compile_inputs(tmp_path)

    assert [asset.sdist_path for asset in assets] == [
        "core/crates/solstone-core-helper/src/asset.txt"
    ]


@pytest.mark.parametrize(
    ("helper_lib", "message"),
    [
        (
            'const ASSET: &str = include_str!(concat!("asset", ".txt"));\n',
            "unsupported-include-argument",
        ),
        (
            'const ASSET: &str = include_str!("../../../../../outside.txt");\n',
            "outside-repo",
        ),
        ('const ASSET: &str = include_str!("missing.txt");\n', "compile-input-missing"),
        ('#[path = "missing.rs"] mod missing;\n', "path-module-missing"),
    ],
)
def test_discovery_failures_are_loud(
    tmp_path: Path, helper_lib: str, message: str
) -> None:
    _write_workspace(tmp_path, helper_lib=helper_lib)

    with pytest.raises(CoreCompileInputError, match=message):
        discover_core_compile_inputs(tmp_path)


def test_unterminated_cfg_test_region_fails_loudly(tmp_path: Path) -> None:
    _write_workspace(tmp_path, helper_lib="#[cfg(test)]\nmod tests {\n")

    with pytest.raises(CoreCompileInputError, match="delimiter-match-failed"):
        discover_core_compile_inputs(tmp_path)


def _write_workspace(
    root: Path,
    *,
    helper_lib: str,
    extra: dict[str, str] | None = None,
) -> None:
    _write(
        root / "core/Cargo.toml",
        (
            '[workspace]\nmembers = ["crates/solstone-core", '
            '"crates/solstone-core-helper"]\nresolver = "3"\n\n'
            "[workspace.dependencies]\n"
            'solstone-core-helper = { path = "crates/solstone-core-helper" }\n'
        ),
    )
    _write(
        root / "core/crates/solstone-core/Cargo.toml",
        (
            '[package]\nname = "solstone-core"\nversion = "1.2.3"\n'
            "\n[dependencies]\nsolstone-core-helper.workspace = true\n"
            '\n[[bin]]\nname = "sol"\npath = "src/main.rs"\n'
        ),
    )
    _write(root / "core/crates/solstone-core/src/main.rs", "fn main() {}\n")
    _write(
        root / "core/crates/solstone-core-helper/Cargo.toml",
        '[package]\nname = "solstone-core-helper"\nversion = "1.2.3"\n',
    )
    _write(root / "core/crates/solstone-core-helper/src/lib.rs", helper_lib)
    for relative, content in (extra or {}).items():
        _write(root / relative, content)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

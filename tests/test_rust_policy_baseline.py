# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GRAPH_TARGETS = {
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "aarch64-apple-darwin",
    "aarch64-apple-ios",
}


def _read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _makefile_block(name: str, next_name: str) -> str:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    start = text.index(f"\n{name}:")
    end = text.index(f"\n{next_name}:", start + 1)
    return text[start:end]


def _workspace_member_manifest_paths() -> list[Path]:
    workspace = _read_toml(ROOT / "core" / "Cargo.toml")
    members = workspace["workspace"]["members"]
    assert isinstance(members, list)
    assert members, "workspace member enumeration must not be empty"

    paths: list[Path] = []
    for member in members:
        assert isinstance(member, str)
        path = ROOT / "core" / member / "Cargo.toml"
        assert path.is_file(), f"workspace member manifest is missing: {path}"
        paths.append(path)
    assert len(paths) == len(members)
    return paths


def _rust_source_paths() -> list[Path]:
    paths = sorted((ROOT / "core" / "crates").rglob("*.rs"))
    assert paths, "Rust source enumeration must not be empty"
    return paths


def test_check_rust_deny_recipe_is_version_asserted_locked_and_offline() -> None:
    block = _makefile_block("check-rust-deny", "audit")
    required_commands = [
        "scripts/check_release_preflight.py cargo-deny",
        "--locked",
        "--offline",
        "check bans licenses sources",
    ]

    assert required_commands
    for command in required_commands:
        assert command in block


def test_audit_recipe_uses_signed_packet_without_fetch_db() -> None:
    block = _makefile_block("audit", "skills")
    required_commands = [
        "scripts/check_release_preflight.py cargo-deny",
        "scripts/advisory_mirror_audit.py",
        "AUDIT_ADVISORY_BUNDLE",
        "AUDIT_ADVISORY_RECEIPT",
        "AUDIT_ADVISORY_PUBKEY",
        "AUDIT_ADVISORY_LOCATOR",
        "--bundle",
        "--receipt",
        "--pubkey",
        "--locator",
    ]

    assert required_commands
    for command in required_commands:
        assert command in block
    assert "fetch db" not in block

    # make audit stdout must be exactly the witness JSON; the private locator must never be echoed.
    preflight_line = next(
        line
        for line in block.splitlines()
        if "check_release_preflight.py cargo-deny" in line
    )
    audit_line = next(
        line
        for line in block.splitlines()
        if "scripts/advisory_mirror_audit.py" in line
    )
    assert ">&2" in preflight_line
    assert audit_line.lstrip("\t").startswith("@")


def test_release_candidate_driver_binds_policy_before_artifact_construction() -> None:
    driver_text = (ROOT / "scripts" / "release_candidate_driver.py").read_text(
        encoding="utf-8"
    )
    release_text = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    policy_call = "policy_run = svc.prepare_policy(root, env)"
    build_call = "svc.build_local_dist(root, include_models)"
    inspected_commands = [policy_call, build_call]

    assert inspected_commands, "release rail command enumeration must not be empty"
    assert policy_call in driver_text
    assert build_call in driver_text
    assert driver_text.index(policy_call) < driver_text.index(build_call)
    assert "make audit" not in release_text


def test_ci_summary_names_only_established_evidence_classes() -> None:
    block = _makefile_block("ci", "verify")
    summary_start = block.index("All CI checks passed; evidence classes:")
    summary = block[summary_start:]
    expected_lines = [
        "All CI checks passed; evidence classes:",
        "  GNU-host checks: fmt, MSRV, clippy, tests, dependency policy",
        "  iOS cross-target canary: check-rust-ios",
    ]

    assert expected_lines
    for line in expected_lines:
        assert line in summary
    lower_summary = summary.lower()
    assert "advis" not in lower_summary
    assert "release" not in lower_summary
    assert "artifact" not in lower_summary
    assert "native-host" not in lower_summary


def test_deny_toml_models_supported_graph_and_unknown_git_policy() -> None:
    deny = _read_toml(ROOT / "core" / "deny.toml")
    targets = deny["graph"]["targets"]
    sources = deny["sources"]

    assert isinstance(targets, list)
    assert len(targets) == len(EXPECTED_GRAPH_TARGETS)
    assert set(targets) == EXPECTED_GRAPH_TARGETS
    assert sources["unknown-git"] == "deny"
    assert sources["allow-git"] == ["https://github.com/solpbc/spl-rust"]


def test_workspace_forbids_unsafe_and_members_inherit_lints() -> None:
    workspace = _read_toml(ROOT / "core" / "Cargo.toml")
    member_paths = _workspace_member_manifest_paths()

    assert workspace["workspace"]["lints"]["rust"]["unsafe_code"] == "forbid"
    assert member_paths
    for path in member_paths:
        member = _read_toml(path)
        assert member["lints"]["workspace"] is True


def test_member_manifests_do_not_shadow_workspace_unsafe_floor() -> None:
    member_paths = _workspace_member_manifest_paths()

    assert member_paths
    for path in member_paths:
        member = _read_toml(path)
        rust_lints = member.get("lints", {}).get("rust", {})
        assert "unsafe_code" not in rust_lints, path


def test_core_crates_have_zero_unsafe_inventory() -> None:
    paths = _rust_source_paths()
    hits: list[str] = []

    assert paths
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            if "unsafe" in line:
                hits.append(f"{path.relative_to(ROOT)}:{line_number}:{line.strip()}")

    assert not hits, "\n".join(hits)


def test_core_crates_do_not_allow_unsafe_code() -> None:
    paths = _rust_source_paths()
    hits: list[str] = []

    assert paths
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            if "allow(unsafe_code)" in line:
                hits.append(f"{path.relative_to(ROOT)}:{line_number}:{line.strip()}")

    assert not hits, "\n".join(hits)

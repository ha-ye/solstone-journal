# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.check_release_preflight as preflight
import scripts.release_tool_pins as pins


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_preflight_imports_release_tool_pins_from_authoritative_module() -> None:
    assert preflight.ZIG_VERSION == pins.ZIG_VERSION
    assert preflight.CARGO_DENY_VERSION == pins.CARGO_DENY_VERSION

    source = Path(preflight.__file__).read_text(encoding="utf-8")
    assert "EXPECTED_ZIG_VERSION =" not in source
    assert "EXPECTED_CARGO_DENY_VERSION =" not in source


def test_load_toolchain_spec(tmp_path: Path) -> None:
    (tmp_path / "rust-toolchain.toml").write_text(
        """
[toolchain]
channel = "1.97.1"
profile = "minimal"
components = ["rustfmt", "clippy"]
targets = ["x86_64-unknown-linux-musl"]
""".lstrip(),
        encoding="utf-8",
    )

    spec = preflight.load_toolchain_spec(tmp_path)

    assert spec.channel == "1.97.1"
    assert spec.profile == "minimal"
    assert spec.components == ("rustfmt", "clippy")
    assert spec.targets == ("x86_64-unknown-linux-musl",)


def test_missing_toolchain_reports_repair_without_rustup_call(tmp_path: Path) -> None:
    _toolchain_dir, failures = preflight.check_toolchain_installed(
        "1.95.0",
        tmp_path / "rustup-home",
    )

    assert failures
    assert failures[0].expected == "installed rustup toolchain 1.95.0"
    assert failures[0].repair == "rustup toolchain install 1.95.0"


def test_rustup_toolchain_override_must_match_pin() -> None:
    failures = preflight.check_rustup_override(
        "1.97.1",
        {"RUSTUP_TOOLCHAIN": "1.96.0"},
    )

    assert failures
    assert failures[0].actual == "1.96.0"


def test_rustup_toolchain_override_accepts_exact_pin() -> None:
    assert (
        preflight.check_rustup_override(
            "1.97.1",
            {"RUSTUP_TOOLCHAIN": "1.97.1"},
        )
        == []
    )


def test_rustc_version_mismatch_reports_expected_actual_and_repair(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "1.97.1-x86_64-unknown-linux-gnu"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin" / "rustc").write_text("", encoding="utf-8")

    failures = preflight.check_rustc_version(
        toolchain,
        "1.97.1",
        runner=lambda *_args, **_kwargs: _completed("rustc 1.96.0 (abc 2026-01-01)"),
    )

    assert failures
    assert failures[0].expected == "rustc 1.97.1"
    assert "rustc 1.96.0" in failures[0].actual
    assert failures[0].repair == "rustup toolchain install 1.97.1"


def test_missing_toolchain_component_reports_repair(tmp_path: Path) -> None:
    toolchain = tmp_path / "1.97.1-x86_64-unknown-linux-gnu"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin" / "rustfmt").write_text("", encoding="utf-8")

    failures = preflight.check_toolchain_components(
        toolchain,
        "1.97.1",
        ("rustfmt", "clippy"),
    )

    assert len(failures) == 1
    assert failures[0].error == "release toolchain component is not installed"
    assert "clippy" in failures[0].expected
    assert failures[0].actual == "missing"
    assert failures[0].repair == "rustup component add --toolchain 1.97.1 clippy"


def test_missing_toolchain_target_reports_repair(tmp_path: Path) -> None:
    toolchain = tmp_path / "1.97.1-x86_64-unknown-linux-gnu"
    (toolchain / "lib" / "rustlib" / "x86_64-unknown-linux-musl" / "lib").mkdir(
        parents=True
    )

    failures = preflight.check_toolchain_targets(
        toolchain,
        "1.97.1",
        ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl"),
    )

    assert len(failures) == 1
    assert failures[0].error == "release toolchain target is not installed"
    assert "aarch64-unknown-linux-musl" in failures[0].expected
    assert failures[0].actual == "missing"
    assert (
        failures[0].repair
        == "rustup target add --toolchain 1.97.1 aarch64-unknown-linux-musl"
    )


def test_missing_zig_reports_repair() -> None:
    failures = preflight.check_zig(which=lambda _name: None)

    assert failures
    assert failures[0].expected == f"zig {pins.ZIG_VERSION}"
    assert failures[0].actual == "not found"


def test_mismatched_zig_reports_expected_actual_and_repair() -> None:
    failures = preflight.check_zig(
        which=lambda _name: "/usr/bin/zig",
        runner=lambda *_args, **_kwargs: _completed("0.15.0"),
    )

    assert failures
    assert failures[0].expected == pins.ZIG_VERSION
    assert failures[0].actual == "0.15.0"
    assert f"ziglang=={pins.ZIG_VERSION}" in failures[0].repair


def test_missing_cargo_deny_reports_force_install_repair() -> None:
    failures = preflight.check_cargo_deny(which=lambda _name: None)

    assert failures
    assert failures[0].expected == pins.CARGO_DENY_VERSION
    assert failures[0].actual == "not found"
    assert (
        failures[0].repair
        == f"cargo install cargo-deny@{pins.CARGO_DENY_VERSION} --locked --force"
    )


def test_mismatched_cargo_deny_reports_expected_actual_and_repair() -> None:
    failures = preflight.check_cargo_deny(
        which=lambda _name: "/usr/bin/cargo-deny",
        runner=lambda *_args, **_kwargs: _completed("cargo-deny 0.19.9"),
    )

    assert failures
    assert failures[0].expected == pins.CARGO_DENY_VERSION
    assert failures[0].actual == "cargo-deny 0.19.9"
    assert (
        failures[0].repair
        == f"cargo install cargo-deny@{pins.CARGO_DENY_VERSION} --locked --force"
    )


def test_dirty_local_status_names_offending_paths() -> None:
    failures = preflight.check_local_clean_status(
        " M core/Cargo.toml\n?? scratch.txt\n"
    )

    assert failures
    assert "core/Cargo.toml" in failures[0].actual
    assert "scratch.txt" in failures[0].actual


def test_remote_state_reports_ref_mismatch_and_dirty_tree() -> None:
    failures = preflight.check_remote_state(
        "abc123",
        "def456",
        "?? remote.txt\n",
        label="mac-builder",
    )

    assert len(failures) == 2
    assert failures[0].expected == "abc123"
    assert failures[0].actual == "def456"
    assert (
        failures[0].repair
        == "python3 scripts/check_release_preflight.py remote-state --help"
    )
    assert "remote.txt" in failures[1].actual


def test_expected_lane_tool_evidence_uses_grounded_release_pins() -> None:
    source = preflight.expected_lane_tool_evidence("source")
    linux = preflight.expected_lane_tool_evidence("linux-aarch64-musl")
    macos = preflight.expected_lane_tool_evidence("macos-arm64")
    macos_presign = preflight.expected_presign_lane_tool_evidence("macos-arm64")

    assert source["python"] == pins.PYTHON_SOURCE_LINUX_VERSION
    assert "zig" not in source
    assert linux["zig"] == pins.ZIG_PIN
    assert macos["python"] == pins.PYTHON_MACOS_VERSION
    assert macos["swift"] == pins.MACOS_SWIFT_PIN
    assert macos["codesign"] == pins.MACOS_CODESIGN_PUBLIC_PIN
    assert macos["signing_mode"] == pins.MACOS_SIGNING_MODE
    assert "signing_mode" not in macos_presign


@pytest.mark.parametrize("lane", tuple(preflight.LANE_TOOL_KEYS))
def test_fixture_lane_tool_evidence_key_sets_match_expectations(lane: str) -> None:
    assert set(pins.fixture_lane_tool_evidence(lane)) == set(
        preflight.expected_lane_tool_evidence(lane)
    )
    assert set(pins.fixture_presign_lane_tool_evidence(lane)) == set(
        preflight.expected_presign_lane_tool_evidence(lane)
    )


def test_host_variant_tool_keys_include_swift_and_uv() -> None:
    assert pins.HOST_VARIANT_TOOL_KEYS == frozenset(("uv", "swift"))


@pytest.mark.parametrize(
    "uv_banner",
    (
        pins.UV_LINUX_FIXTURE_BANNER,
        pins.UV_MACOS_FIXTURE_BANNER,
    ),
)
def test_host_variant_uv_evidence_accepts_strict_version(uv_banner: str) -> None:
    source = pins.fixture_lane_tool_evidence("source")
    source["uv"] = uv_banner
    macos_presign = pins.fixture_presign_lane_tool_evidence("macos-arm64")
    macos_presign["uv"] = uv_banner

    assert preflight.check_lane_tool_evidence("source", source) == []
    assert (
        preflight.check_presign_lane_tool_evidence("macos-arm64", macos_presign) == []
    )


@pytest.mark.parametrize(
    "uv_banner",
    (
        "uv 0.10.0 (x86_64-unknown-linux-gnu)",
        "not found",
        "exit 1",
        "",
        "   ",
        f"{pins.UV_LINUX_FIXTURE_BANNER}\nextra",
        "uvx 0.11.4 (x86_64-unknown-linux-gnu)",
        "uv release channel stable",
        pins.UV_PIN,
    ),
)
def test_host_variant_uv_evidence_fails_closed(uv_banner: str) -> None:
    evidence = pins.fixture_lane_tool_evidence("source")
    evidence["uv"] = uv_banner

    failures = preflight.check_lane_tool_evidence("source", evidence)

    uv_failures = [
        failure
        for failure in failures
        if failure.error == "release lane tool uv is not pinned"
    ]
    assert len(uv_failures) == 1
    assert uv_failures[0].expected == pins.UV_PIN


@pytest.mark.parametrize("uv_value", (None, 123, True, {"a": 1}))
def test_host_variant_uv_non_string_evidence_fails_closed(uv_value: object) -> None:
    lane_evidence: dict[str, object] = dict(pins.fixture_lane_tool_evidence("source"))
    lane_evidence["uv"] = uv_value
    presign_evidence: dict[str, object] = dict(
        pins.fixture_presign_lane_tool_evidence("macos-arm64")
    )
    presign_evidence["uv"] = uv_value

    lane_failures = preflight.check_lane_tool_evidence("source", lane_evidence)
    presign_failures = preflight.check_presign_lane_tool_evidence(
        "macos-arm64", presign_evidence
    )

    lane_uv_failures = [
        failure
        for failure in lane_failures
        if failure.error == "release lane tool uv is not pinned"
    ]
    presign_uv_failures = [
        failure
        for failure in presign_failures
        if failure.error == "pre-sign lane tool uv is not pinned"
    ]

    assert len(lane_uv_failures) == 1
    assert lane_uv_failures[0].expected == pins.UV_PIN
    assert lane_uv_failures[0].actual == str(uv_value)
    assert len(presign_uv_failures) == 1
    assert presign_uv_failures[0].expected == pins.UV_PIN
    assert presign_uv_failures[0].actual == str(uv_value)


@pytest.mark.parametrize(
    "swift_banner",
    (
        pins.MACOS_SWIFT_FIXTURE_BANNER,
        pins.MACOS_SWIFT_PIN,
        pins.MACOS_SWIFT_RAW_BANNER,
        pins.MACOS_SWIFT_FLATTENED_BANNER,
    ),
)
def test_host_variant_swift_evidence_accepts_strict_identity(
    swift_banner: str,
) -> None:
    lane_evidence = pins.fixture_lane_tool_evidence("macos-arm64")
    lane_evidence["swift"] = swift_banner
    presign_evidence = pins.fixture_presign_lane_tool_evidence("macos-arm64")
    presign_evidence["swift"] = swift_banner

    assert preflight.check_lane_tool_evidence("macos-arm64", lane_evidence) == []
    assert (
        preflight.check_presign_lane_tool_evidence("macos-arm64", presign_evidence)
        == []
    )


@pytest.mark.parametrize(
    "swift_banner",
    (
        pins.MACOS_SWIFT_RAW_BANNER,
        pins.MACOS_SWIFT_FLATTENED_BANNER,
    ),
)
def test_parse_macos_swift_banner_accepts_real_host_forms(
    swift_banner: str,
) -> None:
    assert pins.parse_macos_swift_banner(swift_banner) == (
        "6.3.3",
        "6.3.3.1.3",
        "2100.1.1.101",
    )
    assert (
        pins.check_host_variant_tool_pin("swift", pins.MACOS_SWIFT_PIN, swift_banner)
        is True
    )


def test_check_host_variant_swift_pin_rejects_mutated_flattened_identity() -> None:
    mutated = pins.MACOS_SWIFT_FLATTENED_BANNER.replace(
        "Apple Swift version 6.3.3", "Apple Swift version 6.3.4"
    )
    assert pins.parse_macos_swift_banner(mutated) == (
        "6.3.4",
        "6.3.3.1.3",
        "2100.1.1.101",
    )
    assert (
        pins.check_host_variant_tool_pin("swift", pins.MACOS_SWIFT_PIN, mutated)
        is False
    )


@pytest.mark.parametrize(
    "swift_value",
    (
        "not found",
        "exit 1",
        "",
        "   ",
        "swift 6.3.3",
        "swift-driver 1.148.6 Apple Swift version 6.3.3 (swiftlang-6.3.3.1.3 clang-2100.1.1.101)",
        f"{pins.MACOS_SWIFT_FIXTURE_BANNER} extra",
        f"{pins.MACOS_SWIFT_FIXTURE_BANNER} Apple Swift version 6.3.3 (swiftlang-6.3.3.1.3 clang-2100.1.1.101)",
        "Apple Swift 6.3.3 (swiftlang-6.3.3.1.3 clang-2100.1.1.101)",
        pins.MACOS_SWIFT_FIXTURE_BANNER.replace(
            "Apple Swift version 6.3.3", "Apple Swift version 6.3.4"
        ),
        pins.MACOS_SWIFT_FIXTURE_BANNER.replace(
            "swiftlang-6.3.3.1.3", "swiftlang-6.3.3.1.4"
        ),
        pins.MACOS_SWIFT_FIXTURE_BANNER.replace(
            "clang-2100.1.1.101", "clang-2100.1.1.102"
        ),
        None,
        123,
        True,
        {"a": 1},
    ),
)
def test_host_variant_swift_evidence_fails_closed(swift_value: object) -> None:
    lane_evidence: dict[str, object] = dict(
        pins.fixture_lane_tool_evidence("macos-arm64")
    )
    lane_evidence["swift"] = swift_value
    presign_evidence: dict[str, object] = dict(
        pins.fixture_presign_lane_tool_evidence("macos-arm64")
    )
    presign_evidence["swift"] = swift_value

    lane_failures = preflight.check_lane_tool_evidence("macos-arm64", lane_evidence)
    presign_failures = preflight.check_presign_lane_tool_evidence(
        "macos-arm64", presign_evidence
    )

    lane_swift_failures = [
        failure
        for failure in lane_failures
        if failure.error == "release lane tool swift is not pinned"
    ]
    presign_swift_failures = [
        failure
        for failure in presign_failures
        if failure.error == "pre-sign lane tool swift is not pinned"
    ]
    assert len(lane_swift_failures) == 1
    assert lane_swift_failures[0].expected == pins.MACOS_SWIFT_PIN
    assert lane_swift_failures[0].actual == str(swift_value)
    assert len(presign_swift_failures) == 1
    assert presign_swift_failures[0].expected == pins.MACOS_SWIFT_PIN
    assert presign_swift_failures[0].actual == str(swift_value)


def test_notarytool_evidence_accepts_bare_grounded_output() -> None:
    lane_evidence = pins.fixture_lane_tool_evidence("macos-arm64")
    lane_evidence["notarytool"] = "1.1.2 (41)"
    presign_evidence = pins.fixture_presign_lane_tool_evidence("macos-arm64")
    presign_evidence["notarytool"] = "1.1.2 (41)"

    assert preflight.check_lane_tool_evidence("macos-arm64", lane_evidence) == []
    assert (
        preflight.check_presign_lane_tool_evidence("macos-arm64", presign_evidence)
        == []
    )


@pytest.mark.parametrize(
    "notarytool_value",
    (
        "notarytool 1.1.2 (41)",
        "1.1.3 (41)",
        "1.1.2 (42)",
    ),
)
def test_notarytool_evidence_rejects_ungrounded_or_wrong_output(
    notarytool_value: str,
) -> None:
    lane_evidence = pins.fixture_lane_tool_evidence("macos-arm64")
    lane_evidence["notarytool"] = notarytool_value
    presign_evidence = pins.fixture_presign_lane_tool_evidence("macos-arm64")
    presign_evidence["notarytool"] = notarytool_value

    lane_failures = preflight.check_lane_tool_evidence("macos-arm64", lane_evidence)
    presign_failures = preflight.check_presign_lane_tool_evidence(
        "macos-arm64", presign_evidence
    )

    assert any(
        failure.error == "release lane tool notarytool is not pinned"
        for failure in lane_failures
    )
    assert any(
        failure.error == "pre-sign lane tool notarytool is not pinned"
        for failure in presign_failures
    )


def test_lane_tool_skew_fails_closed() -> None:
    evidence = pins.fixture_lane_tool_evidence("macos-arm64")
    evidence["swift"] = "swift 6.3.3"

    failures = preflight.check_lane_tool_evidence("macos-arm64", evidence)

    assert failures
    assert failures[0].error == "release lane tool swift is not pinned"
    assert failures[0].expected == pins.MACOS_SWIFT_PIN


def test_collect_lane_tools_normalizes_macos_observations() -> None:
    outputs = {
        "python": "Python 3.14.6",
        "rustc": pins.RUSTC_VERSION_BANNER,
        "cargo": pins.CARGO_VERSION_PIN,
        "uv": pins.UV_MACOS_FIXTURE_BANNER,
        "maturin": pins.MATURIN_PIN,
        "cargo-deny": pins.CARGO_DENY_PIN,
        "xcodebuild": f"Xcode {pins.MACOS_XCODE_VERSION}\nBuild version {pins.MACOS_XCODE_BUILD}\n",
        "swift": f"{pins.MACOS_SWIFT_FIXTURE_BANNER}\nTarget: arm64-apple-macosx26.0\n",
        "xcrun": pins.MACOS_NOTARYTOOL_PIN,
    }

    def which(name: str) -> str | None:
        if name == "codesign":
            return pins.MACOS_CODESIGN_PATH
        return f"/tools/{name}" if name in outputs else None

    def runner(argv, **_kwargs) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if argv[0] == "python":
            name = "python"
        return _completed(outputs[name])

    evidence = preflight.collect_lane_tool_evidence(
        "macos-arm64",
        which=which,
        runner=runner,
        python_executable="python",
    )

    assert "signing_mode" not in evidence
    assert evidence["swift"] == pins.MACOS_SWIFT_FIXTURE_BANNER
    assert preflight.check_presign_lane_tool_evidence("macos-arm64", evidence) == []
    failures = preflight.check_presign_lane_tool_evidence(
        "macos-arm64",
        {**evidence, "signing_mode": pins.MACOS_SIGNING_MODE},
    )
    assert any(
        failure.error == "pre-sign lane tool evidence keys do not match lane"
        for failure in failures
    )
    assert (
        preflight.check_lane_tool_evidence(
            "macos-arm64",
            {**evidence, "signing_mode": pins.MACOS_SIGNING_MODE},
        )
        == []
    )


def _native_record(role: str) -> dict[str, object]:
    return {
        "role": role,
        "unsigned_members": {},
        "signing_mode": pins.MACOS_SIGNING_MODE,
        "signing": {
            "signer_pinned": True,
            "team_pinned": True,
            "hardened_runtime": True,
            "trusted_timestamp": True,
        },
        "notarization_status": "accepted",
    }


def test_macos_tool_finalizer_requires_valid_native_records() -> None:
    preflight_evidence = pins.fixture_presign_lane_tool_evidence("macos-arm64")

    final, failures = preflight.finalize_macos_tool_evidence(
        preflight_evidence,
        (
            _native_record("root"),
            _native_record("core"),
            _native_record("speakers-analyze"),
        ),
    )

    assert failures == []
    assert final is not None
    assert final["signing_mode"] == pins.MACOS_SIGNING_MODE

    bad_record = _native_record("root")
    bad_record["notarization_status"] = "rejected"
    final, failures = preflight.finalize_macos_tool_evidence(
        preflight_evidence,
        (
            bad_record,
            _native_record("core"),
            _native_record("speakers-analyze"),
        ),
    )

    assert final is None
    assert any(
        failure.error == "macOS native record notarization is not accepted"
        for failure in failures
    )

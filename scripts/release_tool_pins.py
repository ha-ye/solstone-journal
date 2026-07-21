#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pinned public and private inputs for the release rail."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

LaneName = Literal[
    "source",
    "linux-x86_64-musl",
    "linux-aarch64-musl",
    "macos-arm64",
]

PYTHON_SOURCE_LINUX_VERSION = "3.14.3"
PYTHON_MACOS_VERSION = "3.14.6"

RUSTC_VERSION_BANNER = "rustc 1.97.1 (8bab26f4f 2026-07-14)"
RUSTC_BINARY_PIN = "rustc"
RUSTC_COMMIT_HASH_PIN = "8bab26f4f68e0e26f0bb7960be334d5b520ea452"
RUSTC_COMMIT_DATE_PIN = "2026-07-14"
RUSTC_RELEASE_PIN = "1.97.1"
RUSTC_LLVM_PIN = "22.1.6"
CARGO_VERSION_PIN = "cargo 1.97.1 (c980f4866 2026-06-30)"
CARGO_RELEASE_PIN = "1.97.1"
CARGO_DENY_VERSION = "0.20.2"
CARGO_DENY_PIN = f"cargo-deny {CARGO_DENY_VERSION}"

UV_VERSION = "0.11.4"
UV_PIN = f"uv {UV_VERSION}"
MATURIN_VERSION = "1.14.1"
MATURIN_REQUIREMENT = f"maturin=={MATURIN_VERSION}"
MATURIN_PIN = f"maturin {MATURIN_VERSION}"
ZIG_VERSION = "0.16.0"
ZIG_PIN = f"zig {ZIG_VERSION}"

MACOS_XCODE_VERSION = "26.6"
MACOS_XCODE_BUILD = "17F113"
MACOS_XCODE_PIN = f"xcode {MACOS_XCODE_VERSION} build {MACOS_XCODE_BUILD}"
MACOS_SWIFT_PIN = "Apple Swift 6.3.3 (swiftlang-6.3.3.1.3 clang-2100.1.1.101)"
MACOS_CODESIGN_PATH = "/usr/bin/codesign"
MACOS_CODESIGN_PUBLIC_PIN = "codesign pinned-path verified"
MACOS_NOTARYTOOL_VERSION = "1.1.2 (41)"
MACOS_NOTARYTOOL_PIN = f"notarytool {MACOS_NOTARYTOOL_VERSION}"
MACOS_SIGNING_MODE = "signed-verified"

MACOS_SIGNER_IDENTITY = "Developer ID Application: sol pbc (7QCG8V4M6H)"
MACOS_TEAM_IDENTIFIER = "7QCG8V4M6H"
PRIVATE_SIGNING_POLICY_VALUES = frozenset(
    (MACOS_SIGNER_IDENTITY, MACOS_TEAM_IDENTIFIER)
)

SETUPTOOLS_BUILD_REQUIRES = ("setuptools==83.0.0", "wheel==0.47.0")


@dataclass(frozen=True)
class ReleaseToolPins:
    python_source_linux_version: str = PYTHON_SOURCE_LINUX_VERSION
    python_macos_version: str = PYTHON_MACOS_VERSION
    rustc_version_banner: str = RUSTC_VERSION_BANNER
    cargo_version_pin: str = CARGO_VERSION_PIN
    cargo_deny_version: str = CARGO_DENY_VERSION
    uv_pin: str = UV_PIN
    maturin_pin: str = MATURIN_PIN
    zig_pin: str = ZIG_PIN
    macos_xcode_pin: str = MACOS_XCODE_PIN
    macos_swift_pin: str = MACOS_SWIFT_PIN
    macos_codesign_public_pin: str = MACOS_CODESIGN_PUBLIC_PIN
    macos_notarytool_pin: str = MACOS_NOTARYTOOL_PIN
    macos_signing_mode: str = MACOS_SIGNING_MODE


def load_release_tool_pins(_root: Path | None = None) -> ReleaseToolPins:
    return ReleaseToolPins()


def fixture_native_tools(lane: LaneName) -> dict[str, str]:
    if lane == "source":
        return {"uv": UV_PIN, "maturin": MATURIN_PIN}
    if lane in {"linux-x86_64-musl", "linux-aarch64-musl"}:
        return {"uv": UV_PIN, "maturin": MATURIN_PIN, "zig": ZIG_PIN}
    if lane == "macos-arm64":
        return {
            "uv": UV_PIN,
            "maturin": MATURIN_PIN,
            "xcode": MACOS_XCODE_PIN,
            "swift": MACOS_SWIFT_PIN,
            "codesign": MACOS_CODESIGN_PUBLIC_PIN,
            "notarytool": MACOS_NOTARYTOOL_PIN,
            "signing_mode": MACOS_SIGNING_MODE,
        }
    raise ValueError(f"unknown release lane: {lane}")

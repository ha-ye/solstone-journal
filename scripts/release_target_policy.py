#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared native release target policy for build and proof lanes."""

from __future__ import annotations

from collections.abc import Mapping

TARGET_POLICY: Mapping[str, tuple[str, str]] = {
    "linux-x86_64-musl": ("Linux", "x86_64"),
    "linux-aarch64-musl": ("Linux", "aarch64"),
    "macos-arm64": ("Darwin", "arm64"),
}
TARGET_ENV_KEYS: Mapping[str, str] = {
    "linux-x86_64-musl": "RELEASE_PROOF_HOST_LINUX_X86_64_MUSL_CHANNEL",
    "linux-aarch64-musl": "RELEASE_PROOF_HOST_LINUX_AARCH64_MUSL_CHANNEL",
    "macos-arm64": "RELEASE_PROOF_HOST_MACOS_ARM64_CHANNEL",
}

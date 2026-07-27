# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Examine-only restic readiness checks for solstone backup.

Stdlib-only by contract. Must NOT import third-party packages,
solstone.observe.*, solstone.think.journal_io, or any module that writes journal
state.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

RESTIC_VERSION = "0.19.0"
RESTIC_SCHEMA_VERSION = 1
RESTIC_TOOL = "restic"
RESTIC_BUNDLE_ENV = "SOLSTONE_RESTIC_BUNDLE"
RESTIC_GITHUB_URL_TEMPLATE = (
    "https://github.com/restic/restic/releases/download/v0.19.0/"
    "restic_0.19.0_{os}_{arch}.bz2"
)
RESTIC_BZ2_SHA256: dict[str, str] = {
    "restic_0.19.0_darwin_amd64.bz2": (
        "c9d9a71234bc0955fdba6da93cc9375f8793ec1e1cbce77a91014d536a969148"
    ),
    "restic_0.19.0_darwin_arm64.bz2": (
        "1475397bf759ef4be16a77b19dec650bdbfec00d2cacd82005553411cdd37997"
    ),
    "restic_0.19.0_linux_amd64.bz2": (
        "13176fe6d89d4357947a2cd107218ab2873a5f9d8e1ac2d4cd1c8e07e6839c21"
    ),
    "restic_0.19.0_linux_arm64.bz2": (
        "e522ce6bf748d753fee8093e8ec59359972cf5b6bc65fc7c7cf38ae952351d91"
    ),
}
ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
MAC_TOOL_DIR = Path.home() / "Library/Application Support/solstone/restic"
LINUX_TOOL_DIR = Path.home() / ".cache/solstone/restic"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_info() -> tuple[str, str]:
    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    machine = platform.machine().lower()
    arch = ARCH_ALIASES.get(machine)
    if os_name not in {"darwin", "linux"} or arch is None:
        raise RuntimeError(
            "restic unsupported platform: "
            f"{os_name}/{machine}; supported: darwin|linux on amd64|arm64"
        )
    return os_name, arch


def select_restic_asset(
    os_name: str | None = None,
    arch: str | None = None,
) -> tuple[str, str, str]:
    if os_name is None or arch is None:
        os_name, arch = _platform_info()
    normalized_arch = ARCH_ALIASES.get(arch.lower())
    if os_name not in {"darwin", "linux"} or normalized_arch is None:
        raise RuntimeError(
            "restic unsupported platform: "
            f"{os_name}/{arch}; supported: darwin|linux on amd64|arm64"
        )
    filename = f"restic_{RESTIC_VERSION}_{os_name}_{normalized_arch}.bz2"
    sha256 = RESTIC_BZ2_SHA256[filename]
    url = RESTIC_GITHUB_URL_TEMPLATE.format(os=os_name, arch=normalized_arch)
    return filename, url, sha256


def _tool_dir(os_name: str | None = None) -> Path:
    if os_name is None:
        os_name, _arch = _platform_info()
    if os_name == "darwin":
        return MAC_TOOL_DIR
    if os_name == "linux":
        return LINUX_TOOL_DIR
    raise RuntimeError(f"restic unsupported platform: {os_name}")


def _binary_path(tool_dir: Path) -> Path:
    return tool_dir / RESTIC_TOOL


def _sentinel_path(tool_dir: Path) -> Path:
    return tool_dir / ".install-complete"


def _license_path(tool_dir: Path) -> Path:
    return tool_dir / "restic.LICENSE"


def _load_sentinel(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sentinel_ready(
    payload: dict[str, Any] | None,
    os_name: str,
    arch: str,
    binary_path: Path,
) -> str | None:
    if payload is None:
        return None
    platform_info = payload.get("platform")
    if not isinstance(platform_info, dict):
        return None
    if (
        payload.get("schema_version") != RESTIC_SCHEMA_VERSION
        or payload.get("tool") != RESTIC_TOOL
        or payload.get("version") != RESTIC_VERSION
    ):
        return None
    if platform_info.get("os") != os_name or platform_info.get("arch") != arch:
        return None
    if payload.get("binary_path") != str(binary_path):
        return None
    sha256 = payload.get("sha256")
    return sha256 if isinstance(sha256, str) and sha256 else None


def _restic_version_ok(binary_path: Path) -> bool:
    try:
        result = subprocess.run(
            [str(binary_path), "version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and f"restic {RESTIC_VERSION}" in result.stdout


def _verify_binary(binary_path: Path, expected_sha256: str) -> bool:
    if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
        return False
    try:
        actual_sha256 = _file_sha256(binary_path)
    except OSError:
        return False
    return actual_sha256 == expected_sha256


def check_restic_ready(tool_dir: Path | None = None) -> Path | None:
    os_name, arch = _platform_info()
    resolved_tool_dir = tool_dir if tool_dir is not None else _tool_dir(os_name)
    binary_path = _binary_path(resolved_tool_dir)
    expected_sha256 = _sentinel_ready(
        _load_sentinel(_sentinel_path(resolved_tool_dir)),
        os_name,
        arch,
        binary_path,
    )
    if expected_sha256 is None:
        return None
    if not _restic_version_ok(binary_path):
        return None
    if not _verify_binary(binary_path, expected_sha256):
        return None
    return binary_path

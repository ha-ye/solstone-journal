# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acquire the pinned rclone binary used by hosted append-only backup."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from solstone.think.backup.install import (
    _download_with_retries,
    _write_binary_atomic,
    _write_sentinel_atomic,
)
from solstone.think.backup.readiness import _file_sha256, _platform_info

RCLONE_VERSION = "1.74.4"
RCLONE_SCHEMA_VERSION = 1
RCLONE_TOOL = "rclone"
RCLONE_BUNDLE_ENV = "SOLSTONE_RCLONE_BUNDLE"
RCLONE_URL_TEMPLATE = (
    "https://downloads.rclone.org/v{version}/rclone-v{version}-{asset_os}-{arch}.zip"
)
RCLONE_ZIP_SHA256: dict[str, str] = {
    "rclone-v1.74.4-linux-amd64.zip": (
        "fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"
    ),
    "rclone-v1.74.4-linux-arm64.zip": (
        "97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419"
    ),
    "rclone-v1.74.4-osx-amd64.zip": (
        "4188aa84043d7a6240912923f47639a9d2da21f3b40a521c065c8d92e66563f6"
    ),
    "rclone-v1.74.4-osx-arm64.zip": (
        "c2100e2d4a4b3be04c55cd45380cafe7647e1ad772bb055f52f00876ed701167"
    ),
}
RCLONE_LICENSE_TEXT = """Copyright (C) 2012 by Nick Craig-Wood http://www.craig-wood.com/nick/

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


def _asset_os(os_name: str) -> str:
    return "osx" if os_name == "darwin" else os_name


def select_rclone_asset(
    os_name: str | None = None,
    arch: str | None = None,
) -> tuple[str, str, str]:
    if os_name is None or arch is None:
        os_name, arch = _platform_info()
    if os_name not in {"darwin", "linux"} or arch not in {"amd64", "arm64"}:
        raise RuntimeError(
            "rclone unsupported platform: "
            f"{os_name}/{arch}; supported: darwin|linux on amd64|arm64"
        )
    filename = f"rclone-v{RCLONE_VERSION}-{_asset_os(os_name)}-{arch}.zip"
    return (
        filename,
        RCLONE_URL_TEMPLATE.format(
            version=RCLONE_VERSION,
            asset_os=_asset_os(os_name),
            arch=arch,
        ),
        RCLONE_ZIP_SHA256[filename],
    )


def _tool_dir(os_name: str) -> Path:
    if os_name == "darwin":
        return Path.home() / "Library/Application Support/solstone/rclone"
    if os_name == "linux":
        return Path.home() / ".cache/solstone/rclone"
    raise RuntimeError(f"rclone unsupported platform: {os_name}")


def _bundle_path(asset_filename: str) -> Path | None:
    env_path = os.getenv(RCLONE_BUNDLE_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    bundled = Path(__file__).resolve().parent / "_bin" / asset_filename
    return bundled if bundled.exists() else None


def _binary_path(tool_dir: Path) -> Path:
    return tool_dir / RCLONE_TOOL


def _sentinel_path(tool_dir: Path) -> Path:
    return tool_dir / ".install-complete"


def _license_path(tool_dir: Path) -> Path:
    return tool_dir / "rclone.LICENSE"


def _binary_member(filename: str) -> str:
    return f"{filename.removesuffix('.zip')}/rclone"


def _extract_binary(data: bytes, filename: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.read(_binary_member(filename))
    except (KeyError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"rclone asset extraction failed: {filename}") from exc


def _sentinel_payload(
    os_name: str,
    arch: str,
    binary_path: Path,
    binary_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RCLONE_SCHEMA_VERSION,
        "tool": RCLONE_TOOL,
        "version": RCLONE_VERSION,
        "sha256": binary_sha256,
        "platform": {"os": os_name, "arch": arch},
        "binary_path": str(binary_path),
    }


def check_rclone_ready(
    tool_dir: Path | None = None,
    *,
    version_timeout: float = 10.0,
) -> Path | None:
    os_name, arch = _platform_info()
    resolved_tool_dir = tool_dir if tool_dir is not None else _tool_dir(os_name)
    sentinel_path = _sentinel_path(resolved_tool_dir)
    try:
        payload = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    binary_path = _binary_path(resolved_tool_dir)
    expected = _sentinel_payload(
        os_name,
        arch,
        binary_path,
        str(payload.get("sha256", "")),
    )
    if payload != expected or not expected["sha256"]:
        return None
    if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
        return None
    try:
        actual_sha256 = _file_sha256(binary_path)
    except OSError:
        return None
    if actual_sha256 != expected["sha256"]:
        return None
    try:
        result = subprocess.run(
            [str(binary_path), "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=version_timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or f"rclone v{RCLONE_VERSION}" not in result.stdout:
        return None
    return binary_path


def ensure_rclone(
    *,
    force: bool = False,
    tool_dir: Path | None = None,
    attempts: int = 3,
    timeout: float = 60.0,
) -> Path:
    os_name, arch = _platform_info()
    resolved_tool_dir = tool_dir if tool_dir is not None else _tool_dir(os_name)
    if not force:
        ready_path = check_rclone_ready(
            resolved_tool_dir,
            version_timeout=10.0,
        )
        if ready_path is not None:
            return ready_path

    filename, url, expected_sha256 = select_rclone_asset(os_name, arch)
    bundle_path = _bundle_path(filename)
    if bundle_path is not None:
        data = bundle_path.read_bytes()
        source = str(bundle_path)
    else:
        data = _download_with_retries(url, attempts=attempts, timeout=timeout)
        source = url

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"rclone asset SHA mismatch: {source}\n"
            f"  expected: {expected_sha256}\n"
            f"  actual:   {actual_sha256}"
        )

    binary_data = _extract_binary(data, filename)
    binary_sha256 = hashlib.sha256(binary_data).hexdigest()
    binary_path = _binary_path(resolved_tool_dir)
    _write_binary_atomic(binary_path, binary_data)
    _write_sentinel_atomic(
        _sentinel_path(resolved_tool_dir),
        _sentinel_payload(os_name, arch, binary_path, binary_sha256),
    )
    _license_path(resolved_tool_dir).write_text(
        RCLONE_LICENSE_TEXT,
        encoding="utf-8",
    )
    return binary_path


__all__ = [
    "RCLONE_BUNDLE_ENV",
    "RCLONE_VERSION",
    "RCLONE_ZIP_SHA256",
    "check_rclone_ready",
    "ensure_rclone",
    "select_rclone_asset",
]

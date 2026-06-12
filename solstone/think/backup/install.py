# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Acquire and install the pinned restic binary for sol private backup."""

from __future__ import annotations

import bz2
import hashlib
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from solstone.think.backup.readiness import (
    RESTIC_BUNDLE_ENV,
    RESTIC_SCHEMA_VERSION,
    RESTIC_TOOL,
    RESTIC_VERSION,
    _binary_path,
    _license_path,
    _platform_info,
    _sentinel_path,
    _tool_dir,
    check_restic_ready,
    select_restic_asset,
)

RESTIC_LICENSE_TEXT = """BSD 2-Clause License

Copyright (c) 2014, Alexander Neumann <alexander@bumpern.de>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


def _bundle_path(asset_filename: str) -> Path | None:
    env_path = os.getenv(RESTIC_BUNDLE_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    bundled = Path(__file__).resolve().parent / "_bin" / asset_filename
    return bundled if bundled.exists() else None


def _fetch_url(url: str, *, timeout: float) -> bytes:
    if not url.startswith("https://"):
        raise RuntimeError(f"restic download URL must use HTTPS: {url}")
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _download_with_retries(url: str, *, attempts: int, timeout: float) -> bytes:
    if attempts < 1:
        raise ValueError("restic download attempts must be at least 1")
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return _fetch_url(url, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.25 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise RuntimeError("restic download failed without an error")


def _verify_bz2(data: bytes, expected_sha256: str, source: str) -> None:
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"restic asset SHA mismatch: {source}\n"
            f"  expected: {expected_sha256}\n"
            f"  actual:   {actual_sha256}"
        )


def _decompress_bz2(data: bytes, source: str) -> bytes:
    try:
        return bz2.decompress(data)
    except OSError as exc:
        raise RuntimeError(f"restic asset decompression failed: {source}") from exc


def _write_binary_atomic(binary_path: Path, data: bytes) -> None:
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=binary_path.parent,
            delete=False,
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        os.chmod(tmp_path, 0o755)
        tmp_path.rename(binary_path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _write_sentinel_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = Path(handle.name)
        tmp_path.rename(path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def _write_license(path: Path) -> None:
    path.write_text(RESTIC_LICENSE_TEXT, encoding="utf-8")


def _sentinel_payload(
    os_name: str,
    arch: str,
    binary_path: Path,
    binary_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RESTIC_SCHEMA_VERSION,
        "tool": RESTIC_TOOL,
        "version": RESTIC_VERSION,
        "sha256": binary_sha256,
        "platform": {"os": os_name, "arch": arch},
        "binary_path": str(binary_path),
    }


def _install_from_bz2(
    data: bytes,
    *,
    expected_sha256: str,
    source: str,
    tool_dir: Path,
    os_name: str,
    arch: str,
) -> Path:
    _verify_bz2(data, expected_sha256, source)
    binary_data = _decompress_bz2(data, source)
    binary_sha256 = hashlib.sha256(binary_data).hexdigest()
    tool_dir.mkdir(parents=True, exist_ok=True)
    binary_path = _binary_path(tool_dir)
    _write_binary_atomic(binary_path, binary_data)
    _write_sentinel_atomic(
        _sentinel_path(tool_dir),
        _sentinel_payload(os_name, arch, binary_path, binary_sha256),
    )
    _write_license(_license_path(tool_dir))
    return binary_path


def ensure_restic(
    *,
    force: bool = False,
    tool_dir: Path | None = None,
    attempts: int = 3,
    timeout: float = 30.0,
) -> Path:
    os_name, arch = _platform_info()
    resolved_tool_dir = tool_dir if tool_dir is not None else _tool_dir(os_name)
    if not force:
        ready_path = check_restic_ready(resolved_tool_dir)
        if ready_path is not None:
            return ready_path

    asset_filename, url, expected_sha256 = select_restic_asset(os_name, arch)
    bundle_path = _bundle_path(asset_filename)
    if bundle_path is not None:
        source = str(bundle_path)
        data = bundle_path.read_bytes()
    else:
        source = url
        data = _download_with_retries(url, attempts=attempts, timeout=timeout)

    return _install_from_bz2(
        data,
        expected_sha256=expected_sha256,
        source=source,
        tool_dir=resolved_tool_dir,
        os_name=os_name,
        arch=arch,
    )

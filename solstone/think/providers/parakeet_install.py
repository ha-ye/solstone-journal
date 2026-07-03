# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and inspect bundled parakeet.cpp provider artifacts.

This module is the sole writer for ``providers.bundled.parakeet`` install state.
It performs no network access at import time.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import random
import shutil
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from solstone.think import parakeet_readiness
from solstone.think.journal_config import read_journal_config, write_journal_config
from solstone.think.providers.install_state import (
    IN_FLIGHT_STATES,
    InstallStatus,
    bump_progress,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.utils import get_journal

PARAKEET_PROVIDER_NAME = "parakeet"
_PROBE_TIMEOUT_SECONDS = 10
_INSTALL_LOCK_TIMEOUT_SECONDS = 60.0 * 60.0
_INSTALL_LOCK_POLL_INTERVAL_SECONDS = 1.0
_PARAKEET_METADATA_KEYS = frozenset(
    {
        "binary_artifact_cpu",
        "binary_sha256_cpu",
        "binary_path_cpu",
        "binary_artifact_vulkan",
        "binary_sha256_vulkan",
        "binary_path_vulkan",
        "model_repo",
        "model_filename",
        "model_revision",
        "model_path",
        "model_sha256",
    }
)

PARAKEET_SERVER_PINS: dict[tuple[str, str], dict[str, str]] = {
    ("x86_64-unknown-linux-gnu", "vulkan"): {
        "filename": "parakeet-v0.4.0-bin-linux-vulkan-x64.tar.gz",
        "sha256": "12ee636ccb4a8b3c8f316f1f40c63f5aa4da178bf11563795b39385480ede87e",
    },
    ("x86_64-unknown-linux-gnu", "cpu"): {
        "filename": "parakeet-v0.4.0-bin-linux-cpu-x64.tar.gz",
        "sha256": "0846509eeb64fcb40e0ad28cd16b5bec5387e4799e08c85fb600b428bb306240",
    },
    ("aarch64-unknown-linux-gnu", "vulkan"): {
        "filename": "parakeet-v0.4.0-bin-linux-vulkan-arm64.tar.gz",
        "sha256": "b1e9251c9d247dffffc5e2db44bb993fb5ec40faab208ec83f7b89b8cc24efd0",
    },
    ("aarch64-unknown-linux-gnu", "cpu"): {
        "filename": "parakeet-v0.4.0-bin-linux-cpu-arm64.tar.gz",
        "sha256": "6634487a4cdbd3185e7a127aa4f22fbc49ec56421f7bfb14f450400260597773",
    },
}


@dataclass(frozen=True)
class ParakeetModelSpec:
    repo: str
    filename: str
    revision: str
    sha256: str
    size_bytes: int


PARAKEET_MODEL_SPEC = ParakeetModelSpec(
    repo=parakeet_readiness.PARAKEET_CPP_MODEL_REPO,
    filename=parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
    revision=parakeet_readiness.PARAKEET_CPP_MODEL_REVISION,
    sha256="4d69a4a6683f4f2d952bad794c1357ca6eb628027695b4699c5a9ad4cd07d757",
    size_bytes=940663680,
)


class ParakeetProviderError(RuntimeError):
    """Parakeet provider failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _validate_backend(backend: str) -> str:
    if backend not in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
        valid = ", ".join(parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS)
        raise ParakeetProviderError(
            "unsupported_backend", f"parakeet backend must be one of: {valid}"
        )
    return backend


def _pin_for_backend(artifact_key: str, backend: str) -> dict[str, str]:
    backend = _validate_backend(backend)
    pin = PARAKEET_SERVER_PINS.get((artifact_key, backend))
    if pin is None:
        raise ParakeetProviderError(
            "unsupported_platform",
            f"No pinned parakeet artifact for {artifact_key}/{backend}",
        )
    return pin


def parakeet_server_artifact_key() -> str:
    try:
        return parakeet_readiness.parakeet_cpp_artifact_key()
    except RuntimeError as exc:
        raise ParakeetProviderError("unsupported_platform", str(exc)) from exc


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return parakeet_readiness.parakeet_cpp_cache_root(root)


def binary_install_dir(backend: str, journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_binary_install_dir(
        cache_root(journal_path),
        parakeet_server_artifact_key(),
        _validate_backend(backend),
    )


def binary_path(backend: str, journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_binary_path(
        cache_root(journal_path),
        parakeet_server_artifact_key(),
        _validate_backend(backend),
    )


def model_dir(journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_model_dir(cache_root(journal_path))


def model_path(journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_model_path(cache_root(journal_path))


def install_hint() -> str:
    return "journal install-provider parakeet"


def _read_parakeet_status(
    journal_path: str | Path | None = None,
) -> InstallStatus:
    return read_install_status(
        scope="bundled", name=PARAKEET_PROVIDER_NAME, journal_path=journal_path
    )


def _write_parakeet_status(
    status: InstallStatus,
    journal_path: str | Path | None = None,
) -> InstallStatus:
    write_install_status(status, scope="bundled", journal_path=journal_path)
    return status


def _write_parakeet_metadata(
    updates: dict[str, str],
    journal_path: str | Path | None = None,
) -> None:
    unknown_keys = sorted(set(updates) - _PARAKEET_METADATA_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown parakeet install metadata key: {unknown_keys[0]}")

    config = read_journal_config(journal_path)
    slot = (
        config.setdefault("providers", {})
        .setdefault("bundled", {})
        .setdefault(PARAKEET_PROVIDER_NAME, {})
    )
    for key, value in updates.items():
        slot[key] = value
    write_journal_config(config, journal_path)


def _record_parakeet_progress(
    received: int,
    total: int | None,
    *,
    journal_path: str | Path | None = None,
) -> None:
    status = _read_parakeet_status(journal_path)
    if status["install_state"] not in IN_FLIGHT_STATES:
        return
    _write_parakeet_status(
        bump_progress(status, received=received, total=total),
        journal_path,
    )


def _install_lock_path(journal_path: str | Path | None = None) -> Path:
    return cache_root(journal_path) / "install"


@contextmanager
def _hold_install_lock(journal_path: str | Path | None = None) -> Iterator[None]:
    path = _install_lock_path(journal_path)
    lock_path = path.parent / f"{path.name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _INSTALL_LOCK_TIMEOUT_SECONDS
    lock_file = open(lock_path, "w", encoding="utf-8")
    try:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise ParakeetProviderError(
                        "install_lock_timeout",
                        f"Timed out waiting for parakeet install lock at {lock_path}",
                    ) from exc
                time.sleep(random.uniform(0.1, _INSTALL_LOCK_POLL_INTERVAL_SECONDS))
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _ready_status_if_installed(
    journal_path: str | Path | None = None,
) -> InstallStatus | None:
    try:
        parakeet_readiness.check_parakeet_cpp_files(
            cache_root(journal_path), parakeet_server_artifact_key()
        )
    except (ParakeetProviderError, RuntimeError):
        return None
    return _write_parakeet_status(
        transition_state(_read_parakeet_status(journal_path), new_state="installed"),
        journal_path,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str) -> None:
    actual = _sha256_file(path)
    if actual != expected:
        raise ParakeetProviderError(
            "sha256_mismatch",
            f"sha256 mismatch for {path.name}: expected {expected}, got {actual}",
        )


def _download_file(
    url: str,
    dest: Path,
    *,
    timeout_s: float = 600.0,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with httpx.stream("GET", url, timeout=timeout_s, follow_redirects=True) as response:
        response.raise_for_status()
        total_header = response.headers.get("content-length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        received = 0
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes():
                if chunk:
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
    tmp.rename(dest)


def _safe_extract_tarball(tarball: Path, dest: Path) -> None:
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(tarball, "r:*") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ParakeetProviderError(
                    "archive_path_traversal",
                    f"Unsafe tar member path: {member.name}",
                )
        archive.extractall(dest)


def _find_extracted_binary(dest: Path, binary_name: str) -> Path:
    direct = dest / binary_name
    if direct.exists():
        return direct
    matches = [path for path in dest.rglob(binary_name) if path.is_file()]
    if not matches:
        raise ParakeetProviderError(
            "binary_missing",
            f"Extracted archive did not contain {binary_name}",
        )
    if len(matches) > 1:
        matches.sort(key=lambda path: len(path.parts))
    return matches[0]


def _chmod_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def probe_binary_runnable(binary_path: str | Path) -> tuple[bool, str | None]:
    import subprocess

    try:
        completed = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_PROBE_TIMEOUT_SECONDS}s"
    except Exception as exc:
        return False, str(exc)

    if completed.returncode == 0:
        return True, None

    detail = (
        (completed.stderr or "").strip()
        or (completed.stdout or "").strip()
        or f"exited with status {completed.returncode}"
    )
    return False, detail


def _install_parakeet_server_unlocked(
    backend: str,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    backend = _validate_backend(backend)
    artifact_key = parakeet_server_artifact_key()
    pin = _pin_for_backend(artifact_key, backend)
    url = (
        "https://github.com/mudler/parakeet.cpp/releases/download/"
        f"{parakeet_readiness.PARAKEET_CPP_RELEASE_TAG}/{pin['filename']}"
    )
    install_dir = binary_install_dir(backend, journal_path)
    tarball = install_dir / pin["filename"]

    try:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="downloading"
            ),
            journal_path,
        )
        _write_parakeet_metadata(
            {f"binary_artifact_{backend}": pin["filename"]},
            journal_path,
        )
        _download_file(
            url,
            tarball,
            on_progress=lambda received, total: _record_parakeet_progress(
                received, total, journal_path=journal_path
            ),
        )
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="verifying"
            ),
            journal_path,
        )
        _verify_sha256(tarball, pin["sha256"])
        if install_dir.exists():
            for child in install_dir.iterdir():
                if child != tarball:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        _safe_extract_tarball(tarball, install_dir)
        extracted = _find_extracted_binary(
            install_dir, parakeet_readiness.PARAKEET_CPP_BINARY_NAME
        )
        final_path = binary_path(backend, journal_path)
        inner_dir = extracted.parent
        if inner_dir != install_dir:
            for item in inner_dir.iterdir():
                shutil.move(str(item), str(install_dir / item.name))
            inner_dir.rmdir()
        _chmod_executable(final_path)
        _write_parakeet_metadata(
            {
                f"binary_artifact_{backend}": pin["filename"],
                f"binary_sha256_{backend}": pin["sha256"],
                f"binary_path_{backend}": str(final_path),
            },
            journal_path,
        )
        return _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="installed"
            ),
            journal_path,
        )
    except Exception as exc:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path),
                new_state="failed",
                error=str(exc),
            ),
            journal_path,
        )
        raise


def install_parakeet_server(
    backend: str,
    *,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    with _hold_install_lock(journal_path):
        return _install_parakeet_server_unlocked(backend, journal_path)


def _install_model_unlocked(
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    spec = PARAKEET_MODEL_SPEC
    url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}"
    dest = model_path(journal_path)

    try:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="downloading"
            ),
            journal_path,
        )
        _write_parakeet_metadata(
            {
                "model_repo": spec.repo,
                "model_filename": spec.filename,
                "model_revision": spec.revision,
            },
            journal_path,
        )
        _download_file(
            url,
            dest,
            on_progress=lambda received, total: _record_parakeet_progress(
                received, total, journal_path=journal_path
            ),
        )
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="verifying"
            ),
            journal_path,
        )
        _verify_sha256(dest, spec.sha256)
        _write_parakeet_metadata(
            {
                "model_repo": spec.repo,
                "model_filename": spec.filename,
                "model_revision": spec.revision,
                "model_path": str(dest),
                "model_sha256": spec.sha256,
            },
            journal_path,
        )
        return _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="installed"
            ),
            journal_path,
        )
    except Exception as exc:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path),
                new_state="failed",
                error=str(exc),
            ),
            journal_path,
        )
        raise


def install_model(
    *,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    with _hold_install_lock(journal_path):
        return _install_model_unlocked(journal_path)


def install_parakeet(
    *,
    force: bool = False,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    with _hold_install_lock(journal_path):
        if not force:
            ready_status = _ready_status_if_installed(journal_path)
            if ready_status is not None:
                return ready_status
        for backend in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
            _install_parakeet_server_unlocked(backend, journal_path)
        return _install_model_unlocked(journal_path)


def inspect_readiness(journal_path: str | Path | None = None) -> dict[str, Any]:
    status = _read_parakeet_status(journal_path)
    cpu_path = binary_path("cpu", journal_path)
    vulkan_path = binary_path("vulkan", journal_path)
    gguf_path = model_path(journal_path)
    cpu_installed = cpu_path.exists() and os.access(cpu_path, os.X_OK)
    vulkan_installed = vulkan_path.exists() and os.access(vulkan_path, os.X_OK)
    model_installed = gguf_path.exists()
    return {
        "install_state": status["install_state"],
        "binary_installed": cpu_installed and vulkan_installed,
        "binary_cpu_installed": cpu_installed,
        "binary_vulkan_installed": vulkan_installed,
        "model_installed": model_installed,
        "binary_path_cpu": str(cpu_path),
        "binary_path_vulkan": str(vulkan_path),
        "model_path": str(gguf_path),
        "install_error": status["install_error"],
    }


def ensure_artifacts_installed(
    backend: str,
    *,
    journal_path: str | Path | None = None,
) -> tuple[Path, Path]:
    backend = _validate_backend(backend)
    readiness = inspect_readiness(journal_path)
    if not readiness["binary_installed"]:
        raise ParakeetProviderError(
            "binary_missing", "Parakeet server binaries are not installed."
        )
    if not readiness["model_installed"]:
        raise ParakeetProviderError("model_missing", "Parakeet model is not installed.")
    return Path(readiness[f"binary_path_{backend}"]), Path(readiness["model_path"])


__all__ = [
    "PARAKEET_MODEL_SPEC",
    "PARAKEET_PROVIDER_NAME",
    "PARAKEET_SERVER_PINS",
    "ParakeetModelSpec",
    "ParakeetProviderError",
    "binary_install_dir",
    "binary_path",
    "cache_root",
    "ensure_artifacts_installed",
    "inspect_readiness",
    "install_hint",
    "install_model",
    "install_parakeet",
    "install_parakeet_server",
    "model_dir",
    "model_path",
    "parakeet_server_artifact_key",
    "probe_binary_runnable",
]

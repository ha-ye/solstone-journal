# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and inspect bundled local provider artifacts.

This module is the sole writer for ``providers.bundled.local`` install state.
It performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from solstone.think.journal_config import read_journal_config, write_journal_config
from solstone.think.models import LOCAL_FLASH
from solstone.think.providers.local import (
    LOCAL_MODEL_SPECS,
    LocalModelSpec,
    LocalProviderError,
    normalize_model_id,
)
from solstone.think.utils import get_journal, now_ms

LOCAL_PROVIDER_NAME = "local"
STATE_IDLE = "idle"
STATE_DOWNLOADING = "downloading"
STATE_VERIFYING = "verifying"
STATE_INSTALLED = "installed"
STATE_FAILED = "failed"

LLAMA_SERVER_PINS: dict[str, dict[str, str]] = {
    "aarch64-apple-darwin": {
        "release_tag": "b9291",
        "filename": "llama-b9291-bin-macos-arm64.tar.gz",
        "sha256": "0e985f87dd71f96a9cb9ebc3ad26f8388030342d000e7e82d4a38d14913373ff",
        "binary_name": "llama-server",
    },
    "x86_64-unknown-linux-gnu": {
        "release_tag": "b9291",
        "filename": "llama-b9291-bin-ubuntu-x64.tar.gz",
        "sha256": "8cb79eb596cc5cc15a6089ceadaa2723e3d75c1e7b37cfb9977ad1d4dc4a41eb",
        "binary_name": "llama-server",
    },
}


def llama_server_artifact_key() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x64"}:
        machine = "x86_64"
    elif machine == "arm64":
        machine = "aarch64"

    if sys.platform == "darwin":
        return f"{machine}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"{machine}-unknown-linux-gnu"
    return f"{machine}-{sys.platform}"


def pin_for_current_platform() -> dict[str, str]:
    key = llama_server_artifact_key()
    pin = LLAMA_SERVER_PINS.get(key)
    if not pin:
        raise LocalProviderError(
            "unsupported_platform",
            f"No pinned llama-server artifact for platform {key}",
        )
    return pin


def cache_root() -> Path:
    return Path(get_journal()) / "cache" / "providers" / LOCAL_PROVIDER_NAME


def binary_install_dir(
    artifact_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> Path:
    artifact_key = artifact_key or llama_server_artifact_key()
    pin = pin or pin_for_current_platform()
    return cache_root() / "bin" / artifact_key / pin["release_tag"]


def binary_path_for_pin(
    artifact_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> Path:
    pin = pin or pin_for_current_platform()
    return binary_install_dir(artifact_key, pin) / pin["binary_name"]


def model_dir(model_id: str) -> Path:
    safe_id = model_id.replace("/", "__")
    return cache_root() / "models" / safe_id


def model_path(model_id: str) -> Path:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    return model_dir(spec.model_id) / spec.filename


def read_install_state() -> dict[str, Any]:
    config = read_journal_config()
    record = config.get("providers", {}).get("bundled", {}).get(LOCAL_PROVIDER_NAME, {})
    if not isinstance(record, dict):
        record = {}
    return {
        "state": STATE_IDLE,
        "last_transition_at": None,
        "install_error": None,
        **record,
    }


def write_install_state(record: dict[str, Any]) -> dict[str, Any]:
    config = read_journal_config()
    providers = config.setdefault("providers", {})
    bundled = providers.setdefault("bundled", {})
    existing = bundled.get(LOCAL_PROVIDER_NAME, {})
    if not isinstance(existing, dict):
        existing = {}
    updated = {
        **existing,
        **record,
        "last_transition_at": now_ms(),
    }
    bundled[LOCAL_PROVIDER_NAME] = updated
    write_journal_config(config)
    return updated


def _transition(state: str, **updates: Any) -> dict[str, Any]:
    return write_install_state({"state": state, **updates})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str) -> None:
    actual = _sha256_file(path)
    if actual != expected:
        raise LocalProviderError(
            "sha256_mismatch",
            f"sha256 mismatch for {path.name}: expected {expected}, got {actual}",
        )


def _download_file(url: str, dest: Path, *, timeout_s: float = 600.0) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with httpx.stream("GET", url, timeout=timeout_s, follow_redirects=True) as response:
        response.raise_for_status()
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes():
                if chunk:
                    handle.write(chunk)
    tmp.replace(dest)


def _safe_extract_tarball(tarball: Path, dest: Path) -> None:
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(tarball, "r:*") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise LocalProviderError(
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
        raise LocalProviderError(
            "binary_missing",
            f"Extracted archive did not contain {binary_name}",
        )
    if len(matches) > 1:
        matches.sort(key=lambda path: len(path.parts))
    return matches[0]


def _chmod_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _clear_macos_quarantine(path: Path) -> None:
    if sys.platform != "darwin":
        return
    import subprocess

    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def install_llama_server() -> dict[str, Any]:
    artifact_key = llama_server_artifact_key()
    pin = pin_for_current_platform()
    url = (
        "https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{pin['release_tag']}/{pin['filename']}"
    )
    install_dir = binary_install_dir(artifact_key, pin)
    tarball = install_dir / pin["filename"]

    try:
        _transition(
            STATE_DOWNLOADING, binary_artifact=pin["filename"], install_error=None
        )
        _download_file(url, tarball)
        _transition(STATE_VERIFYING, binary_artifact=pin["filename"])
        _verify_sha256(tarball, pin["sha256"])
        if install_dir.exists():
            for child in install_dir.iterdir():
                if child != tarball:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        _safe_extract_tarball(tarball, install_dir)
        extracted = _find_extracted_binary(install_dir, pin["binary_name"])
        final_path = binary_path_for_pin(artifact_key, pin)
        if extracted != final_path:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extracted, final_path)
        _chmod_executable(final_path)
        _clear_macos_quarantine(final_path)
        return _transition(
            STATE_INSTALLED,
            binary_artifact=pin["filename"],
            binary_sha256=pin["sha256"],
            binary_path=str(final_path),
            install_error=None,
        )
    except Exception as exc:
        _transition(STATE_FAILED, install_error=str(exc))
        raise


def install_model(model_id: str = LOCAL_FLASH) -> dict[str, Any]:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}"
    dest = model_path(spec.model_id)

    try:
        _transition(STATE_DOWNLOADING, model_id=spec.model_id, install_error=None)
        _download_file(url, dest)
        _transition(STATE_VERIFYING, model_id=spec.model_id)
        _verify_sha256(dest, spec.sha256)
        return _transition(
            STATE_INSTALLED,
            model_id=spec.model_id,
            model_path=str(dest),
            model_sha256=spec.sha256,
            install_error=None,
        )
    except Exception as exc:
        _transition(STATE_FAILED, install_error=str(exc))
        raise


def install_local(model_id: str = LOCAL_FLASH) -> dict[str, Any]:
    install_llama_server()
    return install_model(model_id)


def _ram_sufficient(spec: LocalModelSpec) -> bool:
    try:
        import psutil

        return int(psutil.virtual_memory().total) >= spec.min_ram_bytes
    except Exception:
        return True


def inspect_readiness(model_id: str | None = None) -> dict[str, Any]:
    state = read_install_state()
    selected_model = normalize_model_id(
        model_id or state.get("model_id") or LOCAL_FLASH
    )
    spec = LOCAL_MODEL_SPECS[selected_model]
    binary_path = Path(state.get("binary_path") or binary_path_for_pin())
    gguf_path = Path(state.get("model_path") or model_path(selected_model))
    ram_sufficient = _ram_sufficient(spec)
    return {
        "state": state.get("state", STATE_IDLE),
        "binary_installed": binary_path.exists() and os.access(binary_path, os.X_OK),
        "model_installed": gguf_path.exists(),
        "ram_sufficient": ram_sufficient,
        "binary_path": str(binary_path),
        "model_path": str(gguf_path),
        "model_id": selected_model,
        "install_error": state.get("install_error"),
    }


def ensure_artifacts_installed(model_id: str) -> tuple[Path, Path]:
    selected_model = normalize_model_id(model_id)
    readiness = inspect_readiness(selected_model)
    if not readiness["ram_sufficient"]:
        raise LocalProviderError(
            "ram_insufficient",
            "This computer does not have enough memory for the selected local model.",
        )
    if not readiness["binary_installed"]:
        raise LocalProviderError("binary_missing", "Local runtime is not installed.")
    if not readiness["model_installed"]:
        raise LocalProviderError(
            "model_missing", "Local model files are not installed."
        )
    return Path(readiness["binary_path"]), Path(readiness["model_path"])


__all__ = [
    "LLAMA_SERVER_PINS",
    "STATE_IDLE",
    "STATE_DOWNLOADING",
    "STATE_VERIFYING",
    "STATE_INSTALLED",
    "STATE_FAILED",
    "llama_server_artifact_key",
    "pin_for_current_platform",
    "binary_path_for_pin",
    "model_path",
    "read_install_state",
    "write_install_state",
    "install_llama_server",
    "install_model",
    "install_local",
    "inspect_readiness",
    "ensure_artifacts_installed",
]

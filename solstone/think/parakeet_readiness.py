# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Examine-only parakeet readiness checks shared by doctor and provider setup.

Stdlib-only by contract. Must NOT import solstone.observe.* (inference),
installer modules, any third-party package, or anything that downloads,
installs, or loads inference code. doctor depends on this
boundary to stay fast and diagnostic-only — do not add heavy imports here.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

BACKEND = "parakeet"
MODEL_VERSION = "v3"
MAC_CACHE_DIR = Path.home() / "Library/Application Support/solstone/parakeet/models"
# FluidAudio's downloadAndLoad(to:) treats the passed dir as the parent and writes into <parent>/<repo-folder>; verified empirically against FluidAudio v0.14.0 v3 on Apple Silicon (helper invocation against a fresh cache dir wrote to <parent>/parakeet-tdt-0.6b-v3/).
MAC_FLUIDAUDIO_REPO_NAME = "parakeet-tdt-0.6b-v3"
MAC_SENTINEL = MAC_CACHE_DIR / ".install-complete"
LINUX_HUB_DIR = Path.home() / ".cache/huggingface/hub"
LINUX_MODEL_DIR = LINUX_HUB_DIR / "models--istupakov--parakeet-tdt-0.6b-v3-onnx"
LINUX_SENTINEL = LINUX_HUB_DIR / ".solstone-install-complete"
LINUX_MODEL_FILES = (
    "encoder-model.onnx",
    "decoder_joint-model.onnx",
    "config.json",
    "vocab.txt",
)
LINUX_MIN_CACHE_BYTES = 2_400_000_000
MAC_MODEL_FILES = (
    "Encoder.mlmodelc/weights/weight.bin",
    "Decoder.mlmodelc/weights/weight.bin",
    "JointDecision.mlmodelc/weights/weight.bin",
    "Preprocessor.mlmodelc/weights/weight.bin",
)
PARAKEET_CPP_RELEASE_TAG = "v0.4.0"
PARAKEET_CPP_BINARY_NAME = "parakeet-server"
PARAKEET_CPP_BINARY_BACKENDS = ("cpu", "vulkan")
PARAKEET_CPP_MODEL_REPO = "mudler/parakeet-cpp-gguf"
PARAKEET_CPP_MODEL_FILENAME = "tdt-0.6b-v3-q8_0.gguf"
PARAKEET_CPP_MODEL_REVISION = "bf0af9f425fa01809cadec671b3cb672709d13e9"


def _platform_info() -> tuple[str, str]:
    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    return os_name, platform.machine().lower()


def parakeet_cpp_artifact_key(
    os_name: str | None = None, arch: str | None = None
) -> str:
    if os_name is None or arch is None:
        detected_os, detected_arch = _platform_info()
        os_name = os_name or detected_os
        arch = arch or detected_arch

    if os_name != "linux":
        raise RuntimeError(f"parakeet-cpp is unsupported on {os_name}/{arch}")

    normalized_arch = arch.lower()
    if normalized_arch in {"amd64", "x64", "x86_64"}:
        return "x86_64-unknown-linux-gnu"
    if normalized_arch in {"arm64", "aarch64"}:
        return "aarch64-unknown-linux-gnu"
    raise RuntimeError(f"parakeet-cpp is unsupported on {os_name}/{arch}")


def parakeet_cpp_cache_root(journal_path: Path) -> Path:
    return journal_path / "cache" / "providers" / "parakeet"


def parakeet_cpp_binary_install_dir(
    cache_root: Path, artifact_key: str, backend: str
) -> Path:
    return cache_root / "bin" / artifact_key / backend / PARAKEET_CPP_RELEASE_TAG


def parakeet_cpp_binary_path(cache_root: Path, artifact_key: str, backend: str) -> Path:
    return (
        parakeet_cpp_binary_install_dir(cache_root, artifact_key, backend)
        / PARAKEET_CPP_BINARY_NAME
    )


def parakeet_cpp_model_dir(cache_root: Path) -> Path:
    return (
        cache_root
        / "models"
        / PARAKEET_CPP_MODEL_REPO.replace("/", "__")
        / PARAKEET_CPP_MODEL_REVISION
    )


def parakeet_cpp_model_path(cache_root: Path) -> Path:
    return parakeet_cpp_model_dir(cache_root) / PARAKEET_CPP_MODEL_FILENAME


def check_parakeet_cpp_files(cache_root: Path, artifact_key: str) -> dict[str, Path]:
    paths = {
        "binary_cpu": parakeet_cpp_binary_path(cache_root, artifact_key, "cpu"),
        "binary_vulkan": parakeet_cpp_binary_path(cache_root, artifact_key, "vulkan"),
        "model": parakeet_cpp_model_path(cache_root),
    }
    for key in ("binary_cpu", "binary_vulkan"):
        path = paths[key]
        if not path.is_file():
            raise RuntimeError(f"parakeet-cpp check failed: {key} missing at {path}")
        if not os.access(path, os.X_OK):
            raise RuntimeError(
                f"parakeet-cpp check failed: {key} not executable at {path}"
            )
    model_path = paths["model"]
    if not model_path.is_file():
        raise RuntimeError(f"parakeet-cpp check failed: model missing at {model_path}")
    return paths


def _sentinel_path(variant: str) -> Path:
    return MAC_SENTINEL if variant == "coreml" else LINUX_SENTINEL


def _cache_dir(variant: str) -> Path:
    return MAC_CACHE_DIR if variant == "coreml" else LINUX_MODEL_DIR


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
    variant: str,
) -> Path | None:
    if payload is None:
        return None
    platform_info = payload.get("platform")
    if not isinstance(platform_info, dict):
        return None
    if payload.get("schema_version") != 1 or payload.get("backend") != BACKEND:
        return None
    if (
        payload.get("variant") != variant
        or payload.get("model_version") != MODEL_VERSION
        or payload.get("quantization") != "fp32"
    ):
        return None
    if platform_info.get("os") != os_name or platform_info.get("arch") != arch:
        return None
    cache_dir = payload.get("cache_dir")
    if not isinstance(cache_dir, str) or not cache_dir:
        return None
    if variant == "coreml" and not payload.get("fluidaudio_version"):
        return None
    resolved = Path(cache_dir).expanduser()
    return resolved if resolved.exists() else None


def _verify_linux_cache(cache_dir: Path) -> bool:
    snapshots_dir = cache_dir / "snapshots"
    if not snapshots_dir.is_dir():
        return False
    for child in snapshots_dir.iterdir():
        if not child.is_dir():
            continue
        if not all(
            (child / relative_path).is_file() for relative_path in LINUX_MODEL_FILES
        ):
            continue
        total_bytes = sum(
            path.stat().st_size for path in child.rglob("*") if path.is_file()
        )
        if total_bytes >= LINUX_MIN_CACHE_BYTES:
            return True
    return False


def _verify_mac_cache(cache_dir: Path) -> bool:
    return all(
        (cache_dir.parent / MAC_FLUIDAUDIO_REPO_NAME / relative_path).is_file()
        for relative_path in MAC_MODEL_FILES
    )


def _verify_variant_cache(variant: str, cache_dir: Path) -> bool:
    if variant in {"cpu", "cuda"}:
        return _verify_linux_cache(cache_dir)
    return _verify_mac_cache(cache_dir)


def _check_parakeet_ready(
    os_name: str,
    arch: str,
    variant: str,
    sentinel_path: Path,
) -> Path:
    ready_cache = _sentinel_ready(
        _load_sentinel(sentinel_path),
        os_name,
        arch,
        variant,
    )
    if ready_cache is None:
        raise RuntimeError(
            f"parakeet check failed: sentinel not ready at {sentinel_path}"
        )
    if not _verify_variant_cache(variant, ready_cache):
        raise RuntimeError(
            f"parakeet check failed: cache verification failed at {ready_cache}"
        )
    return ready_cache

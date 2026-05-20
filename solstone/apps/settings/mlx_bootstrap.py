# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""MLX first-run model bootstrap helpers for Settings."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import platform
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import huggingface_hub
import psutil
from huggingface_hub import constants
from huggingface_hub.file_download import repo_folder_name

from solstone.think.providers.mlx import (
    _MLX_MODEL_REGISTRY,
    is_mlx_available_for_model,
)

logger = logging.getLogger(__name__)

BootstrapStateName = Literal["idle", "downloading", "verifying", "installed", "failed"]
_STALL_SECONDS = 60.0
_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass
class MlxBootstrapState:
    state: BootstrapStateName = "idle"
    received_bytes: int = 0
    total_bytes: int = 0
    started_at: float | None = None
    last_progress_at: float | None = None
    message: str | None = None
    thread: threading.Thread | None = None


class MlxBootstrapUnavailableError(RuntimeError):
    """Raised when the host cannot run the MLX provider."""


class MlxBootstrapStartError(RuntimeError):
    """Raised when the bootstrap worker could not be started."""


class MlxVerificationError(RuntimeError):
    """Raised when a downloaded file fails sha256 verification."""


_STATES: dict[str, MlxBootstrapState] = {
    name: MlxBootstrapState() for name in _MLX_MODEL_REGISTRY
}
_STATES_LOCK = threading.Lock()


def _snapshot_dir(model: str) -> Path:
    spec = _MLX_MODEL_REGISTRY[model]
    repo_folder = repo_folder_name(
        repo_id=spec.repo,
        repo_type="model",
    )
    return Path(constants.HF_HUB_CACHE) / repo_folder / "snapshots" / spec.revision


def _safetensors_paths(model: str) -> list[str]:
    snapshot_dir = _snapshot_dir(model)
    index_path = snapshot_dir / "model.safetensors.index.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    weight_map = data.get("weight_map")
    if not isinstance(weight_map, dict):
        raise ValueError("model.safetensors.index.json missing weight_map")
    paths = sorted({str(path) for path in weight_map.values() if str(path)})
    if not paths:
        raise ValueError("model.safetensors.index.json has no safetensors paths")
    return paths


def check_model_present(model: str) -> bool:
    """Return whether the pinned MLX snapshot is structurally present."""
    snapshot_dir = _snapshot_dir(model)
    index_path = snapshot_dir / "model.safetensors.index.json"
    if not snapshot_dir.is_dir() or not index_path.is_file():
        return False
    try:
        for rel_path in _safetensors_paths(model):
            file_path = snapshot_dir / rel_path
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def _is_package_installed(package: str) -> bool:
    if package in sys.modules and sys.modules[package] is None:
        return False
    return importlib.util.find_spec(package) is not None


def get_availability_payload(model: str) -> dict[str, bool | float | int | str]:
    """Return the MLX availability payload used by Settings."""
    spec = _MLX_MODEL_REGISTRY[model]
    ok, reason = is_mlx_available_for_model(spec)
    model_present = check_model_present(model)
    available = ok and model_present
    if ok and not model_present:
        reason = "model snapshot not present"
    elif available:
        reason = ""

    total_memory_gb = round(psutil.virtual_memory().total / 1024**3, 1)
    return {
        "model": model,
        "is_apple_silicon": platform.system() == "Darwin"
        and platform.machine() == "arm64",
        "total_memory_gb": total_memory_gb,
        "mlx_installed": _is_package_installed("mlx_vlm"),
        "min_ram_gb": spec.min_ram_bytes // 1024**3,
        "model_present": model_present,
        "available": available,
        "reason": reason,
    }


def _serialize_state_locked(model: str) -> dict[str, int | str | None]:
    state = _STATES[model]
    return {
        "state": state.state,
        "received_bytes": int(state.received_bytes),
        "total_bytes": int(state.total_bytes),
        "message": state.message,
    }


def _mark_installed_locked(model: str) -> None:
    state = _STATES[model]
    state.state = "installed"
    state.message = None
    state.thread = None
    state.last_progress_at = time.monotonic()


def _mark_downloading_locked(model: str, thread: threading.Thread, now: float) -> None:
    state = _STATES[model]
    state.state = "downloading"
    state.received_bytes = 0
    state.total_bytes = 0
    state.started_at = now
    state.last_progress_at = now
    state.message = None
    state.thread = thread


def _mark_verifying(model: str) -> None:
    now = time.monotonic()
    with _STATES_LOCK:
        state = _STATES[model]
        if state.state != "downloading":
            return
        state.state = "verifying"
        state.received_bytes = 0
        state.total_bytes = 0
        state.last_progress_at = now
        state.message = None


def _mark_failed_locked(model: str, message: str) -> None:
    state = _STATES[model]
    state.state = "failed"
    state.message = message
    state.last_progress_at = time.monotonic()


def _add_progress(model: str, received_delta: int, total: int | None = None) -> None:
    if received_delta < 0:
        received_delta = 0
    now = time.monotonic()
    with _STATES_LOCK:
        state = _STATES[model]
        state.received_bytes += received_delta
        if total is not None:
            state.total_bytes = max(0, int(total))
        state.last_progress_at = now


def _set_progress_total(model: str, total: int) -> None:
    with _STATES_LOCK:
        state = _STATES[model]
        state.total_bytes = max(0, int(total))
        state.last_progress_at = time.monotonic()


def _set_verify_progress(model: str, received_bytes: int, total_bytes: int) -> None:
    with _STATES_LOCK:
        state = _STATES[model]
        if state.state != "verifying":
            return
        state.received_bytes = max(0, int(received_bytes))
        state.total_bytes = max(0, int(total_bytes))
        state.last_progress_at = time.monotonic()


def _observe_stall_locked(model: str, now: float) -> None:
    state = _STATES[model]
    if state.state not in ("downloading", "verifying"):
        return
    thread = state.thread
    if thread is not None and thread.is_alive():
        return
    last_progress = state.last_progress_at or state.started_at
    if last_progress is None or now - last_progress <= _STALL_SECONDS:
        return
    _mark_failed_locked(model, f"{state.state} stalled with no progress")


def get_state(model: str) -> dict[str, int | str | None]:
    """Return the serialized bootstrap state, applying stall detection."""
    with _STATES_LOCK:
        _observe_stall_locked(model, time.monotonic())
        return _serialize_state_locked(model)


def _remote_safetensors_metadata(
    model: str, paths: list[str]
) -> dict[str, tuple[str, int]]:
    spec = _MLX_MODEL_REGISTRY[model]
    wanted = set(paths)
    found: dict[str, tuple[str, int]] = {}
    api = huggingface_hub.HfApi()
    for entry in api.list_repo_tree(
        repo_id=spec.repo,
        revision=spec.revision,
        repo_type="model",
        recursive=True,
    ):
        if not isinstance(entry, huggingface_hub.RepoFile) or entry.path not in wanted:
            continue
        if entry.lfs is None:
            raise MlxVerificationError(f"missing LFS sha256 for {entry.path}")
        found[entry.path] = (entry.lfs.sha256, int(entry.lfs.size))
    missing = sorted(wanted - set(found))
    if missing:
        raise MlxVerificationError(f"missing published sha256 for {missing[0]}")
    return found


def _verify_safetensors_sha256_hashes(model: str) -> None:
    snapshot_dir = _snapshot_dir(model)
    safetensors_paths = _safetensors_paths(model)
    metadata = _remote_safetensors_metadata(model, safetensors_paths)
    total_bytes = sum(size for _sha, size in metadata.values())
    hashed_total = 0
    _set_verify_progress(model, 0, total_bytes)

    for rel_path in safetensors_paths:
        expected_sha, _expected_size = metadata[rel_path]
        file_path = snapshot_dir / rel_path
        digest = hashlib.sha256()
        file_received = 0
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
                file_received += len(chunk)
                _set_verify_progress(model, hashed_total + file_received, total_bytes)
        actual_sha = digest.hexdigest()
        if actual_sha != expected_sha:
            raise MlxVerificationError(f"sha256 mismatch for {rel_path}")
        hashed_total += file_received
        _set_verify_progress(model, hashed_total, total_bytes)


class _BootstrapTqdm:
    _model = next(iter(_MLX_MODEL_REGISTRY))

    def __init__(self, *args, **kwargs):
        self._track_bytes = kwargs.get("unit") == "B"
        self._total = int(kwargs.get("total") or 0)
        if self._track_bytes:
            _set_progress_total(self._model, self._total)
            initial = int(kwargs.get("initial") or 0)
            if initial:
                _add_progress(self._model, initial)

    @property
    def total(self) -> int:
        return self._total

    @total.setter
    def total(self, value: int | float | None) -> None:
        self._total = int(value or 0)
        if self._track_bytes:
            _set_progress_total(self._model, self._total)

    def update(self, n: int | float | None = 1) -> None:
        if self._track_bytes:
            _add_progress(self._model, int(n or 0))

    def refresh(self) -> None:
        return None

    def set_description(self, _description: str) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


def start_bootstrap(model: str) -> tuple[dict[str, str], int]:
    """Start the MLX model bootstrap worker if needed."""
    ok, reason = is_mlx_available_for_model(_MLX_MODEL_REGISTRY[model])
    if not ok:
        raise MlxBootstrapUnavailableError(reason)

    present = check_model_present(model)
    with _STATES_LOCK:
        state = _STATES[model]
        if (
            state.state in ("downloading", "verifying")
            and state.thread
            and state.thread.is_alive()
        ):
            return {"state": state.state}, 200
        retry_after_failed = state.state == "failed"
        if present and not retry_after_failed:
            _mark_installed_locked(model)
            return {"state": "installed"}, 200

    with _STATES_LOCK:
        state = _STATES[model]
        if (
            state.state in ("downloading", "verifying")
            and state.thread
            and state.thread.is_alive()
        ):
            return {"state": state.state}, 200
        retry_after_failed = retry_after_failed or state.state == "failed"
        if not retry_after_failed and check_model_present(model):
            _mark_installed_locked(model)
            return {"state": "installed"}, 200
        try:
            thread = threading.Thread(
                target=_run_bootstrap_worker,
                args=(model,),
                name=f"mlx-model-bootstrap-{model}",
                daemon=True,
            )
        except Exception as exc:
            _mark_failed_locked(model, str(exc))
            raise MlxBootstrapStartError(str(exc)) from exc
        _mark_downloading_locked(model, thread, time.monotonic())

    try:
        thread.start()
    except Exception as exc:
        with _STATES_LOCK:
            _mark_failed_locked(model, str(exc))
        raise MlxBootstrapStartError(str(exc)) from exc
    return {"state": "downloading"}, 202


def _run_bootstrap_worker(model: str) -> None:
    spec = _MLX_MODEL_REGISTRY[model]

    class _ModelBoundTqdm(_BootstrapTqdm):
        _model = model

    try:
        # v1.15.0 resumes via .incomplete files automatically; no resume_download kwarg.
        huggingface_hub.snapshot_download(
            repo_id=spec.repo,
            revision=spec.revision,
            tqdm_class=_ModelBoundTqdm,
        )
        _mark_verifying(model)
        _verify_safetensors_sha256_hashes(model)
        with _STATES_LOCK:
            if _STATES[model].state == "verifying":
                _mark_installed_locked(model)
    except Exception as exc:
        logger.exception("MLX model bootstrap failed")
        with _STATES_LOCK:
            _mark_failed_locked(model, str(exc))

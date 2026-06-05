# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Memory and disk helpers for bundled local provider readiness."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import psutil

MLX_SINGLE_VLM_RESIDENT_BYTES = int(12.5 * 1024**3)
MLX_HEADROOM_BYTES = int(0.5 * 1024**3)
MLX_AVAILABLE_FLOOR_BYTES = MLX_SINGLE_VLM_RESIDENT_BYTES + MLX_HEADROOM_BYTES


@dataclass(frozen=True)
class MemoryVerdict:
    available_bytes: int | None
    required_bytes: int
    severity: str


def read_available_bytes() -> int | None:
    """Return available memory bytes, or None when detection is unreliable."""
    try:
        memory = psutil.virtual_memory()
        available = int(memory.available)
        total = int(memory.total)
    except Exception:
        return None
    if available <= 0 or total <= 0 or available > total:
        return None
    return available


def read_total_bytes() -> int | None:
    """Return total memory bytes for legacy display-only payloads."""
    try:
        total = int(psutil.virtual_memory().total)
    except Exception:
        return None
    return total if total > 0 else None


def assess_memory(required_bytes: int, *, block_below_floor: bool) -> MemoryVerdict:
    available = read_available_bytes()
    if available is None:
        severity = "warning"
    elif available >= required_bytes:
        severity = "ok"
    elif block_below_floor:
        severity = "blocked"
    else:
        severity = "warning"
    return MemoryVerdict(
        available_bytes=available,
        required_bytes=required_bytes,
        severity=severity,
    )


def gb(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 1024**3, 1)


def gb_label(value: int) -> str:
    value_gb = gb(value)
    assert value_gb is not None
    return f"{value_gb:g}"


def free_bytes(target: Path) -> int:
    usage_root = target
    while not usage_root.exists() and usage_root != usage_root.parent:
        usage_root = usage_root.parent
    return int(shutil.disk_usage(usage_root).free)


__all__ = [
    "MLX_AVAILABLE_FLOOR_BYTES",
    "MLX_HEADROOM_BYTES",
    "MLX_SINGLE_VLM_RESIDENT_BYTES",
    "MemoryVerdict",
    "assess_memory",
    "free_bytes",
    "gb",
    "gb_label",
    "read_available_bytes",
    "read_total_bytes",
]

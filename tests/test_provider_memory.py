# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from types import SimpleNamespace

import pytest

from solstone.think.providers import memory


def test_mlx_available_floor_constants_fit_single_vlm_footprint() -> None:
    assert memory.MLX_SINGLE_VLM_RESIDENT_BYTES == int(12.5 * 1024**3)
    assert memory.MLX_HEADROOM_BYTES == int(0.5 * 1024**3)
    assert memory.MLX_AVAILABLE_FLOOR_BYTES == 13 * 1024**3
    assert memory.MLX_AVAILABLE_FLOOR_BYTES < 2 * memory.MLX_SINGLE_VLM_RESIDENT_BYTES


def test_assess_memory_blocks_or_warns_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = memory.MLX_AVAILABLE_FLOOR_BYTES
    monkeypatch.setattr(memory, "read_available_bytes", lambda: required)

    verdict = memory.assess_memory(required, block_below_floor=True)

    assert verdict.severity == "ok"
    assert verdict.available_bytes == required

    monkeypatch.setattr(memory, "read_available_bytes", lambda: required - 1)

    assert memory.assess_memory(required, block_below_floor=True).severity == "blocked"
    assert memory.assess_memory(required, block_below_floor=False).severity == "warning"


def test_assess_memory_warns_when_detection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory, "read_available_bytes", lambda: None)

    verdict = memory.assess_memory(8 * 1024**3, block_below_floor=True)

    assert verdict.available_bytes is None
    assert verdict.severity == "warning"


def test_read_available_bytes_returns_none_when_psutil_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail():
        raise RuntimeError("no memory data")

    monkeypatch.setattr(memory.psutil, "virtual_memory", fail)

    assert memory.read_available_bytes() is None


@pytest.mark.parametrize(
    "payload",
    [
        SimpleNamespace(available=0, total=100),
        SimpleNamespace(available=-1, total=100),
        SimpleNamespace(available=101, total=100),
    ],
)
def test_read_available_bytes_rejects_nonsense(
    monkeypatch: pytest.MonkeyPatch,
    payload: SimpleNamespace,
) -> None:
    monkeypatch.setattr(memory.psutil, "virtual_memory", lambda: payload)

    assert memory.read_available_bytes() is None

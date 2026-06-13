# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.observe.transcribe import resource


def test_stt_floor_constants_are_exact_gibibytes() -> None:
    assert resource.STT_LOCAL_FLOOR_LINUX_BYTES == 4 * 1024**3
    assert resource.STT_LOCAL_FLOOR_DARWIN_BYTES == 2 * 1024**3


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", resource.STT_LOCAL_FLOOR_DARWIN_BYTES),
        ("Linux", "x86_64", resource.STT_LOCAL_FLOOR_LINUX_BYTES),
        ("Linux", "aarch64", resource.STT_LOCAL_FLOOR_LINUX_BYTES),
        ("Windows", "AMD64", None),
    ],
)
def test_stt_local_floor_bytes_platform_mapping(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: int | None,
) -> None:
    monkeypatch.setattr(resource.platform, "system", lambda: system)
    monkeypatch.setattr(resource.platform, "machine", lambda: machine)

    assert resource.stt_local_floor_bytes() == expected


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "parakeet"),
        ("Linux", "x86_64", "parakeet"),
        ("Linux", "aarch64", "whisper"),
        ("Windows", "AMD64", None),
    ],
)
def test_local_stt_backend_platform_mapping(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(resource.platform, "system", lambda: system)
    monkeypatch.setattr(resource.platform, "machine", lambda: machine)

    assert resource.local_stt_backend() == expected


@pytest.mark.parametrize(
    (
        "available_bytes",
        "google_key_present",
        "floor_bytes",
        "local_backend",
        "expected",
    ),
    [
        (4 * 1024**3, False, 4 * 1024**3, "parakeet", "parakeet"),
        (5 * 1024**3, True, 4 * 1024**3, "parakeet", "parakeet"),
        (3 * 1024**3, True, 4 * 1024**3, "parakeet", "gemini"),
        (None, True, 4 * 1024**3, "parakeet", "gemini"),
        (3 * 1024**3, True, None, None, "gemini"),
        (3 * 1024**3, False, 4 * 1024**3, "parakeet", resource.STT_SURFACE),
        (None, False, 4 * 1024**3, "parakeet", resource.STT_SURFACE),
        (3 * 1024**3, False, None, None, resource.STT_SURFACE),
        (4 * 1024**3, False, 4 * 1024**3, "whisper", "whisper"),
        (3 * 1024**3, True, 4 * 1024**3, "whisper", "gemini"),
    ],
)
def test_select_stt_backend_matrix(
    available_bytes: int | None,
    google_key_present: bool,
    floor_bytes: int | None,
    local_backend: str | None,
    expected: str,
) -> None:
    assert (
        resource.select_stt_backend(
            available_bytes,
            google_key_present=google_key_present,
            floor_bytes=floor_bytes,
            local_backend=local_backend,
        )
        == expected
    )


def test_select_stt_backend_is_deterministic() -> None:
    args = {
        "available_bytes": 3 * 1024**3,
        "google_key_present": True,
        "floor_bytes": 4 * 1024**3,
        "local_backend": "parakeet",
    }

    assert resource.select_stt_backend(**args) == resource.select_stt_backend(**args)

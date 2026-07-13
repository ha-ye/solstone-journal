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
        ("Linux", "arm64", resource.STT_LOCAL_FLOOR_LINUX_BYTES),
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
        ("Linux", "aarch64", "parakeet"),
        ("Linux", "arm64", "parakeet"),
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
        "explicit_backend",
        "available_bytes",
        "google_key_present",
        "floor_bytes",
        "local_backend",
        "confidential_lane_active",
        "confidential_audio_enabled",
        "expected",
    ),
    [
        ("parakeet", 1, True, 4 * 1024**3, "parakeet", True, True, "parakeet"),
        (
            "parakeet-cpp",
            1,
            True,
            4 * 1024**3,
            "parakeet",
            True,
            True,
            "parakeet-cpp",
        ),
        ("confidential", 1, True, 4 * 1024**3, "parakeet", True, True, "confidential"),
        ("confidential", 1, True, 4 * 1024**3, "parakeet", True, False, "parakeet"),
        (
            "confidential",
            1,
            True,
            4 * 1024**3,
            None,
            True,
            False,
            resource.STT_SURFACE,
        ),
        ("confidential", 1, True, 4 * 1024**3, "parakeet", False, True, "parakeet"),
        (
            "confidential",
            1,
            True,
            4 * 1024**3,
            None,
            False,
            True,
            resource.STT_SURFACE,
        ),
        ("gemini", 1, False, 4 * 1024**3, "parakeet", True, True, "gemini"),
        ("revai", 1, False, 4 * 1024**3, "parakeet", True, True, "revai"),
        (None, 1, False, 4 * 1024**3, "parakeet", True, True, "confidential"),
        (None, 1, True, 4 * 1024**3, "parakeet", True, False, "parakeet"),
        (
            None,
            1,
            True,
            4 * 1024**3,
            None,
            True,
            False,
            resource.STT_SURFACE,
        ),
        (None, 4 * 1024**3, False, 4 * 1024**3, "parakeet", False, True, "parakeet"),
        (None, 5 * 1024**3, True, 4 * 1024**3, "parakeet", False, True, "parakeet"),
        (None, 3 * 1024**3, True, 4 * 1024**3, "parakeet", False, True, "gemini"),
        (None, None, True, 4 * 1024**3, "parakeet", False, True, "gemini"),
        (None, 3 * 1024**3, True, None, None, False, True, "gemini"),
        (
            None,
            None,
            False,
            4 * 1024**3,
            "parakeet",
            False,
            True,
            resource.STT_SURFACE,
        ),
        (None, 3 * 1024**3, False, None, None, False, True, resource.STT_SURFACE),
    ],
)
def test_resolve_stt_backend_choice_matrix(
    explicit_backend: str | None,
    available_bytes: int | None,
    google_key_present: bool,
    floor_bytes: int | None,
    local_backend: str | None,
    confidential_lane_active: bool,
    confidential_audio_enabled: bool,
    expected: str,
) -> None:
    assert (
        resource.resolve_stt_backend_choice(
            explicit_backend,
            available_bytes,
            google_key_present=google_key_present,
            floor_bytes=floor_bytes,
            local_backend=local_backend,
            confidential_lane_active=confidential_lane_active,
            confidential_audio_enabled=confidential_audio_enabled,
        )
        == expected
    )


def test_resolve_stt_backend_choice_is_deterministic() -> None:
    args = {
        "explicit_backend": None,
        "available_bytes": 3 * 1024**3,
        "google_key_present": True,
        "floor_bytes": 4 * 1024**3,
        "local_backend": "parakeet",
        "confidential_lane_active": False,
        "confidential_audio_enabled": True,
    }

    assert resource.resolve_stt_backend_choice(
        **args
    ) == resource.resolve_stt_backend_choice(**args)

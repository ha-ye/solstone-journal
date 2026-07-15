# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.providers import PROVIDER_METADATA, build_provider_status


def test_google_provider_metadata_has_no_runtime_adapter() -> None:
    assert "cogitate_runtime" not in PROVIDER_METADATA["google"]


@pytest.mark.parametrize(
    ("api_key", "expected_status"),
    [
        (
            "",
            {
                "provider": "google",
                "configured": False,
                "generate_ready": False,
                "cogitate_ready": False,
                "issues": ["GOOGLE_API_KEY not set"],
            },
        ),
        (
            "key",
            {
                "provider": "google",
                "configured": True,
                "generate_ready": True,
                "cogitate_ready": True,
                "issues": [],
            },
        ),
    ],
)
def test_google_provider_status_uses_managed_key_only(
    monkeypatch,
    api_key: str,
    expected_status: dict[str, object],
) -> None:
    if api_key:
        monkeypatch.setenv("GOOGLE_API_KEY", api_key)
    else:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    status = build_provider_status(
        [{"name": "google", "env_key": "GOOGLE_API_KEY"}],
    )["google"]

    assert status == expected_status

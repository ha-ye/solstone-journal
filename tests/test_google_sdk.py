# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.providers import PROVIDER_METADATA, build_provider_status


def test_google_provider_metadata_has_no_cogitate_cli() -> None:
    assert "cogitate_cli" not in PROVIDER_METADATA["google"]
    assert "cogitate_runtime" not in PROVIDER_METADATA["google"]


@pytest.mark.parametrize(
    ("api_key", "vertex_creds_configured", "expected_status"),
    [
        (
            "",
            False,
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
            False,
            {
                "provider": "google",
                "configured": True,
                "generate_ready": True,
                "cogitate_ready": True,
                "issues": [],
            },
        ),
        (
            "",
            True,
            {
                "provider": "google",
                "configured": False,
                "generate_ready": False,
                "cogitate_ready": False,
                "issues": ["GOOGLE_API_KEY not set"],
            },
        ),
    ],
)
def test_google_provider_status_ignores_gemini_path(
    monkeypatch,
    api_key: str,
    vertex_creds_configured: bool,
    expected_status: dict[str, object],
) -> None:
    if api_key:
        monkeypatch.setenv("GOOGLE_API_KEY", api_key)
    else:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(
        "solstone.think.providers.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("which should not run")),
    )

    status = build_provider_status(
        [{"name": "google", "env_key": "GOOGLE_API_KEY"}],
        vertex_creds_configured=vertex_creds_configured,
    )["google"]

    assert "cogitate_cli" not in status
    assert "cogitate_cli_found" not in status
    assert status == expected_status

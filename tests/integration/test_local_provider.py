# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Integration tests for the bundled local provider with installed artifacts."""

from __future__ import annotations

import asyncio

import pytest

from solstone.think.models import LOCAL_FLASH


def _local_reachable() -> bool:
    try:
        from solstone.think.providers.local import validate_key

        return bool(validate_key("local", "").get("valid"))
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _local_reachable(),
        reason="Local provider runtime/model not installed or not reachable",
    ),
]


def test_basic_generation():
    from solstone.think.providers.local import run_generate

    result = run_generate(
        "What is 2 + 2? Reply with just the number.",
        model=LOCAL_FLASH,
        max_output_tokens=64,
    )

    assert result["text"]
    assert "4" in result["text"]


def test_async_generation():
    from solstone.think.providers.local import run_agenerate

    result = asyncio.run(
        run_agenerate(
            "What is 3 + 5? Reply with just the number.",
            model=LOCAL_FLASH,
            max_output_tokens=64,
        )
    )

    assert result["text"]
    assert "8" in result["text"]


def test_list_models():
    from solstone.think.providers.local import list_models

    models = list_models("local")

    assert isinstance(models, list)
    assert any(model["model"] == LOCAL_FLASH for model in models)


def test_validate_key_reachable():
    from solstone.think.providers.local import validate_key

    result = validate_key("local", "")

    assert result["valid"] is True

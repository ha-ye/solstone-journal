# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jsonschema import Draft202012Validator

from solstone.observe import describe as describe_mod
from solstone.think.batch import Batch

_SCHEMA = describe_mod._SCHEMA


def test_categorization_image_budget_is_linux_bundled_local_only(monkeypatch):
    from solstone.think.providers import local_endpoint

    monkeypatch.setattr(describe_mod.sys, "platform", "linux")
    monkeypatch.setattr(
        local_endpoint,
        "resolve_local_endpoint",
        lambda: SimpleNamespace(is_bundled=True),
    )

    assert (
        describe_mod._categorization_image_token_budget("local")
        == describe_mod.LOCAL_QWEN_CATEGORIZATION_IMAGE_TOKENS
    )
    assert describe_mod._categorization_image_token_budget("google") is None

    monkeypatch.setattr(
        local_endpoint,
        "resolve_local_endpoint",
        lambda: SimpleNamespace(is_bundled=False),
    )
    assert describe_mod._categorization_image_token_budget("local") is None

    monkeypatch.setattr(describe_mod.sys, "platform", "darwin")
    assert describe_mod._categorization_image_token_budget("local") is None


def test_describe_schema_file_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_SCHEMA)


def test_describe_schema_accepts_and_rejects_expected_values():
    validator = Draft202012Validator(_SCHEMA)

    assert validator.is_valid(
        {
            "visual_description": "A browser window with multiple open tabs.",
            "primary": "browsing",
            "secondary": "reading",
            "overlap": True,
        }
    )
    assert validator.is_valid(
        {
            "visual_description": "A code editor with a terminal pane.",
            "primary": "code",
            "secondary": "none",
            "overlap": False,
        }
    )
    assert validator.is_valid(
        {
            "visual_description": "A social feed shows posts and replies.",
            "primary": "social",
            "secondary": "none",
            "overlap": False,
        }
    )
    assert validator.is_valid(
        {
            "visual_description": "A browser page is open beside a social thread.",
            "primary": "browsing",
            "secondary": "social",
            "overlap": True,
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": "A dashboard view.",
            "primary": "unknown",
            "secondary": "none",
            "overlap": False,
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": "A dashboard view.",
            "primary": "productivity",
            "secondary": "unknown",
            "overlap": False,
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": "A dashboard view.",
            "secondary": "none",
            "overlap": False,
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": "A dashboard view.",
            "primary": "productivity",
            "secondary": "none",
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": "A dashboard view.",
            "primary": "productivity",
            "secondary": "none",
            "overlap": False,
            "confidence": 0.9,
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": "A dashboard view.",
            "primary": "productivity",
            "secondary": "none",
            "overlap": "yes",
        }
    )
    assert not validator.is_valid(
        {
            "visual_description": 7,
            "primary": "productivity",
            "secondary": "none",
            "overlap": False,
        }
    )


@pytest.mark.asyncio
@patch("solstone.think.batch.agenerate_with_result", new_callable=AsyncMock)
async def test_describe_batch_call_passes_schema(mock_agenerate):
    mock_agenerate.return_value = {
        "text": (
            '{"visual_description":"A code editor is visible.","primary":"code",'
            '"secondary":"none","overlap":false}'
        ),
        "finish_reason": "stop",
    }

    batch = Batch(max_concurrent=1)
    req = batch.create(
        contents="Analyze this screenshot frame from a screencast recording.",
        context="observe.describe.frame",
        json_output=True,
        json_schema=_SCHEMA,
    )
    batch.add(req)

    results = []
    async for completed_req in batch.drain_batch():
        results.append(completed_req)

    assert len(results) == 1
    assert mock_agenerate.call_args.kwargs["json_schema"] is describe_mod._SCHEMA


def test_category_enum_matches_registry():
    """The enums in `primary` and `secondary` MUST match the filenames under observe/categories/*.md."""
    categories_dir = Path(describe_mod.__file__).resolve().parent / "categories"
    on_disk = {p.stem for p in categories_dir.glob("*.md")}

    assert set(_SCHEMA["properties"]["primary"]["enum"]) == on_disk
    assert set(_SCHEMA["properties"]["secondary"]["enum"]) - {"none"} == on_disk
    assert "none" in _SCHEMA["properties"]["secondary"]["enum"]

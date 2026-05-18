# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from PIL import Image

from solstone.think.models import CLAUDE_SONNET_4, GEMINI_FLASH, GPT_5


def get_fixtures_env(api_key_name: str):
    fixtures_env = Path(__file__).parent.parent / "fixtures" / ".env"
    if not fixtures_env.exists():
        return None, None, None
    load_dotenv(fixtures_env, override=True)
    return fixtures_env, os.getenv(api_key_name), os.getenv("SOLSTONE_JOURNAL")


def _tiny_image() -> Image.Image:
    return Image.new("RGB", (2, 2), color="red")


@pytest.mark.integration
@pytest.mark.requires_api
def test_google_vision_accepts_tiny_image():
    fixtures_env, api_key, journal_path = get_fixtures_env("GOOGLE_API_KEY")
    if not fixtures_env:
        pytest.skip("tests/fixtures/.env not found")
    if not api_key:
        pytest.skip("GOOGLE_API_KEY not found in tests/fixtures/.env file")
    if not journal_path:
        pytest.skip("SOLSTONE_JOURNAL not found in tests/fixtures/.env file")

    from solstone.think.providers import google

    result = google.run_generate(
        ["Name the dominant color in one word.", _tiny_image()],
        model=GEMINI_FLASH,
        max_output_tokens=64,
        thinking_budget=0,
    )

    assert result["text"].strip()


@pytest.mark.integration
@pytest.mark.requires_api
def test_openai_vision_accepts_tiny_image():
    fixtures_env, api_key, journal_path = get_fixtures_env("OPENAI_API_KEY")
    if not fixtures_env:
        pytest.skip("tests/fixtures/.env not found")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not found in tests/fixtures/.env file")
    if not journal_path:
        pytest.skip("SOLSTONE_JOURNAL not found in tests/fixtures/.env file")

    from solstone.think.providers import openai

    result = openai.run_generate(
        ["Name the dominant color in one word.", _tiny_image()],
        model=GPT_5,
        max_output_tokens=64,
    )

    assert result["text"].strip()


@pytest.mark.integration
@pytest.mark.requires_api
def test_anthropic_vision_accepts_tiny_image():
    fixtures_env, api_key, journal_path = get_fixtures_env("ANTHROPIC_API_KEY")
    if not fixtures_env:
        pytest.skip("tests/fixtures/.env not found")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not found in tests/fixtures/.env file")
    if not journal_path:
        pytest.skip("SOLSTONE_JOURNAL not found in tests/fixtures/.env file")

    from solstone.think.providers import anthropic

    result = anthropic.run_generate(
        ["Name the dominant color in one word.", _tiny_image()],
        model=CLAUDE_SONNET_4,
        max_output_tokens=64,
    )

    assert result["text"].strip()

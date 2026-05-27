# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import logging


def test_build_generate_config_passes_through_at_cap(caplog):
    """Sum exactly at the inclusive cap (65535) passes through without clamp."""
    from solstone.think.providers.google import (
        GEMINI_MAX_OUTPUT_TOKENS,
        _build_generate_config,
    )

    with caplog.at_level(logging.WARNING, logger="solstone.think.providers.google"):
        config = _build_generate_config(
            temperature=0.3,
            max_output_tokens=49151,
            system_instruction=None,
            json_output=False,
            thinking_budget=16384,
        )

    warnings = [
        record
        for record in caplog.records
        if record.name == "solstone.think.providers.google"
        and record.levelno == logging.WARNING
    ]
    assert config.max_output_tokens == GEMINI_MAX_OUTPUT_TOKENS  # 65535
    assert config.thinking_config.thinking_budget == 16384
    assert warnings == []


def test_build_generate_config_clamps_at_inclusive_cap(caplog):
    """Vertex rejects max_output_tokens=65536 — clamp to 65535 inclusive.

    Regression for the talent-default case (49152 + 16384 = 65536) that landed
    as 400 INVALID_ARGUMENT in production before this bound was lowered from
    65536 to 65535.
    """
    from solstone.think.providers.google import (
        GEMINI_MAX_OUTPUT_TOKENS,
        _build_generate_config,
    )

    with caplog.at_level(logging.WARNING, logger="solstone.think.providers.google"):
        config = _build_generate_config(
            temperature=0.3,
            max_output_tokens=49152,
            system_instruction=None,
            json_output=False,
            thinking_budget=16384,
        )

    warnings = [
        record
        for record in caplog.records
        if record.name == "solstone.think.providers.google"
        and record.levelno == logging.WARNING
    ]
    assert config.max_output_tokens == GEMINI_MAX_OUTPUT_TOKENS  # 65535
    assert config.thinking_config.thinking_budget == 16383
    assert len(warnings) == 1
    assert "max_output_tokens=49152" in warnings[0].message
    assert "thinking_budget=16384" in warnings[0].message
    assert "clamped_thinking_budget=16383" in warnings[0].message


def test_build_generate_config_clamps_oversized(caplog):
    """Inputs well over the cap clamp max_output_tokens first, then thinking."""
    from solstone.think.providers.google import (
        GEMINI_MAX_OUTPUT_TOKENS,
        _build_generate_config,
    )

    with caplog.at_level(logging.WARNING, logger="solstone.think.providers.google"):
        config = _build_generate_config(
            temperature=0.3,
            max_output_tokens=49152,
            system_instruction=None,
            json_output=False,
            thinking_budget=24576,
        )

    warnings = [
        record
        for record in caplog.records
        if record.name == "solstone.think.providers.google"
        and record.levelno == logging.WARNING
    ]
    assert config.max_output_tokens == GEMINI_MAX_OUTPUT_TOKENS  # 65535
    # 65535 - 49152 = 16383 left for thinking
    assert config.thinking_config.thinking_budget == 16383
    assert len(warnings) == 1
    assert "max_output_tokens=49152" in warnings[0].message
    assert "thinking_budget=24576" in warnings[0].message
    assert "clamped_thinking_budget=16383" in warnings[0].message

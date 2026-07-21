# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-layer model tiers.

These are vendor-facing ids that need tending as vendors evolve. Provider rows
use explicit model ids. Nothing under ``solstone/think/`` should import this
catalog.
"""

from __future__ import annotations

# fmt: off
MODEL_TIERS = {
    "google": [
        {"tier": "mid", "label": "Gemini 3.5 Flash", "model": "gemini-3.5-flash"},
        {"tier": "lite", "label": "Gemini 3.1 Flash Lite", "model": "gemini-3.1-flash-lite"},
    ],
    "anthropic": [
        {"tier": "top", "label": "Claude Opus", "model": "claude-opus-4-8"},
        {"tier": "mid", "label": "Claude Sonnet", "model": "claude-sonnet-5"},
        {"tier": "lite", "label": "Claude Haiku", "model": "claude-haiku-4-5"},
    ],
    "openai": [
        {"tier": "top", "label": "GPT", "model": "gpt-5.5"},
        {"tier": "mid", "label": "GPT mini", "model": "gpt-5.4-mini"},
        {"tier": "lite", "label": "GPT nano", "model": "gpt-5.4-nano"},
    ],
}
# fmt: on

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-layer model tiers.

These are vendor-facing ids that need tending as vendors evolve. Google's
``-latest`` aliases self-tend; the Anthropic/OpenAI rows are pinned family
aliases. Nothing under ``solstone/think/`` should import this catalog.
"""

from __future__ import annotations

# fmt: off
MODEL_TIERS = {
    "google": [
        {"tier": "top", "label": "Gemini Pro Latest", "model": "gemini-pro-latest"},
        {"tier": "mid", "label": "Gemini Flash Latest", "model": "gemini-flash-latest"},
        {"tier": "lite", "label": "Gemini Flash Lite Latest", "model": "gemini-flash-lite-latest"},
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

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.apps.tokens import copy


def test_copy_constants_are_canonical():
    assert copy.TOKENS_TILE_COST_LABEL == "today's cost"
    assert copy.TOKENS_TILE_COST_VALUE == "${cost:.2f}"
    assert copy.TOKENS_TILE_TOKENS_LABEL == "today's tokens"
    assert copy.TOKENS_TILE_TOKENS_VALUE == "{tokens}"
    assert copy.TOKENS_TILE_RUN_RATE_LABEL == "7-day run rate"
    assert copy.TOKENS_TILE_RUN_RATE_VALUE == "~${rate:.2f}/day"
    assert copy.TOKENS_TILE_TOP_DRIVER_LABEL == "today's biggest cost"
    assert copy.TOKENS_TILE_TOP_DRIVER_VALUE == "{provider} · {model} ({pct}% of today)"
    assert (
        copy.TOKENS_DISCLOSURE_PROVIDER
        == "by provider — {n} providers, top: {top_name} {pct}%"
    )
    assert (
        copy.TOKENS_DISCLOSURE_MODEL == "by model — {n} models, top: {top_name} {pct}%"
    )
    assert (
        copy.TOKENS_DISCLOSURE_TOKEN_TYPE
        == "by token type — input / output / cached / reasoning"
    )
    assert (
        copy.TOKENS_DISCLOSURE_CONTEXT
        == "by context — {n} contexts, top: {top_name} {pct}%"
    )
    assert copy.TOKENS_DISCLOSURE_SEGMENT == "by segment — {n} segments"

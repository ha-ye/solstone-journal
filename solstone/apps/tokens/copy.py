# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

TOKENS_TILE_COST_LABEL = "today's cost"
TOKENS_TILE_COST_VALUE = "${cost:.2f}"
TOKENS_TILE_TOKENS_LABEL = "today's tokens"
TOKENS_TILE_TOKENS_VALUE = "{tokens}"
TOKENS_TILE_RUN_RATE_LABEL = "7-day run rate"
TOKENS_TILE_RUN_RATE_VALUE = "~${rate:.2f}/day"
TOKENS_TILE_TOP_DRIVER_LABEL = "today's biggest cost"
TOKENS_TILE_TOP_DRIVER_VALUE = "{provider} · {model} ({pct}% of today)"

TOKENS_DISCLOSURE_PROVIDER = "by provider — {n} providers, top: {top_name} {pct}%"
TOKENS_DISCLOSURE_MODEL = "by model — {n} models, top: {top_name} {pct}%"
TOKENS_DISCLOSURE_TOKEN_TYPE = "by token type — input / output / cached / reasoning"
TOKENS_DISCLOSURE_CONTEXT = "by context — {n} contexts, top: {top_name} {pct}%"
TOKENS_DISCLOSURE_SEGMENT = "by segment — {n} segments"

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

TOKENS_DISCLOSURE_PROVIDER = (
    "{count} providers · top: {top_provider} ({top_pct}% of today)"
)
TOKENS_DISCLOSURE_MODEL = "{count} models · top: {top_model} ({top_pct}% of today)"
TOKENS_DISCLOSURE_TOKEN_TYPE = (
    "input {input_pct}% · output {output_pct}% · cached {cached_pct}%"
)
TOKENS_DISCLOSURE_CONTEXT = (
    "{count} context prefixes · top: {top_context} ({top_pct}% of today)"
)
TOKENS_DISCLOSURE_SEGMENT = (
    "{count} segments · top: {top_segment} ({top_pct}% of today)"
)

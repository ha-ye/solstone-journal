# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider-facing JSON Schema preparation for strict structured output."""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Solstone schema annotations are canonical-validation hints. Provider reductions
# below are request-shaping only; models.py still validates the canonical schema
# and honors annotations after generation.
SCHEMA_TRUNCATE_KEY = "x-truncate"

# Provider structured-output support references:
# - OpenAI: https://developers.openai.com/api/docs/guides/structured-outputs
#   supports pattern/format/minimum/maximum/minItems/maxItems; minLength and
#   maxLength are not listed in the supported subset.
# - Anthropic: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
#   rejects numeric/string/array constraints beyond minItems values 0 or 1;
#   strip minItems wholesale here for deterministic simplicity. Pattern is
#   documented as supported.
# - Google: https://ai.google.dev/gemini-api/docs/structured-output
#   supports string format, number minimum/maximum, and array minItems/maxItems.
STRICT_UNSUPPORTED_KEYWORDS: dict[str, frozenset[str]] = {
    "openai": frozenset(
        {"$schema", "$comment", "minLength", "maxLength", SCHEMA_TRUNCATE_KEY}
    ),
    "google": frozenset(
        {"$schema", "$comment", "minLength", "maxLength", SCHEMA_TRUNCATE_KEY}
    ),
    "anthropic": frozenset(
        {
            "$schema",
            "$comment",
            SCHEMA_TRUNCATE_KEY,
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "minimum",
            "maximum",
        }
    ),
}

# Hazard: cloud-provider reductions are request-only. Canonical response
# validation in models.py still enforces stripped bounds and annotations, so an
# Anthropic response that overruns future canonical maxItems/maxLength bounds
# will fail loudly with SchemaValidationError unless an honored annotation
# truncates that specific instance path first.


def unsupported_keyword_hits(schema: dict[str, Any] | None, provider: str) -> list[str]:
    """Return JSON-pointer-ish paths for keywords unsupported by ``provider``."""
    if schema is None:
        return []

    unsupported = STRICT_UNSUPPORTED_KEYWORDS.get(provider, frozenset())
    if not unsupported:
        return []

    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}/{key}"
                if key in unsupported:
                    found.append(child_path)
                walk(value, child_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema, "$")
    return found


def prepare_provider_schema(
    schema: dict[str, Any] | None, provider: str
) -> dict[str, Any] | None:
    """Return a provider-facing copy of ``schema`` with unsupported keys removed."""
    if schema is None:
        return None

    prepared = copy.deepcopy(schema)
    unsupported = STRICT_UNSUPPORTED_KEYWORDS.get(provider, frozenset())
    if not unsupported:
        return prepared

    removed: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key in list(node):
                child_path = f"{path}/{key}"
                if key in unsupported:
                    node.pop(key)
                    removed.append(child_path)
                    continue
                walk(node[key], child_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(prepared, "$")

    if removed:
        logger.debug(
            "Removed %d unsupported JSON Schema keyword(s) for provider %s",
            len(removed),
            provider,
        )

    return prepared

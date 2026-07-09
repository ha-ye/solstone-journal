# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure scoring helpers for structured-output schema evals."""

from __future__ import annotations

import json
from typing import Any, Sequence

from jsonschema import Draft202012Validator


def schema_validity(text: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Validate response text against ``schema`` and return a serializable result."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "valid": False,
            "errors": [
                {
                    "path": "$",
                    "message": f"Invalid JSON: {exc.msg}",
                }
            ],
        }

    errors = []
    for error in sorted(
        Draft202012Validator(schema).iter_errors(parsed),
        key=lambda item: list(item.path),
    ):
        path = "$"
        for part in error.path:
            if isinstance(part, int):
                path += f"[{part}]"
            else:
                path += f"/{part}"
        errors.append({"path": path, "message": error.message})

    return {"valid": not errors, "errors": errors}


def content_preservation(text: str, expect_contains: Sequence[str]) -> dict[str, Any]:
    """Score case-insensitive substring preservation in response text."""
    needles = list(expect_contains)
    if not needles:
        return {"fraction": 1.0, "found": [], "missing": []}

    haystack = text.lower()
    found = [needle for needle in needles if needle.lower() in haystack]
    missing = [needle for needle in needles if needle.lower() not in haystack]
    return {
        "fraction": len(found) / len(needles),
        "found": found,
        "missing": missing,
    }

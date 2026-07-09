# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.schema_eval import content_preservation, schema_validity


def test_schema_validity_accepts_valid_response() -> None:
    result = schema_validity(
        '{"items": ["alpha"]}',
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    )

    assert result == {"valid": True, "errors": []}


def test_schema_validity_reports_invalid_json() -> None:
    result = schema_validity("{", {"type": "object"})

    assert result["valid"] is False
    assert result["errors"][0]["path"] == "$"


def test_schema_validity_reports_schema_errors() -> None:
    result = schema_validity(
        '{"items": ["alpha", "beta"]}',
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": 1,
                    "items": {"type": "string"},
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    )

    assert result["valid"] is False
    assert result["errors"][0]["path"] == "$/items"


def test_content_preservation_scores_case_insensitive_matches() -> None:
    result = content_preservation(
        '{"summary": "Alpha and beta are present."}',
        ["alpha", "BETA", "gamma"],
    )

    assert result == {
        "fraction": 2 / 3,
        "found": ["alpha", "BETA"],
        "missing": ["gamma"],
    }


def test_content_preservation_empty_needles_passes() -> None:
    assert content_preservation("anything", []) == {
        "fraction": 1.0,
        "found": [],
        "missing": [],
    }

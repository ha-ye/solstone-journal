# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from typing import Any

import pytest

from solstone.think.schema_bounds import unbounded_nodes


def _schema(property_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"field": property_schema},
        "required": ["field"],
        "additionalProperties": False,
    }


def test_bounded_array_passes() -> None:
    assert unbounded_nodes(_schema({"type": "array", "maxItems": 3})) == []


def test_unbounded_array_fails() -> None:
    assert unbounded_nodes(_schema({"type": "array"})) == ["$/properties/field"]


@pytest.mark.parametrize(
    "property_schema",
    [
        {"type": "string", "enum": ["a"]},
        {"type": "string", "const": "a"},
        {"type": "string", "pattern": "^[a-z]+$"},
        {"type": "string", "format": "date-time"},
    ],
)
def test_constrained_strings_pass(property_schema: dict[str, Any]) -> None:
    assert unbounded_nodes(_schema(property_schema)) == []


def test_bare_string_fails() -> None:
    assert unbounded_nodes(_schema({"type": "string"})) == ["$/properties/field"]


def test_bounded_string_passes() -> None:
    assert unbounded_nodes(_schema({"type": "string", "maxLength": 80})) == []


def test_nested_arrays_and_objects_report_paths() -> None:
    schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": ["string", "null"]},
                        "codes": {
                            "type": ["array", "null"],
                            "maxItems": 3,
                            "items": {"type": "string", "pattern": "^[A-Z]+$"},
                        },
                    },
                    "required": ["title", "codes"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["groups"],
        "additionalProperties": False,
    }

    assert unbounded_nodes(schema) == [
        "$/properties/groups",
        "$/properties/groups/items/properties/title",
    ]

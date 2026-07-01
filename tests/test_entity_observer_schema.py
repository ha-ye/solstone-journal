# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from solstone.think.talent import get_talent

SCHEMA_PATH = (
    Path(__file__).parent.parent
    / "solstone"
    / "apps"
    / "entities"
    / "talent"
    / "entity_observer.schema.json"
)


def _schema_text() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _load_schema() -> dict:
    return json.loads(_schema_text())


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(_load_schema())


def test_talent_exposes_json_schema():
    assert get_talent("entities:entity_observer")["json_schema"] == _load_schema()


def test_valid_operations_payload():
    validator = Draft202012Validator(_load_schema())

    assert validator.is_valid(
        {
            "entities": [
                {
                    "entity_id": "alice_johnson",
                    "operations": [
                        {
                            "op": "update",
                            "target_index": 0,
                            "content": "Prefers concise morning planning meetings",
                            "target_quote": "morning meetings",
                            "reasoning": "Fresh source narrows the preference.",
                        },
                        {
                            "op": "add",
                            "target_index": None,
                            "content": "Has deep knowledge of distributed systems",
                            "target_quote": None,
                            "reasoning": "Durable expertise.",
                        },
                        {
                            "op": "drop",
                            "target_index": 2,
                            "content": None,
                            "target_quote": "legacy planning",
                            "reasoning": "Stale duplicate.",
                        },
                        {
                            "op": "keep",
                            "target_index": 3,
                            "content": None,
                            "target_quote": None,
                            "reasoning": "Still useful.",
                        },
                    ],
                }
            ],
            "summary": "four operations",
        }
    )


def test_invalid_missing_top_required():
    validator = Draft202012Validator(_load_schema())

    assert not validator.is_valid({"entities": []})
    assert not validator.is_valid({"summary": "missing entities"})


def test_invalid_extra_property_at_each_level():
    validator = Draft202012Validator(_load_schema())

    assert not validator.is_valid(
        {"entities": [], "summary": "extra top", "extra": True}
    )
    assert not validator.is_valid(
        {
            "entities": [
                {"entity_id": "alice_johnson", "operations": [], "extra": True}
            ],
            "summary": "extra entity",
        }
    )
    assert not validator.is_valid(
        {
            "entities": [
                {
                    "entity_id": "alice_johnson",
                    "operations": [{"op": "keep", "reasoning": "audit", "extra": True}],
                }
            ],
            "summary": "extra operation",
        }
    )


def test_invalid_unknown_op_enum():
    validator = Draft202012Validator(_load_schema())

    assert not validator.is_valid(
        {
            "entities": [
                {
                    "entity_id": "alice_johnson",
                    "operations": [
                        {
                            "op": "replace",
                            "target_index": None,
                            "content": None,
                            "target_quote": None,
                            "reasoning": "unknown op",
                        }
                    ],
                }
            ],
            "summary": "unknown op",
        }
    )


def test_schema_has_no_conditional_keywords():
    schema = _schema_text()

    assert '"if"' not in schema
    assert '"then"' not in schema
    assert '"oneOf"' not in schema

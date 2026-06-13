# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - optional in this test.
    Draft202012Validator = None


REPO_ROOT = Path(__file__).resolve().parents[1]
CHAT_SCHEMA_PATH = REPO_ROOT / "solstone" / "talent" / "chat.schema.json"


def _load_chat_schema() -> dict:
    return json.loads(CHAT_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_chat_schema_offer_shape_is_exact() -> None:
    schema = _load_chat_schema()
    offer = schema["properties"]["offer"]

    assert "offer" in schema["required"]
    assert offer["type"] == ["object", "null"]
    assert offer["additionalProperties"] is False
    assert offer["required"] == ["kind"]
    assert offer["properties"]["kind"]["enum"] == ["support"]


def test_chat_schema_accepts_offer_and_null_offer_payloads() -> None:
    if Draft202012Validator is None:
        return

    schema = _load_chat_schema()
    validator = Draft202012Validator(schema)
    base_payload = {
        "message": "I can bring in solstone support.",
        "notes": "offered support",
        "talent_request": None,
    }

    validator.validate({**base_payload, "offer": {"kind": "support"}})
    validator.validate({**base_payload, "offer": None})

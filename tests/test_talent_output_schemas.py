# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from solstone.think.talent import get_talent

TALENT_DIR = Path(__file__).resolve().parents[1] / "solstone" / "talent"


def _load_schema(name: str) -> dict:
    return json.loads((TALENT_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _load_frontmatter(name: str) -> dict:
    text = (TALENT_DIR / f"{name}.md").read_text(encoding="utf-8")
    metadata, _end = json.JSONDecoder().raw_decode(text.lstrip())
    assert isinstance(metadata, dict)
    return metadata


def _assert_declares_max_output_tokens(name: str) -> None:
    metadata = _load_frontmatter(name)
    assert "max_output_tokens" in metadata
    assert isinstance(metadata["max_output_tokens"], int)
    assert metadata["max_output_tokens"] > 0


def test_documents_talent_uses_bounded_json_schema():
    schema = _load_schema("documents")
    Draft202012Validator.check_schema(schema)

    talent = get_talent("documents")

    assert talent["output"] == "json"
    _assert_declares_max_output_tokens("documents")
    assert talent["json_schema"] == schema
    assert talent["degradation_check"] is True
    assert sorted(schema["properties"]) == [
        "assets",
        "conditions",
        "important_dates",
        "key_provisions",
        "overview",
        "parties",
        "summary",
    ]


def test_screen_talent_uses_bounded_json_schema():
    schema = _load_schema("screen")
    Draft202012Validator.check_schema(schema)

    talent = get_talent("screen")

    assert talent["output"] == "json"
    _assert_declares_max_output_tokens("screen")
    assert talent["json_schema"] == schema
    assert sorted(schema["properties"]) == ["entities", "narrative"]

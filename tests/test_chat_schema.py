# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHAT_SCHEMA_PATH = REPO_ROOT / "solstone" / "talent" / "chat.schema.json"


def _load_chat_schema() -> dict:
    return json.loads(CHAT_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_chat_schema_has_no_offer_affordance() -> None:
    schema = _load_chat_schema()

    assert "offer" not in schema["properties"]
    assert "offer" not in schema["required"]


def test_chat_schema_has_no_draft_affordance() -> None:
    schema = _load_chat_schema()

    assert "draft" not in schema["properties"]
    assert "draft" not in schema["required"]

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.convey import create_app
from solstone.convey.sol_initiated.copy import (
    KIND_OWNER_CHAT_DISMISSED,
    KIND_OWNER_CHAT_OPEN,
    KIND_SOL_CHAT_REQUEST,
    KIND_SOL_CHAT_REQUEST_SUPERSEDED,
)


@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    config_dir = journal / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "setup": {"completed_at": 1700000000000},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    return client


def test_live_append_origin_tag_script_handles_sol_initiated_events(chat_client):
    response = chat_client.get("/app/chat/workspace")

    assert response.status_code == 200
    fragment = response.get_data(as_text=True)
    renderer = Path("solstone/convey/static/chat_render.js").read_text(encoding="utf-8")

    assert "let pendingSolChatRequest = null;" in fragment
    assert "origin: origin" in fragment
    assert "const supersededRequestId = msg.request_id || '';" in fragment
    assert KIND_SOL_CHAT_REQUEST in fragment
    assert KIND_SOL_CHAT_REQUEST_SUPERSEDED in fragment
    assert KIND_OWNER_CHAT_OPEN in fragment
    assert KIND_OWNER_CHAT_DISMISSED in fragment

    assert "renderOriginTag" in renderer
    assert "item.dataset.requestId = event.origin.request_id;" in renderer

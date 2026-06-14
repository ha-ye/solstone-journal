# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.apps.thinking import copy as thinking_copy
from solstone.convey import create_app

APP_JSON = Path(__file__).resolve().parents[1] / "app.json"


def test_workspace_renders_each_lane(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    config.setdefault("convey", {})["trust_localhost"] = True
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True

    response = app.test_client().get("/app/thinking/", follow_redirects=True)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="providers"' in html
    assert 'id="lane-scout"' in html
    assert 'id="lane-byo"' in html
    assert 'id="lane-local"' in html
    assert 'id="scoutEnable"' in html
    assert 'id="scoutCheck"' in html
    assert 'id="scoutRefresh"' in html
    assert 'id="scoutDisable"' in html
    assert 'id="scoutLaneOperation"' in html
    assert "window.THINKING =" in html
    assert "thinking/static/thinking.js" in html


def test_copy_payload_round_trips_apostrophes() -> None:
    payload = thinking_copy.thinking_copy_payload()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert decoded == payload


def test_thinking_copy_avoids_forbidden_terms() -> None:
    combined = "\n".join(thinking_copy.thinking_copy_values())
    combined += "\n" + json.loads(APP_JSON.read_text(encoding="utf-8"))["label"]

    for term in (
        "account",
        "account_id",
        "sign in",
        "log in",
        "subscribe",
        "upgrade",
        "capture",
        "watch",
        "record",
        "monitor",
        "track",
        "collect",
    ):
        assert re.search(rf"\b{re.escape(term)}\b", combined, re.IGNORECASE) is None

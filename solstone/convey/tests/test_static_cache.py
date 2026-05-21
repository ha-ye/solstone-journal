# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import re
from datetime import timedelta
from pathlib import Path


def test_send_file_max_age_default_configured(convey_env):
    env = convey_env()

    assert env.app.config["SEND_FILE_MAX_AGE_DEFAULT"] == timedelta(seconds=300)


def test_static_asset_carries_max_age_and_etag(convey_env):
    env = convey_env()

    resp = env.client.get("/static/error-handler.js")

    assert resp.status_code == 200
    assert "max-age=300" in resp.headers["Cache-Control"]
    assert resp.headers.get("ETag")


def test_head_scripts_all_deferred():
    app_html = Path(__file__).resolve().parents[1] / "templates" / "app.html"
    text = app_html.read_text(encoding="utf-8")

    script_tags = re.findall(r"<script src=[^>]+>", text)

    assert len(script_tags) == 12
    assert all("defer" in tag or "async" in tag for tag in script_tags)

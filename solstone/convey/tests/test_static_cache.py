# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from datetime import timedelta


def test_send_file_max_age_default_configured(convey_env):
    env = convey_env()

    assert env.app.config["SEND_FILE_MAX_AGE_DEFAULT"] == timedelta(seconds=300)


def test_static_asset_carries_max_age_and_etag(convey_env):
    env = convey_env()

    resp = env.client.get("/static/error-handler.js")

    assert resp.status_code == 200
    assert "max-age=300" in resp.headers["Cache-Control"]
    assert resp.headers.get("ETag")

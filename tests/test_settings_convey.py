# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from solstone.apps.settings.copy import (
    CONVEY_REFUSE_NO_PASSWORD_TRUST,
)
from solstone.convey import create_app


def _read_config(journal_dir: Path) -> dict:
    return json.loads((journal_dir / "config" / "journal.json").read_text("utf-8"))


def _write_config(journal_dir: Path, payload: dict) -> None:
    (journal_dir / "config" / "journal.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_password(journal_dir: Path) -> None:
    config = _read_config(journal_dir)
    config["convey"].pop("password_hash", None)
    config["convey"].pop("password", None)
    _write_config(journal_dir, config)


def _settings_client(journal_dir: Path):
    app = create_app(str(journal_dir))
    app.config["TESTING"] = True
    return app.test_client()


def test_api_get_config_masks_password_without_effective_host_url(journal_copy):
    client = _settings_client(journal_copy)

    response = client.get("/app/settings/api/config")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["convey"]["allow_network_access"] is False
    assert payload["convey"]["has_password"] is True
    assert "password_hash" not in payload["convey"]
    assert "pairing" not in payload


def test_api_put_corrupt_config_returns_reason_without_writing(journal_copy):
    client = _settings_client(journal_copy)
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    config_path = journal_copy / "config" / "journal.json"
    config_path.write_bytes(b"{ invalid json }")
    before = config_path.read_bytes()

    with patch("solstone.apps.settings.routes.write_journal_config") as write_config:
        response = client.put(
            "/app/settings/api/config",
            json={"section": "identity", "data": {"name": "Changed"}},
            content_type="application/json",
        )

    assert response.status_code == 500
    assert response.get_json()["reason_code"] == "corrupt_config"
    write_config.assert_not_called()
    assert config_path.read_bytes() == before


def test_api_put_trust_localhost_refuses_without_password(journal_copy):
    _clear_password(journal_copy)
    client = _settings_client(journal_copy)

    response = client.put(
        "/app/settings/api/config",
        json={"section": "convey", "data": {"trust_localhost": False}},
        content_type="application/json",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert (
        payload["error"] == "I couldn't change localhost trust until a password is set."
    )
    assert payload["reason_code"] == "network_security_requires_password"
    assert payload["detail"] == CONVEY_REFUSE_NO_PASSWORD_TRUST

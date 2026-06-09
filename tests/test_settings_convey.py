# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from solstone.apps.settings.copy import (
    CONVEY_MOVED_NETWORK_DISABLE,
    CONVEY_MOVED_NETWORK_ENABLE,
    CONVEY_NETWORK_ACCESS_CONFIG_REJECTED,
    CONVEY_NETWORK_LOCAL_ONLY_REASON,
    CONVEY_REFUSE_NO_PASSWORD_NETWORK,
    CONVEY_REFUSE_NO_PASSWORD_TRUST,
)
from solstone.convey import create_app
from solstone.think.call import call_app

runner = CliRunner()


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


def _login(client) -> None:
    with client.session_transaction() as sess:
        sess["logged_in"] = True


def test_cli_network_access_enable_is_moved_stub(journal_copy):
    result = runner.invoke(call_app, ["settings", "convey", "network-access", "enable"])

    assert result.exit_code == 2
    assert result.stderr == CONVEY_MOVED_NETWORK_ENABLE + "\n"


def test_cli_network_access_disable_is_moved_stub(journal_copy):
    result = runner.invoke(
        call_app,
        ["settings", "convey", "network-access", "disable"],
    )

    assert result.exit_code == 2
    assert result.stderr == CONVEY_MOVED_NETWORK_DISABLE + "\n"


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


def test_api_put_network_access_key_value_rejected_without_write_or_restart(
    journal_copy,
):
    client = _settings_client(journal_copy)
    before = copy.deepcopy(_read_config(journal_copy))

    with (
        patch("solstone.apps.settings.routes.set_network_access") as set_network,
        patch("solstone.convey.restart.wait_for_convey_restart") as restart,
    ):
        response = client.put(
            "/app/settings/api/config",
            json={"section": "convey", "key": "allow_network_access", "value": True},
            content_type="application/json",
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == CONVEY_NETWORK_ACCESS_CONFIG_REJECTED
    set_network.assert_not_called()
    restart.assert_not_called()
    assert _read_config(journal_copy) == before


def test_api_put_combined_password_and_network_rejected_without_persisting(
    journal_copy,
):
    _clear_password(journal_copy)
    client = _settings_client(journal_copy)
    before = copy.deepcopy(_read_config(journal_copy))

    with (
        patch("solstone.apps.settings.routes.set_network_access") as set_network,
        patch("solstone.convey.restart.wait_for_convey_restart") as restart,
    ):
        response = client.put(
            "/app/settings/api/config",
            json={
                "section": "convey",
                "data": {"password": "atomicpw8", "allow_network_access": True},
            },
            content_type="application/json",
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == CONVEY_NETWORK_ACCESS_CONFIG_REJECTED
    set_network.assert_not_called()
    restart.assert_not_called()
    assert _read_config(journal_copy) == before


def test_api_put_network_access_data_rejected_without_write_or_restart(journal_copy):
    client = _settings_client(journal_copy)
    before = copy.deepcopy(_read_config(journal_copy))

    with (
        patch("solstone.apps.settings.routes.set_network_access") as set_network,
        patch("solstone.convey.restart.wait_for_convey_restart") as restart,
    ):
        response = client.put(
            "/app/settings/api/config",
            json={"section": "convey", "data": {"allow_network_access": True}},
            content_type="application/json",
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == CONVEY_NETWORK_ACCESS_CONFIG_REJECTED
    set_network.assert_not_called()
    restart.assert_not_called()
    assert _read_config(journal_copy) == before


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
        payload["error"] == "I couldn't change network access until a password is set."
    )
    assert payload["reason_code"] == "network_security_requires_password"
    assert payload["detail"] == CONVEY_REFUSE_NO_PASSWORD_TRUST


def test_api_network_access_loopback_success(journal_copy):
    client = _settings_client(journal_copy)

    with (
        patch(
            "solstone.convey.restart.wait_for_convey_restart", return_value=(True, [])
        ) as restart,
        patch(
            "solstone.think.pairing.config.get_host_url",
            return_value="http://192.168.1.44:5015",
        ),
    ):
        response = client.post(
            "/app/settings/api/convey/network-access",
            json={"enable": True},
            content_type="application/json",
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "effective_host_url": "http://192.168.1.44:5015",
        "ok": True,
        "restart_timeout": False,
    }
    restart.assert_called_once_with(timeout=15.0)
    assert _read_config(journal_copy)["convey"]["allow_network_access"] is True


def test_api_network_access_missing_password_refuses_without_persisting(journal_copy):
    _clear_password(journal_copy)
    client = _settings_client(journal_copy)
    _login(client)
    before = copy.deepcopy(_read_config(journal_copy))

    with patch("solstone.convey.restart.wait_for_convey_restart") as restart:
        response = client.post(
            "/app/settings/api/convey/network-access",
            json={"enable": True},
            content_type="application/json",
        )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "network_security_requires_password"
    assert payload["detail"] == CONVEY_REFUSE_NO_PASSWORD_NETWORK
    restart.assert_not_called()
    assert _read_config(journal_copy) == before


def test_api_network_access_timeout_still_saves(journal_copy):
    client = _settings_client(journal_copy)

    with (
        patch(
            "solstone.convey.restart.wait_for_convey_restart", return_value=(False, [])
        ),
        patch(
            "solstone.think.pairing.config.get_host_url",
            return_value="http://localhost:5015",
        ),
    ):
        response = client.post(
            "/app/settings/api/convey/network-access",
            json={"enable": True},
            content_type="application/json",
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "effective_host_url": "http://localhost:5015",
        "ok": True,
        "restart_timeout": True,
    }
    assert _read_config(journal_copy)["convey"]["allow_network_access"] is True


def test_api_network_access_non_loopback_refuses_without_persisting(journal_copy):
    client = _settings_client(journal_copy)
    _login(client)
    before = copy.deepcopy(_read_config(journal_copy))

    with patch("solstone.apps.settings.routes.set_network_access") as set_network:
        response = client.post(
            "/app/settings/api/convey/network-access",
            json={"enable": True},
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "192.168.1.5"},
        )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "local_request_required"
    set_network.assert_not_called()
    assert _read_config(journal_copy) == before


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Forwarded", "for=192.168.1.5"),
        ("X-Forwarded-For", "192.168.1.5"),
        ("X-Real-IP", "192.168.1.5"),
        ("X-Forwarded-Host", "example.test"),
        ("CF-Connecting-IP", "192.168.1.5"),
        ("True-Client-IP", "192.168.1.5"),
        ("Fly-Client-IP", "192.168.1.5"),
    ],
)
def test_api_network_access_forwarded_headers_refuse_without_persisting(
    journal_copy,
    header,
    value,
):
    client = _settings_client(journal_copy)
    _login(client)
    before = copy.deepcopy(_read_config(journal_copy))

    with patch("solstone.apps.settings.routes.set_network_access") as set_network:
        response = client.post(
            "/app/settings/api/convey/network-access",
            json={"enable": True},
            content_type="application/json",
            headers={header: value},
        )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "local_request_required"
    set_network.assert_not_called()
    assert _read_config(journal_copy) == before


def test_api_network_access_empty_forwarded_header_refuses_without_persisting(
    journal_copy,
):
    client = _settings_client(journal_copy)
    _login(client)
    before = copy.deepcopy(_read_config(journal_copy))

    with patch("solstone.apps.settings.routes.set_network_access") as set_network:
        response = client.post(
            "/app/settings/api/convey/network-access",
            json={"enable": True},
            content_type="application/json",
            headers={"X-Forwarded-For": ""},
        )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "local_request_required"
    set_network.assert_not_called()
    assert _read_config(journal_copy) == before


@pytest.mark.parametrize(
    ("body", "reason_code", "detail"),
    [
        (None, "missing_request_body", "No data provided"),
        ({}, "missing_required_field", "enable"),
        ({"enable": "true"}, "invalid_request_value", "enable must be a boolean"),
    ],
)
def test_api_network_access_validates_request_body(
    journal_copy, body, reason_code, detail
):
    client = _settings_client(journal_copy)
    kwargs = {"content_type": "application/json"}
    if body is not None:
        kwargs["json"] = body

    response = client.post("/app/settings/api/convey/network-access", **kwargs)

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == reason_code
    assert payload["detail"] == detail


def test_api_network_access_capability_loopback_writable(journal_copy):
    client = _settings_client(journal_copy)

    response = client.get("/app/settings/api/convey/network-access/capability")

    assert response.status_code == 200
    assert response.get_json() == {
        "can_change_network_access": True,
        "network_access_enabled": False,
        "reason": None,
    }


def test_api_network_access_capability_non_loopback_disabled(journal_copy):
    config = _read_config(journal_copy)
    config["convey"]["allow_network_access"] = True
    _write_config(journal_copy, config)
    client = _settings_client(journal_copy)
    _login(client)

    response = client.get(
        "/app/settings/api/convey/network-access/capability",
        environ_base={"REMOTE_ADDR": "192.168.1.5"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "can_change_network_access": False,
        "network_access_enabled": True,
        "reason": CONVEY_NETWORK_LOCAL_ONLY_REASON,
    }


def test_api_network_access_capability_forwarded_header_disabled(journal_copy):
    client = _settings_client(journal_copy)
    _login(client)

    response = client.get(
        "/app/settings/api/convey/network-access/capability",
        headers={"X-Forwarded-For": "192.168.1.5"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "can_change_network_access": False,
        "network_access_enabled": False,
        "reason": CONVEY_NETWORK_LOCAL_ONLY_REASON,
    }

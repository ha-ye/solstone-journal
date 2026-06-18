# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solstone.apps.settings.call as settings_call
import solstone.apps.settings.routes as settings_routes
from solstone.think.convey_client import ConveyClient
from tests._baseline_harness import make_test_client

runner = CliRunner()

API_ENV_KEYS = (
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "REVAI_ACCESS_TOKEN",
    "PLAUD_ACCESS_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


class _FixedDateTime:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 4, 17, 12, 0, tzinfo=tz or timezone.utc)


@pytest.fixture(autouse=True)
def _settings_client(journal_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    client = ConveyClient(
        session=make_test_client(journal_copy),
        base_url="",
    )
    monkeypatch.setattr(settings_call, "get_client", lambda: client)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture
def fake_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate_token(token: str) -> dict[str, Any]:
        return {"valid": True, "token": token[-4:]}

    monkeypatch.setattr(settings_routes, "datetime", _FixedDateTime)
    monkeypatch.setattr(
        "solstone.observe.transcribe.revai.validate_token", validate_token
    )
    monkeypatch.setattr("solstone.think.importers.plaud.validate_token", validate_token)


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads((journal / "config" / "journal.json").read_text(encoding="utf-8"))


def _write_config(journal: Path, payload: dict[str, Any]) -> None:
    (journal / "config" / "journal.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _assert_json(result, expected: Any) -> None:
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected
    assert result.stderr == ""


def _fake_creds(email: str = "test@test.iam.gserviceaccount.com") -> dict[str, str]:
    return {
        "type": "service_account",
        "project_id": "test-project",
        "client_email": email,
        "private_key": "fake-private-key",
    }


def test_show_and_read_verbs_select_http_fields(journal_copy: Path) -> None:
    show = runner.invoke(settings_call.app, ["show"])
    keys = runner.invoke(settings_call.app, ["keys", "show"])
    providers = runner.invoke(settings_call.app, ["providers", "show"])
    google = runner.invoke(settings_call.app, ["google-backend", "show"])
    transcribe = runner.invoke(settings_call.app, ["transcribe", "show"])
    identity = runner.invoke(settings_call.app, ["identity", "show"])
    observer = runner.invoke(settings_call.app, ["observer", "show"])
    vertex = runner.invoke(settings_call.app, ["vertex-credentials", "show"])

    assert show.exit_code == 0
    show_payload = json.loads(show.stdout)
    assert list(show_payload) == [
        "identity",
        "transcribe",
        "observe",
        "keys",
    ]
    assert show_payload["identity"]["name"] == "Test User"
    assert list(show_payload["keys"]) == [
        "REVAI_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN",
    ]

    assert keys.exit_code == 0
    assert json.loads(keys.stdout) == {key: False for key in show_payload["keys"]}
    for result, command in (
        (providers, "providers show"),
        (google, "google-backend show"),
        (vertex, "vertex-credentials show"),
    ):
        assert result.exit_code == 2
        assert result.stderr == (
            f"Moved to `sol call thinking {command}` — run that instead.\n"
        )
    assert transcribe.exit_code == 0
    assert set(json.loads(transcribe.stdout)) == {"backends", "api_keys", "config"}
    assert identity.exit_code == 0
    assert json.loads(identity.stdout)["name"] == "Test User"
    assert observer.exit_code == 0
    assert json.loads(observer.stdout)["tmux"]["capture_interval"] == 5


def test_settings_config_projects_service_validation_only(journal_copy: Path) -> None:
    config = _read_config(journal_copy)
    config.setdefault("providers", {})["key_validation"] = {
        "revai": {"valid": True, "timestamp": "2026-01-01T00:00:00+00:00"},
        "plaud": {"valid": False, "error": "bad token"},
        "google": {"valid": True},
        "openai": {"valid": True},
        "anthropic": {"valid": True},
        "google_vertex": {"valid": True},
    }
    _write_config(journal_copy, config)

    payload = settings_call.get_client().request("GET", "/app/settings/api/config")

    assert "providers" not in payload
    assert payload["key_validation"] == {
        "revai": {"valid": True, "timestamp": "2026-01-01T00:00:00+00:00"},
        "plaud": {"valid": False, "error": "bad token"},
    }


def test_keys_set_clear_validate_and_invalid_env(
    journal_copy: Path,
    fake_validators: None,
) -> None:
    invalid = runner.invoke(settings_call.app, ["keys", "set", "BOGUS", "value"])
    assert invalid.exit_code == 1
    assert invalid.stderr == (
        "Invalid env var: BOGUS. Must be one of: "
        "REVAI_ACCESS_TOKEN, PLAUD_ACCESS_TOKEN\n"
    )

    moved = runner.invoke(
        settings_call.app,
        ["keys", "set", "ANTHROPIC_API_KEY", "anthropic-test-key"],
    )
    assert moved.exit_code == 2
    assert moved.stderr == ("Moved to `sol call thinking keys …` — run that instead.\n")
    assert "ANTHROPIC_API_KEY" not in _read_config(journal_copy).get("env", {})

    service_set = runner.invoke(
        settings_call.app,
        ["keys", "set", "REVAI_ACCESS_TOKEN", "revai-token"],
    )
    assert service_set.exit_code == 0
    assert json.loads(service_set.stdout) == {
        "env_var": "REVAI_ACCESS_TOKEN",
        "set": True,
        "validation": {
            "valid": True,
            "token": "oken",
            "timestamp": "2026-04-17T12:00:00+00:00",
        },
    }
    assert _read_config(journal_copy)["providers"]["key_validation"]["revai"]["valid"]
    keys_shown = runner.invoke(settings_call.app, ["keys", "show"])
    assert keys_shown.exit_code == 0
    assert "revai-token" not in keys_shown.stdout

    cleared = runner.invoke(settings_call.app, ["keys", "clear", "REVAI_ACCESS_TOKEN"])
    _assert_json(cleared, {"env_var": "REVAI_ACCESS_TOKEN", "cleared": True})
    assert _read_config(journal_copy)["env"]["REVAI_ACCESS_TOKEN"] == ""

    before = (journal_copy / "config" / "journal.json").read_text(encoding="utf-8")
    validate = runner.invoke(settings_call.app, ["keys", "validate"])
    assert validate.exit_code == 0
    assert json.loads(validate.stdout)["key_validation"] == {}
    assert (journal_copy / "config" / "journal.json").read_text(
        encoding="utf-8"
    ) == before

    runner.invoke(
        settings_call.app, ["keys", "set", "PLAUD_ACCESS_TOKEN", "plaud-token"]
    )
    cached = runner.invoke(settings_call.app, ["keys", "validate", "--cache-result"])
    assert cached.exit_code == 0
    assert _read_config(journal_copy)["providers"]["key_validation"]["plaud"]["valid"]


def test_moved_provider_verbs_exit_two() -> None:
    commands = [
        ["providers", "set-generate", "--provider", "openai"],
        ["providers", "set-cogitate", "--provider", "openai"],
        [
            "providers",
            "set-local-endpoint",
            "--url",
            "http://host.test",
            "--model",
            "m",
        ],
        ["providers", "clear-local-endpoint"],
        ["google-backend", "set", "vertex"],
        ["vertex-credentials", "import", "creds.json"],
        ["vertex-credentials", "clear"],
    ]
    for command in commands:
        result = runner.invoke(settings_call.app, command)
        assert result.exit_code == 2
        assert result.stderr.startswith("Moved to `sol call thinking ")


def test_transcribe_setters(journal_copy: Path) -> None:
    transcribe_bad = runner.invoke(
        settings_call.app,
        ["transcribe", "set-backend", "invalid"],
    )
    assert transcribe_bad.exit_code == 1
    assert transcribe_bad.stderr == (
        "Invalid backend: invalid. Must be one of: gemini, parakeet, revai, whisper\n"
    )

    transcribe_set = runner.invoke(
        settings_call.app,
        ["transcribe", "set-backend", "gemini"],
    )
    assert transcribe_set.exit_code == 0
    assert json.loads(transcribe_set.stdout)["backend"] == "gemini"
    assert _read_config(journal_copy)["transcribe"]["backend"] == "gemini"

    options = runner.invoke(
        settings_call.app,
        ["transcribe", "set", "--no-enrich", "--no-noise-upgrade"],
    )
    assert options.exit_code == 0
    payload = json.loads(options.stdout)
    assert payload["enrich"] is False
    assert payload["noise_upgrade"] is False


def test_identity_and_observer_setters(journal_copy: Path) -> None:
    bad_pronouns = runner.invoke(
        settings_call.app,
        ["identity", "set", "--pronouns", "{bad"],
    )
    assert bad_pronouns.exit_code == 1
    assert bad_pronouns.stderr == "Invalid JSON in pronouns\n"

    name = runner.invoke(settings_call.app, ["identity", "set", "--name", "New Name"])
    assert name.exit_code == 0
    assert json.loads(name.stdout)["name"] == "New Name"

    add_email = runner.invoke(
        settings_call.app,
        ["identity", "set", "--add-email", "new@example.com"],
    )
    assert add_email.exit_code == 0
    assert "new@example.com" in json.loads(add_email.stdout)["email_addresses"]

    remove_email = runner.invoke(
        settings_call.app,
        ["identity", "set", "--remove-email", "test@example.com"],
    )
    assert remove_email.exit_code == 0
    assert "test@example.com" not in json.loads(remove_email.stdout)["email_addresses"]

    observer_bad = runner.invoke(
        settings_call.app,
        ["observer", "set", "--capture-interval", "100"],
    )
    assert observer_bad.exit_code == 1
    assert observer_bad.stderr == (
        "tmux.capture_interval must be an integer between 1 and 60\n"
    )

    observer_set = runner.invoke(
        settings_call.app,
        ["observer", "set", "--no-enabled", "--capture-interval", "10"],
    )
    assert observer_set.exit_code == 0
    assert json.loads(observer_set.stdout) == {
        "capture_interval": 10,
        "enabled": False,
    }
    assert _read_config(journal_copy)["observe"]["tmux"] == {
        "capture_interval": 10,
        "enabled": False,
    }


def test_convey_status_host_url(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = runner.invoke(settings_call.app, ["convey", "status"])
    assert status.exit_code == 0
    assert status.stdout == (
        "convey\n"
        "  bind:              127.0.0.1:5015\n"
        "  host url:          http://localhost:5015\n"
    )

    calls = []
    with monkeypatch.context() as m:
        m.setattr(settings_call, "get_client", lambda: calls.append("called"))
        conflict = runner.invoke(
            settings_call.app,
            ["convey", "host-url", "--auto", "--show"],
        )
    assert conflict.exit_code == 1
    assert conflict.stderr == "error: choose exactly one of <url>, --auto, or --show\n"
    assert calls == []

    set_url = runner.invoke(
        settings_call.app,
        ["convey", "host-url", "192.168.1.44:5015"],
    )
    assert set_url.exit_code == 0
    assert set_url.stdout == "host url set: http://192.168.1.44:5015\n"

    show_url = runner.invoke(settings_call.app, ["convey", "host-url", "--show"])
    assert show_url.exit_code == 0
    assert show_url.stdout == "http://192.168.1.44:5015\n"

    manual_status = runner.invoke(settings_call.app, ["convey", "status"])
    assert manual_status.exit_code == 0
    assert "host url:          http://192.168.1.44:5015" in manual_status.stdout

    auto = runner.invoke(settings_call.app, ["convey", "host-url", "--auto"])
    assert auto.exit_code == 0
    assert auto.stdout == "host url cleared. auto-detect is active.\n"

    bad_url = runner.invoke(settings_call.app, ["convey", "host-url", "/bad"])
    assert bad_url.exit_code == 1
    assert bad_url.stderr == "enter an ipv4 address and port, like 192.168.1.44:7657\n"

    bad_host = runner.invoke(
        settings_call.app,
        ["convey", "host-url", "mylab.local:5015"],
    )
    assert bad_host.exit_code == 1
    assert bad_host.stderr == (
        "this needs an ip address — to reach home by name from anywhere, "
        "set up solstone private link\n"
    )

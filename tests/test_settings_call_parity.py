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
from tests._baseline_harness import make_logged_in_test_client

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
        session=make_logged_in_test_client(journal_copy),
        base_url="",
    )
    monkeypatch.setattr(settings_call, "get_client", lambda: client)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture
def fake_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate_key(provider: str, api_key: str) -> dict[str, Any]:
        return {"valid": True, "provider": provider, "fingerprint": api_key[-4:]}

    def validate_token(token: str) -> dict[str, Any]:
        return {"valid": True, "token": token[-4:]}

    def validate_vertex(path: str) -> dict[str, Any]:
        return {"valid": True, "path": path}

    monkeypatch.setattr(settings_routes, "datetime", _FixedDateTime)
    monkeypatch.setattr("solstone.think.providers.validate_key", validate_key)
    monkeypatch.setattr(
        "solstone.observe.transcribe.revai.validate_token", validate_token
    )
    monkeypatch.setattr("solstone.think.importers.plaud.validate_token", validate_token)
    monkeypatch.setattr(settings_routes, "validate_vertex_credentials", validate_vertex)


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
        "providers",
        "transcribe",
        "observe",
        "keys",
    ]
    assert show_payload["identity"]["name"] == "Test User"
    assert show_payload["providers"]["generate"]["provider"] == "google"
    assert list(show_payload["keys"]) == [
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "REVAI_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN",
    ]

    assert keys.exit_code == 0
    assert json.loads(keys.stdout) == {key: False for key in show_payload["keys"]}
    assert providers.exit_code == 0
    assert json.loads(providers.stdout)["cogitate"]["provider"] == "openai"
    assert google.exit_code == 0
    assert json.loads(google.stdout) == {
        "google_backend": "auto",
        "vertex_credentials_configured": False,
        "vertex_credentials_email": "",
    }
    assert transcribe.exit_code == 0
    assert set(json.loads(transcribe.stdout)) == {"backends", "api_keys", "config"}
    assert identity.exit_code == 0
    assert json.loads(identity.stdout)["name"] == "Test User"
    assert observer.exit_code == 0
    assert json.loads(observer.stdout)["tmux"]["capture_interval"] == 5
    assert vertex.exit_code == 0
    assert json.loads(vertex.stdout)["configured"] is False


def test_keys_set_clear_validate_and_invalid_env(
    journal_copy: Path,
    fake_validators: None,
) -> None:
    invalid = runner.invoke(settings_call.app, ["keys", "set", "BOGUS", "value"])
    assert invalid.exit_code == 1
    assert invalid.stderr == (
        "Invalid env var: BOGUS. Must be one of: "
        "GOOGLE_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, "
        "REVAI_ACCESS_TOKEN, PLAUD_ACCESS_TOKEN\n"
    )

    provider_set = runner.invoke(
        settings_call.app,
        ["keys", "set", "ANTHROPIC_API_KEY", "anthropic-test-key"],
    )
    assert provider_set.exit_code == 0
    assert json.loads(provider_set.stdout) == {
        "env_var": "ANTHROPIC_API_KEY",
        "set": True,
        "validation": {
            "valid": True,
            "provider": "anthropic",
            "fingerprint": "-key",
            "timestamp": "2026-04-17T12:00:00+00:00",
        },
    }
    assert (
        _read_config(journal_copy)["env"]["ANTHROPIC_API_KEY"] == "anthropic-test-key"
    )

    service_set = runner.invoke(
        settings_call.app,
        ["keys", "set", "REVAI_ACCESS_TOKEN", "revai-token"],
    )
    assert service_set.exit_code == 0
    assert json.loads(service_set.stdout) == {
        "env_var": "REVAI_ACCESS_TOKEN",
        "set": True,
        "validation": None,
    }
    assert _read_config(journal_copy)["providers"]["key_validation"]["revai"]["valid"]
    keys_shown = runner.invoke(settings_call.app, ["keys", "show"])
    assert keys_shown.exit_code == 0
    assert "anthropic-test-key" not in keys_shown.stdout
    assert "revai-token" not in keys_shown.stdout

    cleared = runner.invoke(settings_call.app, ["keys", "clear", "ANTHROPIC_API_KEY"])
    _assert_json(cleared, {"env_var": "ANTHROPIC_API_KEY", "cleared": True})
    assert _read_config(journal_copy)["env"]["ANTHROPIC_API_KEY"] == ""

    before = (journal_copy / "config" / "journal.json").read_text(encoding="utf-8")
    validate = runner.invoke(settings_call.app, ["keys", "validate"])
    assert validate.exit_code == 0
    assert json.loads(validate.stdout)["key_validation"]["revai"]["valid"] is True
    assert (journal_copy / "config" / "journal.json").read_text(
        encoding="utf-8"
    ) == before

    cached = runner.invoke(settings_call.app, ["keys", "validate", "--cache-result"])
    assert cached.exit_code == 0
    assert _read_config(journal_copy)["providers"]["key_validation"]["revai"]["valid"]


def test_providers_show_human_and_set_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_status = {
        "anthropic": {"issues": ["ANTHROPIC_API_KEY not set"]},
        "google": {"generate_ready": True, "cogitate_ready": True, "issues": []},
        "local": {
            "generate_ready": False,
            "cogitate_ready": False,
            "cogitate_cli": "llama-server",
            "issues": ["binary_missing"],
        },
        "openai": {"generate_ready": True, "cogitate_ready": True, "issues": []},
    }
    monkeypatch.setattr(
        "solstone.think.providers.build_provider_status",
        lambda providers, vertex_creds_configured: provider_status,
    )

    human = runner.invoke(settings_call.app, ["providers", "show", "--human"])
    assert human.exit_code == 0
    assert human.stdout == (
        "anthropic: ANTHROPIC_API_KEY not set\n"
        "google: ready\n"
        "local: binary_missing\n"
        "openai: ready\n"
    )

    success = runner.invoke(
        settings_call.app,
        ["providers", "set-generate", "--provider", "openai"],
    )
    assert success.exit_code == 0
    assert json.loads(success.stdout)["provider"] == "openai"

    bad_provider = runner.invoke(
        settings_call.app,
        ["providers", "set-generate", "--provider", "invalid"],
    )
    assert bad_provider.exit_code == 1
    assert bad_provider.stderr == (
        "Invalid provider: invalid. Must be one of: anthropic, google, local, openai\n"
    )

    bad_backup = runner.invoke(
        settings_call.app,
        ["providers", "set-cogitate", "--backup", "invalid"],
    )
    assert bad_backup.exit_code == 1
    assert bad_backup.stderr == (
        "Invalid backup provider: invalid. Must be one of: "
        "anthropic, google, local, openai\n"
    )

    bad_tier = runner.invoke(
        settings_call.app,
        ["providers", "set-generate", "--tier", "9"],
    )
    assert bad_tier.exit_code == 1
    assert bad_tier.stderr == "Invalid tier: 9. Must be 1, 2, or 3.\n"


def test_google_backend_and_transcribe_setters(journal_copy: Path) -> None:
    google_bad = runner.invoke(settings_call.app, ["google-backend", "set", "invalid"])
    assert google_bad.exit_code == 1
    assert google_bad.stderr == (
        "Invalid google_backend: invalid. Must be 'auto', 'aistudio', or 'vertex'.\n"
    )

    google_set = runner.invoke(settings_call.app, ["google-backend", "set", "vertex"])
    _assert_json(google_set, {"google_backend": "vertex"})
    assert _read_config(journal_copy)["providers"]["google_backend"] == "vertex"

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


def test_vertex_credentials_import_show_clear_and_errors(
    journal_copy: Path,
    tmp_path: Path,
    fake_validators: None,
) -> None:
    missing = runner.invoke(
        settings_call.app,
        ["vertex-credentials", "import", str(tmp_path / "missing.json")],
    )
    assert missing.exit_code == 1
    assert missing.stderr == f"Credential file not found: {tmp_path / 'missing.json'}\n"

    bad_json_path = tmp_path / "bad.json"
    bad_json_path.write_text("{ bad json", encoding="utf-8")
    bad_json = runner.invoke(
        settings_call.app,
        ["vertex-credentials", "import", str(bad_json_path)],
    )
    assert bad_json.exit_code == 1
    assert bad_json.stderr == f"Invalid JSON in credential file: {bad_json_path}\n"

    missing_fields_path = tmp_path / "missing-fields.json"
    missing_fields_path.write_text(
        json.dumps({"type": "service_account"}),
        encoding="utf-8",
    )
    missing_fields = runner.invoke(
        settings_call.app,
        ["vertex-credentials", "import", str(missing_fields_path)],
    )
    assert missing_fields.exit_code == 1
    assert missing_fields.stderr == (
        "Missing required fields: project_id, client_email, private_key\n"
    )

    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps(_fake_creds()), encoding="utf-8")
    imported = runner.invoke(
        settings_call.app,
        ["vertex-credentials", "import", str(creds_path), "--skip-validation"],
    )
    assert imported.exit_code == 0
    payload = json.loads(imported.stdout)
    canonical = journal_copy / ".config" / "vertex-credentials.json"
    assert payload == {
        "configured": True,
        "email": "test@test.iam.gserviceaccount.com",
        "path": str(canonical),
        "validation": None,
    }
    assert canonical.exists()
    assert "fake-private-key" not in imported.stdout

    shown = runner.invoke(settings_call.app, ["vertex-credentials", "show"])
    assert shown.exit_code == 0
    shown_payload = json.loads(shown.stdout)
    assert shown_payload["configured"] is True
    assert shown_payload["email"] == "test@test.iam.gserviceaccount.com"
    assert shown_payload["path"] == str(canonical)
    assert "fake-private-key" not in shown.stdout

    cleared = runner.invoke(settings_call.app, ["vertex-credentials", "clear"])
    _assert_json(cleared, {"configured": False})
    assert not canonical.exists()


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


def test_convey_status_host_url_and_trust_localhost(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = runner.invoke(settings_call.app, ["convey", "status"])
    assert status.exit_code == 0
    assert status.stdout == (
        "convey\n"
        "  network access:    localhost only\n"
        "  bind:              127.0.0.1:5015\n"
        "  password:          set\n"
        "  trust localhost:   yes\n"
        "  host url:          http://localhost:5015 (localhost — network access off)\n"
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
    assert "host url:          http://192.168.1.44:5015 (manual override)" in (
        manual_status.stdout
    )

    auto = runner.invoke(settings_call.app, ["convey", "host-url", "--auto"])
    assert auto.exit_code == 0
    assert auto.stdout == "host url cleared. auto-detect is active.\n"

    bad_url = runner.invoke(settings_call.app, ["convey", "host-url", "/bad"])
    assert bad_url.exit_code == 1
    assert bad_url.stderr == "enter an ipv4 address and port, like 192.168.1.44:5015\n"

    bad_host = runner.invoke(
        settings_call.app,
        ["convey", "host-url", "mylab.local:5015"],
    )
    assert bad_host.exit_code == 1
    assert bad_host.stderr == (
        "this needs an ip address — to reach home by name from anywhere, "
        "set up sol private link\n"
    )

    config = _read_config(journal_copy)
    config["convey"]["allow_network_access"] = True
    _write_config(journal_copy, config)
    health_dir = journal_copy / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "convey.port").write_text("6123", encoding="utf-8")
    monkeypatch.setattr(
        "solstone.think.pairing.config._detect_lan_ipv4",
        lambda: "192.168.1.44",
    )
    auto_status = runner.invoke(settings_call.app, ["convey", "status"])
    assert auto_status.exit_code == 0
    assert auto_status.stdout == (
        "convey\n"
        "  network access:    on\n"
        "  bind:              0.0.0.0:6123\n"
        "  password:          set\n"
        "  trust localhost:   yes\n"
        "  host url:          http://192.168.1.44:6123 (auto-detected)\n"
    )

    trust_enable = runner.invoke(
        settings_call.app, ["convey", "trust-localhost", "enable"]
    )
    assert trust_enable.exit_code == 0
    assert (
        trust_enable.stdout
        == "localhost trust enabled. localhost requests skip the password.\n"
    )

    trust_disable = runner.invoke(
        settings_call.app, ["convey", "trust-localhost", "disable"]
    )
    assert trust_disable.exit_code == 0
    assert trust_disable.stdout == (
        "localhost trust disabled. localhost requests now require the password.\n"
    )

    config = _read_config(journal_copy)
    config["convey"].pop("password_hash", None)
    config["convey"].pop("password", None)
    _write_config(journal_copy, config)
    trust_refuse = runner.invoke(
        settings_call.app, ["convey", "trust-localhost", "disable"]
    )
    assert trust_refuse.exit_code == 1
    assert trust_refuse.stderr == (
        "error: disabling localhost trust requires a password (otherwise no "
        "client could authenticate). set one first with: journal password set\n"
    )

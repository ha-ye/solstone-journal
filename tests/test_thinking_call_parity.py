# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solstone.apps.thinking.call as thinking_call
import solstone.apps.thinking.routes as thinking_routes
from solstone.apps.thinking import copy as thinking_copy
from solstone.think.convey_client import ConveyClient
from solstone.think.services import operations, scout, scout_handoff
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
def _thinking_client(journal_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    client = ConveyClient(
        session=make_test_client(journal_copy),
        base_url="",
    )
    monkeypatch.setattr(thinking_call, "get_client", lambda: client)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture(autouse=True)
def _clear_service_operations() -> None:
    operations.clear_registry()
    yield
    operations.clear_registry()


@pytest.fixture
def fake_validators(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate_key(provider: str, api_key: str) -> dict[str, Any]:
        return {"valid": True, "provider": provider, "fingerprint": api_key[-4:]}

    def validate_vertex(path: str) -> dict[str, Any]:
        return {"valid": True, "path": path}

    monkeypatch.setattr(thinking_routes, "datetime", _FixedDateTime)
    monkeypatch.setattr(thinking_routes, "validate_key", validate_key)
    monkeypatch.setattr(thinking_routes, "validate_vertex_credentials", validate_vertex)


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


def _fake_creds(email: str = "test@test.iam.gservice.test") -> dict[str, str]:
    return {
        "type": "service_credential",
        "project_id": "test-project",
        "client_email": email,
        "private_key": "fake-private-key",
    }


def _approved_scout_payload(key: str = "google-scout-key") -> dict[str, str]:
    return {
        "state": "approved",
        "google_api_key": key,
        "dispatch_token": "dispatch-secret",
        "account_id": "acct-secret",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _clear_scout(journal: Path) -> None:
    config = _read_config(journal)
    config.setdefault("env", {}).pop("GOOGLE_API_KEY", None)
    config.setdefault("services", {}).pop("scout", None)
    _write_config(journal, config)


def _first_json(stdout: str) -> tuple[Any, str]:
    payload, index = json.JSONDecoder().raw_decode(stdout)
    return payload, stdout[index:].strip()


def test_show_verbs_select_http_fields() -> None:
    keys = runner.invoke(thinking_call.app, ["keys", "show"])
    providers = runner.invoke(thinking_call.app, ["providers", "show"])
    google = runner.invoke(thinking_call.app, ["google-backend", "show"])
    vertex = runner.invoke(thinking_call.app, ["vertex-credentials", "show"])

    assert keys.exit_code == 0
    keys_payload = json.loads(keys.stdout)
    assert set(keys_payload) == {"api_keys", "env", "key_validation"}
    assert set(keys_payload["env"]) == {
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    }

    assert providers.exit_code == 0
    providers_payload = json.loads(providers.stdout)
    assert providers_payload["generate"]["provider"] == "google"
    assert providers_payload["cogitate"]["provider"] == "openai"
    assert providers_payload["local_override"]["enabled"] is False
    assert providers_payload["active_lane"]["lane"] == "byo"

    assert google.exit_code == 0
    assert json.loads(google.stdout) == {
        "google_backend": "auto",
        "vertex_credentials_configured": False,
        "vertex_credentials_email": "",
    }
    assert vertex.exit_code == 0
    assert json.loads(vertex.stdout)["configured"] is False


def test_scout_status_matches_http_payload(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    expected = thinking_call._get_scout_status()

    result = runner.invoke(thinking_call.app, ["scout", "status"])

    assert result.exit_code == 0
    payload, guidance = _first_json(result.stdout)
    assert payload == expected
    assert guidance == thinking_call._SCOUT_GUIDANCE[thinking_copy.SCOUT_STATE_OFF]


def test_scout_check_matches_http_response(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    expected = thinking_call._request(
        "POST",
        "/app/thinking/api/scout/check",
    )

    result = runner.invoke(thinking_call.app, ["scout", "check"])

    assert result.exit_code == 0
    payload, guidance = _first_json(result.stdout)
    assert payload == expected
    assert guidance == thinking_call._SCOUT_GUIDANCE[payload["state"]]


def test_scout_disable_matches_http_response(journal_copy: Path) -> None:
    _clear_scout(journal_copy)
    scout.provision_scout_handoff(_approved_scout_payload())
    expected_response = thinking_call._request(
        "POST",
        "/app/thinking/api/scout/disable",
    )

    scout.provision_scout_handoff(_approved_scout_payload())
    result = runner.invoke(thinking_call.app, ["scout", "disable"])

    _assert_json(
        result,
        {
            "result": expected_response["result"],
            "status": expected_response["status"],
        },
    )


def test_scout_enable_polls_terminal_success(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_scout(journal_copy)

    def runner_result(**_kwargs):
        scout.provision_scout_handoff(_approved_scout_payload())
        return operations.HandoffResult("enabled", None, False)

    monkeypatch.setattr(scout_handoff, "run_scout_handoff", runner_result)

    result = runner.invoke(
        thinking_call.app,
        ["scout", "enable", "--wait-seconds", "2", "--poll-interval", "0"],
    )

    assert result.exit_code == 0
    assert result.stderr == ""
    # Status and operation are sampled non-atomically; phase is stable here.
    assert "operation: invited\n" in result.stdout
    assert thinking_call._SCOUT_GUIDANCE[thinking_copy.SCOUT_STATE_INVITED] in (
        result.stdout
    )


def test_scout_enable_exits_nonzero_on_repair_needed(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_scout(journal_copy)
    monkeypatch.setattr(
        scout_handoff,
        "run_scout_handoff",
        lambda **_kwargs: operations.HandoffResult(
            "error",
            "Try again.",
            True,
        ),
    )

    result = runner.invoke(
        thinking_call.app,
        ["scout", "enable", "--wait-seconds", "2", "--poll-interval", "0"],
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    assert "operation: repair_needed\n" in result.stdout
    assert "Try again.\n" in result.stdout
    assert (
        thinking_call._SCOUT_GUIDANCE[thinking_copy.SCOUT_STATE_REPAIR_NEEDED]
        in result.stdout
    )


def test_scout_cli_copy_mirror_matches_thinking_copy() -> None:
    local_states = {
        thinking_call._SCOUT_STATE_OFF,
        thinking_call._SCOUT_STATE_REQUESTED,
        thinking_call._SCOUT_STATE_INVITED,
        thinking_call._SCOUT_STATE_ON,
        thinking_call._SCOUT_STATE_ENDED,
        thinking_call._SCOUT_STATE_MANUAL_KEY_PRESENT,
        thinking_call._SCOUT_STATE_REPAIR_NEEDED,
    }

    assert local_states == set(thinking_copy.SCOUT_STATE_LABELS)
    assert thinking_call._SCOUT_PRODUCT_STATES == set(thinking_copy.SCOUT_STATE_LABELS)
    assert set(thinking_call._SCOUT_GUIDANCE) == set(thinking_copy.SCOUT_STATE_LABELS)
    assert thinking_call._SCOUT_TERMINAL_PHASES == {
        thinking_copy.SCOUT_STATE_INVITED,
        thinking_copy.SCOUT_STATE_REQUESTED,
        thinking_copy.SCOUT_STATE_ENDED,
        thinking_copy.SCOUT_STATE_REPAIR_NEEDED,
    }
    assert thinking_call._SCOUT_CONSENT_CTA == thinking_copy.SCOUT_CONSENT_CTA


def test_keys_set_clear_validate_and_invalid_env(
    journal_copy: Path,
    fake_validators: None,
) -> None:
    invalid = runner.invoke(thinking_call.app, ["keys", "set", "BOGUS", "value"])
    assert invalid.exit_code == 1
    assert invalid.stderr == (
        "Invalid env var: BOGUS. Must be one of: "
        "GOOGLE_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY\n"
    )

    provider_set = runner.invoke(
        thinking_call.app,
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

    keys_shown = runner.invoke(thinking_call.app, ["keys", "show"])
    assert keys_shown.exit_code == 0
    assert "anthropic-test-key" not in keys_shown.stdout

    cleared = runner.invoke(thinking_call.app, ["keys", "clear", "ANTHROPIC_API_KEY"])
    _assert_json(cleared, {"env_var": "ANTHROPIC_API_KEY", "cleared": True})
    assert "ANTHROPIC_API_KEY" not in _read_config(journal_copy)["env"]

    before = (journal_copy / "config" / "journal.json").read_text(encoding="utf-8")
    validate = runner.invoke(thinking_call.app, ["keys", "validate"])
    assert validate.exit_code == 0
    assert (journal_copy / "config" / "journal.json").read_text(
        encoding="utf-8"
    ) == before

    cached = runner.invoke(thinking_call.app, ["keys", "validate", "--cache-result"])
    assert cached.exit_code == 0


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
        thinking_routes,
        "build_provider_status",
        lambda providers, vertex_creds_configured: provider_status,
    )

    human = runner.invoke(thinking_call.app, ["providers", "show", "--human"])
    assert human.exit_code == 0
    assert human.stdout == (
        "active lane: byo\n"
        "anthropic: ANTHROPIC_API_KEY not set\n"
        "google: ready\n"
        "local: binary_missing\n"
        "openai: ready\n"
    )

    success = runner.invoke(
        thinking_call.app,
        ["providers", "set-generate", "--provider", "openai"],
    )
    assert success.exit_code == 0
    assert json.loads(success.stdout)["provider"] == "openai"

    bad_provider = runner.invoke(
        thinking_call.app,
        ["providers", "set-generate", "--provider", "invalid"],
    )
    assert bad_provider.exit_code == 1
    assert bad_provider.stderr == (
        "Invalid provider: invalid. Must be one of: anthropic, google, openai, local\n"
    )

    bad_tier = runner.invoke(
        thinking_call.app,
        ["providers", "set-generate", "--tier", "9"],
    )
    assert bad_tier.exit_code == 1
    assert bad_tier.stderr == "Invalid tier: 9. Must be one of: 1, 2, 3\n"


def test_local_endpoint_verbs_use_http_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_body": json_body,
            }
        )
        return {
            "local_endpoint": {
                "enabled": method != "DELETE",
                "endpoint_url": "http://host.test",
                "served_model_id": "served-model",
                "credential_configured": bool((json_body or {}).get("credential")),
            }
        }

    monkeypatch.setattr(thinking_call, "_request", fake_request)

    no_credential = runner.invoke(
        thinking_call.app,
        [
            "set-local-endpoint",
            "--url",
            "http://host.test",
            "--model",
            "served-model",
        ],
    )

    assert no_credential.exit_code == 0
    assert calls[-1] == {
        "method": "POST",
        "path": "/app/thinking/api/local/endpoint",
        "params": None,
        "json_body": {
            "endpoint_url": "http://host.test",
            "served_model_id": "served-model",
        },
    }

    with_credential = runner.invoke(
        thinking_call.app,
        [
            "set-local-endpoint",
            "--url",
            "http://host.test",
            "--model",
            "served-model",
            "--credential",
            "test-token-PLACEHOLDER",
        ],
    )

    assert with_credential.exit_code == 0
    assert calls[-1]["json_body"] == {
        "endpoint_url": "http://host.test",
        "served_model_id": "served-model",
        "credential": "test-token-PLACEHOLDER",
    }

    cleared = runner.invoke(thinking_call.app, ["clear-local-endpoint"])

    assert cleared.exit_code == 0
    assert calls[-1] == {
        "method": "DELETE",
        "path": "/app/thinking/api/local/endpoint",
        "params": None,
        "json_body": None,
    }


def test_google_backend_and_vertex_credentials(
    journal_copy: Path,
    tmp_path: Path,
    fake_validators: None,
) -> None:
    google_bad = runner.invoke(thinking_call.app, ["google-backend", "set", "invalid"])
    assert google_bad.exit_code == 1
    assert google_bad.stderr == (
        "Invalid google_backend: invalid. Must be one of: auto, aistudio, vertex\n"
    )

    google_set = runner.invoke(thinking_call.app, ["google-backend", "set", "vertex"])
    _assert_json(google_set, {"google_backend": "vertex"})
    assert _read_config(journal_copy)["providers"]["google_backend"] == "vertex"

    missing = runner.invoke(
        thinking_call.app,
        ["vertex-credentials", "import", str(tmp_path / "missing.json")],
    )
    assert missing.exit_code == 1
    assert missing.stderr == f"Credential file not found: {tmp_path / 'missing.json'}\n"

    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps(_fake_creds()), encoding="utf-8")
    imported = runner.invoke(
        thinking_call.app,
        ["vertex-credentials", "import", str(creds_path), "--skip-validation"],
    )
    assert imported.exit_code == 0
    payload = json.loads(imported.stdout)
    canonical = journal_copy / ".config" / "vertex-credentials.json"
    assert payload == {
        "configured": True,
        "email": "test@test.iam.gservice.test",
        "path": str(canonical),
        "validation": None,
    }
    assert canonical.exists()
    assert "fake-private-key" not in imported.stdout

    shown = runner.invoke(thinking_call.app, ["vertex-credentials", "show"])
    assert shown.exit_code == 0
    shown_payload = json.loads(shown.stdout)
    assert shown_payload["configured"] is True
    assert shown_payload["email"] == "test@test.iam.gservice.test"
    assert "fake-private-key" not in shown.stdout

    cleared = runner.invoke(thinking_call.app, ["vertex-credentials", "clear"])
    _assert_json(cleared, {"configured": False})
    assert not canonical.exists()


def test_local_verbs_hit_expected_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_body": json_body,
            }
        )
        if path == "/app/thinking/api/providers":
            return {"ai_readiness": {"local": {"status": "ready"}}}
        return {"ok": True}

    monkeypatch.setattr(thinking_call, "_request", fake_request)

    assert runner.invoke(thinking_call.app, ["local", "readiness"]).exit_code == 0
    assert runner.invoke(thinking_call.app, ["local", "status"]).exit_code == 0
    assert (
        runner.invoke(
            thinking_call.app, ["local", "availability", "--model", "m"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            thinking_call.app, ["local", "bootstrap", "--model", "m"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            thinking_call.app, ["local", "bootstrap-status", "--model", "m"]
        ).exit_code
        == 0
    )
    assert runner.invoke(thinking_call.app, ["local", "models"]).exit_code == 0

    assert calls == [
        {
            "method": "GET",
            "path": "/app/thinking/api/providers",
            "params": None,
            "json_body": None,
        },
        {
            "method": "GET",
            "path": "/app/thinking/api/providers/local/status",
            "params": None,
            "json_body": None,
        },
        {
            "method": "GET",
            "path": "/app/thinking/api/local/availability",
            "params": {"model": "m"},
            "json_body": None,
        },
        {
            "method": "POST",
            "path": "/app/thinking/api/local/bootstrap",
            "params": {"model": "m"},
            "json_body": None,
        },
        {
            "method": "GET",
            "path": "/app/thinking/api/local/bootstrap/status",
            "params": {"model": "m"},
            "json_body": None,
        },
        {
            "method": "GET",
            "path": "/app/thinking/api/local/models",
            "params": None,
            "json_body": None,
        },
    ]

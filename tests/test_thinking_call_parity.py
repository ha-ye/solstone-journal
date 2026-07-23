# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solstone.apps.thinking.call as thinking_call
import solstone.apps.thinking.routes as thinking_routes
from solstone.apps.thinking import copy as thinking_copy
from solstone.think.convey_client import ConveyClient, ConveyUnreachableError
from solstone.think.services import operations, scout, scout_handoff, spp, spp_handoff
from tests._baseline_harness import make_test_client
from tests.helpers.module_mocks import inline_thread_constructor, module_mock

runner = CliRunner()

API_ENV_KEYS = (
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "PLAUD_ACCESS_TOKEN",
)


class _FixedDateTime:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 4, 17, 12, 0, tzinfo=tz or timezone.utc)


@pytest.fixture(autouse=True)
def _thinking_client(journal_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    config = json.loads(
        (journal_copy / "config" / "journal.json").read_text(encoding="utf-8")
    )
    providers = config.setdefault("providers", {})
    providers.pop("generate", None)
    providers.pop("cogitate", None)
    providers["active"] = {
        "provider": "google",
        "model": "gemini-custom-flash-test",
    }
    (journal_copy / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
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

    monkeypatch.setattr(thinking_routes, "datetime", _FixedDateTime)
    monkeypatch.setattr(thinking_routes, "validate_key", validate_key)


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


def _approved_scout_payload(key: str = "google-scout-key") -> dict[str, str]:
    return {
        "state": "approved",
        "google_api_key": key,
        "dispatch_token": "dispatch-secret",
        "account_id": "acct-secret",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _confidential_payload(suffix: str = "one") -> dict[str, str]:
    return {
        "endpoint_url": f"https://spp-{suffix}.example.test/v1",
        "served_model_id": f"confidential-model-{suffix}",
        "credential": f"credential-{suffix}",
        "account_id": f"acct-{suffix}",
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


def _stable_confidential_handoff_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        spp_handoff,
        "build_confidential_handoff_url",
        lambda: (
            "http://portal.test/enable/spp?nonce=NONCE",
            "NONCE",
            "http://portal.test",
        ),
    )


def _confidential_status_subset(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "confidential_enabled": state.get("confidential_enabled"),
        "confidential_provenance_configured": state.get(
            "confidential_provenance_configured"
        ),
        "confidential_operation": state.get("confidential_operation"),
        "confidential_attestation": state.get("confidential_attestation"),
    }


def _checked_confidential_attestation() -> dict[str, str | None]:
    return {
        "state": "stale",
        "reason": "brain_record_missing",
        "observed_at": None,
        "expires_at": None,
    }


def _assert_no_confidential_secret(output: str, suffix: str = "secret") -> None:
    assert f"credential-{suffix}" not in output
    assert f"acct-{suffix}" not in output


def test_show_verbs_select_http_fields() -> None:
    keys = runner.invoke(thinking_call.app, ["keys", "show"])
    providers = runner.invoke(thinking_call.app, ["providers", "show"])

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
    assert providers_payload["active"]["provider"] == "google"
    assert providers_payload["local_override"]["enabled"] is False
    assert providers_payload["active_lane"]["lane"] == "byo"
    assert providers_payload["active_lane"]["confidential_attestation"] == {
        "state": "off",
        "reason": "confidential_not_configured",
        "observed_at": None,
        "expires_at": None,
    }


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
    monkeypatch.setattr(
        operations,
        "threading",
        module_mock(
            operations.threading,
            Thread=inline_thread_constructor(),
        ),
    )

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


def test_confidential_status_matches_http_active_lane_subset(
    journal_copy: Path,
) -> None:
    before = (journal_copy / "config" / "journal.json").read_text(encoding="utf-8")
    expected = _confidential_status_subset(thinking_call._get_confidential_state())

    result = runner.invoke(thinking_call.app, ["confidential", "status"])

    _assert_json(result, expected)
    assert (journal_copy / "config" / "journal.json").read_text(
        encoding="utf-8"
    ) == before


@pytest.mark.parametrize(
    ("refresh_ok", "expected_error"),
    [
        (True, None),
        (False, "check_not_started"),
    ],
)
def test_confidential_recheck_rereads_attestation(
    monkeypatch: pytest.MonkeyPatch,
    refresh_ok: bool,
    expected_error: str | None,
) -> None:
    spp.provision_confidential_handoff(_confidential_payload("recheck"))
    monkeypatch.setattr(
        thinking_routes,
        "request_brain_refresh",
        lambda *, surface: surface == "thinking" and refresh_ok,
    )
    monkeypatch.setattr(
        thinking_routes,
        "build_brain_snapshot",
        lambda *_args, **_kwargs: {"state": "checking"},
    )

    result = runner.invoke(thinking_call.app, ["confidential", "recheck"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    expected = {
        "ok": refresh_ok,
        "attestation": thinking_call._get_confidential_state().get(
            "confidential_attestation"
        ),
    }
    if expected_error is not None:
        expected["error"] = expected_error
    assert payload == expected
    assert "brain" not in payload


_NOT_VERIFIED_GUIDANCE = (
    "Hardware attestation is not yet verified. "
    "Thinking stays blocked until verification finishes."
)


@pytest.mark.parametrize(
    (
        "raw_phase",
        "guidance",
        "retryable",
        "subscribe_url",
        "expected_phase",
        "expected_exit",
    ),
    [
        (
            "enabled",
            _NOT_VERIFIED_GUIDANCE,
            False,
            None,
            "not_verified",
            0,
        ),
        ("error", "Try again.", True, None, "repair_needed", 1),
        ("early_access", None, False, None, "early_access", 1),
        (
            "needs_subscription",
            "Subscription required.",
            False,
            "https://subscribe.example.test",
            "needs_subscription",
            1,
        ),
        ("revoked", "Access was revoked.", False, None, "revoked", 1),
    ],
)
def test_confidential_enable_terminal_phase_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    raw_phase: str,
    guidance: str | None,
    retryable: bool,
    subscribe_url: str | None,
    expected_phase: str,
    expected_exit: int,
) -> None:
    _stable_confidential_handoff_url(monkeypatch)

    def runner_result(**_kwargs):
        if raw_phase == "enabled":
            spp.provision_confidential_handoff(_confidential_payload("terminal"))
        return operations.HandoffResult(
            raw_phase,
            guidance,
            retryable,
            subscribe_url=subscribe_url,
        )

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner_result)
    monkeypatch.setattr(
        operations,
        "threading",
        module_mock(
            operations.threading,
            Thread=inline_thread_constructor(),
        ),
    )

    result = runner.invoke(
        thinking_call.app,
        ["confidential", "enable", "--wait-seconds", "2", "--poll-interval", "0"],
    )

    output = result.stdout + result.stderr
    assert result.exit_code == expected_exit
    assert result.stderr == ""
    assert (
        "continue in browser → http://portal.test/enable/spp?nonce=NONCE"
        in result.stdout
    )
    assert f"operation: {expected_phase}\n" in result.stdout
    if guidance:
        assert guidance in result.stdout
    if subscribe_url:
        assert f"subscribe_url: {subscribe_url}\n" in result.stdout
    if expected_exit == 0:
        assert "next: sol call thinking confidential recheck\n" in result.stdout
        assert "attestation_state: verified" not in result.stdout
    else:
        assert "next: sol call thinking confidential recheck" not in result.stdout
    assert "credential-terminal" not in output
    assert "acct-terminal" not in output


def test_confidential_enable_timeout_does_not_fabricate_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stable_confidential_handoff_url(monkeypatch)
    release = threading.Event()
    finished = threading.Event()

    def runner_result(**_kwargs):
        try:
            release.wait(5)
            return operations.HandoffResult("pending", "Still pending.", False)
        finally:
            finished.set()

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner_result)

    try:
        result = runner.invoke(
            thinking_call.app,
            [
                "confidential",
                "enable",
                "--wait-seconds",
                "0",
                "--poll-interval",
                "0",
            ],
        )
    finally:
        release.set()
        finished.wait(1)

    output = result.stdout + result.stderr
    assert result.exit_code == 1
    assert "operation continues server-side" in output
    assert "sol call thinking confidential status" in output
    assert "repair_needed" not in output
    assert "Timed out waiting for Scout" not in output


@pytest.mark.parametrize(
    ("configured", "expected_exit"),
    [
        (True, 0),
        (False, 1),
    ],
)
def test_confidential_enable_swept_operation_resolution(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    expected_exit: int,
) -> None:
    def fake_post_confidential_action(path: str) -> dict[str, Any]:
        assert path == "/app/thinking/api/confidential/enable"
        return {
            "operation": {
                "phase": "starting",
                "portal_url": "http://portal.test/enable/spp?nonce=NONCE",
            }
        }

    monkeypatch.setattr(
        thinking_call,
        "_post_confidential_action",
        fake_post_confidential_action,
    )
    attestation = _checked_confidential_attestation()
    states = [
        {
            "confidential_enabled": False,
            "confidential_provenance_configured": False,
            "confidential_operation": {
                "phase": "starting",
                "guidance": None,
                "subscribe_url": None,
            },
            "confidential_attestation": attestation,
        },
        {
            "confidential_enabled": configured,
            "confidential_provenance_configured": configured,
            "confidential_operation": None,
            "confidential_attestation": attestation,
        },
    ]

    def scripted_state() -> dict[str, Any]:
        if states:
            return states.pop(0)
        return {
            "confidential_enabled": configured,
            "confidential_provenance_configured": configured,
            "confidential_operation": None,
            "confidential_attestation": attestation,
        }

    monkeypatch.setattr(thinking_call, "_get_confidential_state", scripted_state)

    result = runner.invoke(
        thinking_call.app,
        ["confidential", "enable", "--wait-seconds", "2", "--poll-interval", "0"],
    )

    output = result.stdout + result.stderr
    assert result.exit_code == expected_exit
    assert (
        "continue in browser → http://portal.test/enable/spp?nonce=NONCE"
        in result.stdout
    )
    if configured:
        assert "next: sol call thinking confidential recheck\n" in result.stdout
        assert "operation ended without enabling confidential processing" not in output
    else:
        assert "operation ended without enabling confidential processing" in output
        assert "sol call thinking confidential status" in output
        assert "next: sol call thinking confidential recheck" not in output


def test_confidential_unreachable_uses_convey_cli_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreachableClient:
        def request(self, *_args, **_kwargs):
            raise ConveyUnreachableError("I couldn't reach the journal over HTTP.")

    monkeypatch.setattr(thinking_call, "get_client", lambda: UnreachableClient())

    result = runner.invoke(thinking_call.app, ["confidential", "status"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "I couldn't reach the journal over HTTP.\n"


def test_confidential_multi_verb_outputs_scrub_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spp.provision_confidential_handoff(_confidential_payload("secret"))
    monkeypatch.setattr(
        thinking_routes,
        "request_brain_refresh",
        lambda *, surface: surface == "thinking",
    )
    monkeypatch.setattr(
        thinking_routes,
        "build_brain_snapshot",
        lambda *_args, **_kwargs: {"state": "checking"},
    )

    results = [
        runner.invoke(thinking_call.app, ["confidential", "status"]),
        runner.invoke(thinking_call.app, ["confidential", "recheck"]),
        runner.invoke(thinking_call.app, ["confidential", "enable"]),
        runner.invoke(thinking_call.app, ["confidential", "disable"]),
    ]

    assert [result.exit_code for result in results] == [0, 0, 1, 0]
    for result in results:
        _assert_no_confidential_secret(result.stdout + result.stderr)


def test_confidential_service_busy_surfaces_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stable_confidential_handoff_url(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_flow() -> operations.HandoffResult:
        try:
            started.set()
            release.wait(5)
            return operations.HandoffResult("pending", "Still pending.", False)
        finally:
            finished.set()

    operations.start_operation(
        thinking_routes.SERVICE_SPP,
        "enable",
        "http://portal.test/enable/spp?nonce=BUSY",
        blocking_flow,
    )
    assert started.wait(1)

    try:
        result = runner.invoke(thinking_call.app, ["confidential", "enable"])
    finally:
        release.set()
        finished.wait(1)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "operation already running\n"


def test_confidential_refusals_surface_route_detail() -> None:
    inactive = runner.invoke(thinking_call.app, ["confidential", "recheck"])

    assert inactive.exit_code == 1
    assert inactive.stdout == ""
    assert inactive.stderr == "confidential processing is not active.\n"

    spp.provision_confidential_handoff(_confidential_payload("refusal"))
    already_enabled = runner.invoke(thinking_call.app, ["confidential", "enable"])

    assert already_enabled.exit_code == 1
    assert already_enabled.stdout == ""
    assert already_enabled.stderr == "confidential processing is already set up.\n"


def test_confidential_disable_matches_http_result_without_secret() -> None:
    spp.provision_confidential_handoff(_confidential_payload("disable"))

    result = runner.invoke(thinking_call.app, ["confidential", "disable"])

    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert set(payload) == {"result"}
    assert set(payload["result"]) == {"was_enabled", "credential_preserved"}
    assert payload["result"]["was_enabled"] is True
    assert payload["result"]["credential_preserved"] is False
    assert "credential-disable" not in result.stdout
    assert "acct-disable" not in result.stdout
    assert "spp-disable.example.test" not in result.stdout
    assert "confidential-model-disable" not in result.stdout


def test_confidential_cli_phase_mirror_matches_routes() -> None:
    assert (
        thinking_call._CONFIDENTIAL_PHASE_TO_PRODUCT
        == thinking_routes._CONFIDENTIAL_PHASE_TO_PRODUCT
    )
    assert (
        thinking_call._CONFIDENTIAL_RAW_TERMINAL_PHASES
        == operations.TERMINAL_PHASES
    )
    assert thinking_call._CONFIDENTIAL_TERMINAL_PHASES == {
        "not_verified",
        "needs_subscription",
        "revoked",
        "repair_needed",
        "early_access",
    }


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
            "issues": ["binary_missing"],
        },
        "openai": {"generate_ready": True, "cogitate_ready": True, "issues": []},
    }
    monkeypatch.setattr(
        thinking_routes,
        "build_provider_status",
        lambda providers, **_kwargs: provider_status,
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
        ["providers", "set-active", "--provider", "openai"],
    )
    assert success.exit_code == 0
    assert json.loads(success.stdout)["provider"] == "openai"

    bad_provider = runner.invoke(
        thinking_call.app,
        ["providers", "set-active", "--provider", "invalid"],
    )
    assert bad_provider.exit_code == 1
    assert bad_provider.stderr == (
        "Invalid provider: invalid. Must be one of: anthropic, google, openai, local\n"
    )

    bad_tier = runner.invoke(
        thinking_call.app,
        ["providers", "set-active", "--tier", "9"],
    )
    assert bad_tier.exit_code != 0
    assert "No such option: --tier" in bad_tier.stderr


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
            "path": "/app/thinking/api/providers/local/status",
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

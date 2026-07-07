# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for Thinking configuration."""

from __future__ import annotations

import json
import time
from typing import Any

import typer

from solstone.convey.reasons import (
    FILE_NOT_FOUND,
    INVALID_CONFIG_VALUE,
    INVALID_JSON_REQUEST,
    INVALID_OPERATION_FOR_STATE,
    MISSING_REQUIRED_FIELD,
)
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

# Mirrors solstone.apps.thinking.routes.AI_KEY_ENV_VARS; reconstructed here
# rather than imported so this call.py remains a pure Convey HTTP client.
_AI_KEY_ENV_VARS = [
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
]
_AI_ENV_TO_PROVIDER = {
    "GOOGLE_API_KEY": "google",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
}
_PROVIDERS = ("anthropic", "google", "openai", "local")
_CLOUD_PROVIDERS = ("anthropic", "google", "openai")
_GOOGLE_BACKENDS = ("auto", "aistudio", "vertex")

# Mirrors solstone.apps.thinking.copy; reconstructed here so this call.py
# remains a pure Convey HTTP client.
_SCOUT_STATE_OFF = "off"
_SCOUT_STATE_REQUESTED = "requested"
_SCOUT_STATE_INVITED = "invited"
_SCOUT_STATE_ON = "on"
_SCOUT_STATE_ENDED = "ended"
_SCOUT_STATE_MANUAL_KEY_PRESENT = "manual_key_present"
_SCOUT_STATE_REPAIR_NEEDED = "repair_needed"
_SCOUT_PRODUCT_STATES = {
    _SCOUT_STATE_OFF,
    _SCOUT_STATE_REQUESTED,
    _SCOUT_STATE_INVITED,
    _SCOUT_STATE_ON,
    _SCOUT_STATE_ENDED,
    _SCOUT_STATE_MANUAL_KEY_PRESENT,
    _SCOUT_STATE_REPAIR_NEEDED,
}
_SCOUT_TERMINAL_PHASES = {
    _SCOUT_STATE_INVITED,
    _SCOUT_STATE_REQUESTED,
    _SCOUT_STATE_ENDED,
    _SCOUT_STATE_REPAIR_NEEDED,
}
_SCOUT_GUIDANCE = {
    _SCOUT_STATE_OFF: "Scout is off.",
    _SCOUT_STATE_REQUESTED: "Scout is waiting for approval.",
    _SCOUT_STATE_INVITED: "Scout is ready; use the Scout lane in Thinking.",
    _SCOUT_STATE_ON: "Scout is on.",
    _SCOUT_STATE_ENDED: "Scout has ended; enable Scout to use it again.",
    _SCOUT_STATE_MANUAL_KEY_PRESENT: "Clear the BYO Gemini key before enabling Scout.",
    _SCOUT_STATE_REPAIR_NEEDED: "Scout needs repair; try again from Thinking.",
}
_SCOUT_CONSENT_CTA = "continue to approve →"

app = typer.Typer(help="Thinking providers, keys, and local model setup.")

keys_app = typer.Typer(help="AI key management.")
app.add_typer(keys_app, name="keys")
providers_app = typer.Typer(help="AI provider configuration.")
app.add_typer(providers_app, name="providers")
google_backend_app = typer.Typer(help="Google backend selection.")
app.add_typer(google_backend_app, name="google-backend")
vertex_app = typer.Typer(help="Vertex credentials.")
app.add_typer(vertex_app, name="vertex-credentials")
local_app = typer.Typer(help="Local model readiness and setup.")
app.add_typer(local_app, name="local")
scout_app = typer.Typer(help="Scout hosted Gemini lane.")
app.add_typer(scout_app, name="scout")


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, object | None] | None = None,
    json_body: dict[str, object | None] | None = None,
) -> Any:
    clean_params = (
        {key: value for key, value in params.items() if value is not None}
        if params
        else None
    )
    clean_body = (
        {key: value for key, value in json_body.items() if value is not None}
        if json_body
        else None
    )
    return get_client().request(
        method,
        path,
        params=clean_params,
        json=clean_body,
    )


def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2))


def _exit_with(message: str, code: int = 1) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _validate_env_var_or_exit(env_var: str) -> None:
    if env_var not in _AI_KEY_ENV_VARS:
        _exit_with(
            f"Invalid env var: {env_var}. Must be one of: {', '.join(_AI_KEY_ENV_VARS)}"
        )


def _validate_provider_or_exit(provider: str, *, cloud_only: bool = False) -> None:
    valid = _CLOUD_PROVIDERS if cloud_only else _PROVIDERS
    if provider not in valid:
        _exit_with(f"Invalid provider: {provider}. Must be one of: {', '.join(valid)}")


def _validate_tier_or_exit(tier: int | None) -> None:
    if tier is not None and tier not in {1, 2, 3}:
        _exit_with(f"Invalid tier: {tier}. Must be one of: 1, 2, 3")


def _get_providers() -> dict[str, Any]:
    return _request("GET", "/app/thinking/api/providers")


def _get_keys() -> dict[str, Any]:
    return _request("GET", "/app/thinking/api/keys")


def _get_scout_status() -> dict[str, Any]:
    return _request("GET", "/app/thinking/api/scout")


def _post_scout_action(path: str) -> dict[str, Any]:
    try:
        return _request("POST", path)
    except ConveyClientError as err:
        if err.reason_code == INVALID_OPERATION_FOR_STATE.code and err.detail:
            _exit_with(err.detail)
        raise


def _scout_guidance(key: Any) -> str | None:
    return _SCOUT_GUIDANCE.get(str(key or ""))


def _echo_scout_guidance(key: Any) -> None:
    guidance = _scout_guidance(key)
    if guidance:
        typer.echo(guidance)


def _maybe_echo_scout_portal(operation: Any) -> None:
    if not isinstance(operation, dict):
        return
    portal_url = operation.get("portal_url")
    if portal_url:
        typer.echo(f"{_SCOUT_CONSENT_CTA} {portal_url}")


def _poll_scout_until_terminal(
    *,
    wait_seconds: float,
    poll_interval: float,
) -> tuple[dict[str, Any], str | None, str | None]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    interval = max(0.0, poll_interval)

    while True:
        status = _get_scout_status()
        operation = status.get("operation")
        if not isinstance(operation, dict):
            return status, None, None

        phase = str(operation.get("phase") or "")
        if phase in _SCOUT_TERMINAL_PHASES:
            guidance = operation.get("guidance")
            return status, phase, str(guidance) if guidance else None

        if time.monotonic() >= deadline:
            return (
                status,
                _SCOUT_STATE_REPAIR_NEEDED,
                "Timed out waiting for Scout.",
            )

        if interval:
            time.sleep(interval)


def _echo_scout_terminal(
    status: dict[str, Any],
    phase: str | None,
    operation_guidance: str | None,
) -> None:
    state = status.get("state")
    typer.echo(f"state: {state}")
    if phase:
        typer.echo(f"operation: {phase}")
    if operation_guidance:
        typer.echo(operation_guidance)
    _echo_scout_guidance(phase or state)


@scout_app.command("status")
@convey_cli
def scout_status() -> None:
    """Show Scout hosted Gemini lane status."""

    response = _get_scout_status()
    _echo_json(response)
    _echo_scout_guidance(response.get("state"))


@scout_app.command("check")
@convey_cli
def scout_check() -> None:
    """Check Scout hosted Gemini status."""

    response = _post_scout_action("/app/thinking/api/scout/check")
    _echo_json(response)
    _echo_scout_guidance(response.get("state"))


@scout_app.command("enable")
@convey_cli
def scout_enable(
    wait_seconds: float = typer.Option(
        900.0, "--wait-seconds", help="Maximum seconds to wait for the operation."
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between status polls."
    ),
) -> None:
    """Enable Scout hosted Gemini."""

    response = _post_scout_action("/app/thinking/api/scout/enable")
    _maybe_echo_scout_portal(
        response.get("operation") if isinstance(response, dict) else None
    )
    status, phase, operation_guidance = _poll_scout_until_terminal(
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )
    _echo_scout_terminal(status, phase, operation_guidance)
    if phase == _SCOUT_STATE_REPAIR_NEEDED:
        raise typer.Exit(1)


@scout_app.command("refresh")
@convey_cli
def scout_refresh(
    wait_seconds: float = typer.Option(
        900.0, "--wait-seconds", help="Maximum seconds to wait for the operation."
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between status polls."
    ),
) -> None:
    """Refresh Scout hosted Gemini status."""

    response = _post_scout_action("/app/thinking/api/scout/refresh")
    _maybe_echo_scout_portal(
        response.get("operation") if isinstance(response, dict) else None
    )
    status, phase, operation_guidance = _poll_scout_until_terminal(
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )
    _echo_scout_terminal(status, phase, operation_guidance)
    if phase == _SCOUT_STATE_REPAIR_NEEDED:
        raise typer.Exit(1)


@scout_app.command("disable")
@convey_cli
def scout_disable() -> None:
    """Disable Scout hosted Gemini."""

    response = _post_scout_action("/app/thinking/api/scout/disable")
    _echo_json(
        {
            "result": response.get("result", {}),
            "status": response.get("status", {}),
        }
    )


@keys_app.command("show")
@convey_cli
def keys_show() -> None:
    """Show configured AI key status."""

    response = _get_keys()
    _echo_json(
        {
            "api_keys": response.get("api_keys", {}),
            "env": response.get("env", {}),
            "key_validation": response.get("key_validation", {}),
        }
    )


@keys_app.command("set")
@convey_cli
def keys_set(
    env_var: str = typer.Argument(..., help="Environment variable to set."),
    value: str = typer.Argument(..., help="API key value."),
) -> None:
    """Set an AI key in journal config."""

    _validate_env_var_or_exit(env_var)
    try:
        response = _request(
            "PUT",
            "/app/thinking/api/keys",
            json_body={"env_var": env_var, "value": value},
        )
    except ConveyClientError as err:
        if err.reason_code == INVALID_CONFIG_VALUE.code and err.detail:
            _exit_with(err.detail)
        raise
    provider = _AI_ENV_TO_PROVIDER[env_var]
    _echo_json(
        {
            "env_var": env_var,
            "set": True,
            "validation": response.get("key_validation", {}).get(provider),
        }
    )


@keys_app.command("clear")
@convey_cli
def keys_clear(
    env_var: str = typer.Argument(..., help="Environment variable to clear."),
) -> None:
    """Clear an AI key from journal config."""

    _validate_env_var_or_exit(env_var)
    _request(
        "PUT",
        "/app/thinking/api/keys",
        json_body={"env_var": env_var, "value": ""},
    )
    _echo_json({"env_var": env_var, "cleared": True})


@keys_app.command("validate")
@convey_cli
def keys_validate(
    cache_result: bool = typer.Option(
        False, "--cache-result", help="Persist results to providers.key_validation."
    ),
) -> None:
    """Validate configured AI keys and Vertex credentials."""

    method = "POST" if cache_result else "GET"
    response = _request(method, "/app/thinking/api/validate-keys")
    _echo_json({"key_validation": response.get("key_validation", {})})


@providers_app.command("show")
@convey_cli
def providers_show(
    human: bool = typer.Option(False, "--human", help="Print one-line statuses."),
) -> None:
    """Show provider configuration."""

    response = _get_providers()
    if human:
        active = response.get("active_lane", {})
        typer.echo(f"active lane: {active.get('lane', 'advanced')}")
        for name, status in sorted(response.get("provider_status", {}).items()):
            issues = status.get("issues", [])
            if issues:
                status_text = issues[0]
            elif status.get("cogitate_ready") or status.get("generate_ready"):
                status_text = "ready"
            else:
                status_text = "not ready"
            typer.echo(f"{name}: {status_text}")
        return
    _echo_json(
        {
            "providers": response.get("providers", []),
            "provider_status": response.get("provider_status", {}),
            "active_lane": response.get("active_lane", {}),
            "generate": response.get("generate", {}),
            "cogitate": response.get("cogitate", {}),
            "local_override": response.get("local_override", {}),
            "api_keys": response.get("api_keys", {}),
            "key_validation": response.get("key_validation", {}),
        }
    )


def _set_provider_type(
    agent_type: str,
    provider: str | None,
    tier: int | None,
    backup: str | None,
) -> dict[str, Any]:
    if provider is not None:
        _validate_provider_or_exit(provider)
    if backup is not None:
        _validate_provider_or_exit(backup)
    _validate_tier_or_exit(tier)
    payload = {
        key: value
        for key, value in {
            "provider": provider,
            "tier": tier,
            "backup": backup,
        }.items()
        if value is not None
    }
    try:
        response = _request(
            "POST",
            "/app/thinking/api/providers",
            json_body={agent_type: payload},
        )
    except ConveyClientError as err:
        if err.reason_code == INVALID_CONFIG_VALUE.code and err.detail:
            _exit_with(err.detail)
        raise
    return response.get(agent_type, {})


@providers_app.command("set-generate")
@convey_cli
def providers_set_generate(
    provider: str | None = typer.Option(None, "--provider", help="Primary provider."),
    tier: int | None = typer.Option(None, "--tier", help="Tier (1, 2, or 3)."),
    backup: str | None = typer.Option(None, "--backup", help="Backup provider."),
) -> None:
    """Set generate provider defaults."""

    _echo_json(_set_provider_type("generate", provider, tier, backup))


@providers_app.command("set-cogitate")
@convey_cli
def providers_set_cogitate(
    provider: str | None = typer.Option(None, "--provider", help="Primary provider."),
    tier: int | None = typer.Option(None, "--tier", help="Tier (1, 2, or 3)."),
    backup: str | None = typer.Option(None, "--backup", help="Backup provider."),
) -> None:
    """Set cogitate provider defaults."""

    _echo_json(_set_provider_type("cogitate", provider, tier, backup))


@app.command("set-local-endpoint")
@convey_cli
def set_local_endpoint(
    url: str = typer.Option(..., "--url", help="OpenAI-compatible endpoint URL."),
    model: str = typer.Option(..., "--model", help="Served model id."),
    credential: str | None = typer.Option(
        None,
        "--credential",
        help="Optional bearer credential for the endpoint.",
    ),
) -> None:
    """Set the BYO local provider endpoint."""

    payload: dict[str, object | None] = {
        "endpoint_url": url,
        "served_model_id": model,
    }
    if credential is not None:
        payload["credential"] = credential
    response = _request(
        "POST",
        "/app/thinking/api/local/endpoint",
        json_body=payload,
    )
    _echo_json(response.get("local_endpoint", response))


@app.command("clear-local-endpoint")
@convey_cli
def clear_local_endpoint() -> None:
    """Clear the BYO local provider endpoint."""

    response = _request("DELETE", "/app/thinking/api/local/endpoint")
    _echo_json(response.get("local_endpoint", response))


@google_backend_app.command("show")
@convey_cli
def google_backend_show() -> None:
    """Show Google backend status."""

    providers = _get_providers()
    _echo_json(
        {
            "google_backend": providers.get("google_backend", "auto"),
            "vertex_credentials_configured": providers.get(
                "vertex_credentials_configured",
                False,
            ),
            "vertex_credentials_email": providers.get("vertex_credentials_email", ""),
        }
    )


@google_backend_app.command("set")
@convey_cli
def google_backend_set(
    backend: str = typer.Argument(..., help="Google backend to use."),
) -> None:
    """Set the Google provider backend."""

    if backend not in _GOOGLE_BACKENDS:
        _exit_with(
            f"Invalid google_backend: {backend}. Must be one of: {', '.join(_GOOGLE_BACKENDS)}"
        )
    _request(
        "POST",
        "/app/thinking/api/providers",
        json_body={"google_backend": backend},
    )
    _echo_json({"google_backend": backend})


@vertex_app.command("show")
@convey_cli
def vertex_credentials_show() -> None:
    """Show Vertex credential status without secrets."""

    providers = _get_providers()
    validation = providers.get("key_validation", {}).get("google_vertex", {})
    _echo_json(
        {
            "configured": providers.get("vertex_credentials_configured", False),
            "email": providers.get("vertex_credentials_email", ""),
            "validation": validation,
        }
    )


@vertex_app.command("import")
@convey_cli
def vertex_credentials_import(
    file_path: str = typer.Argument(..., help="Path to credentials JSON."),
    skip_validation: bool = typer.Option(
        False, "--skip-validation", help="Skip API validation of credentials."
    ),
) -> None:
    """Import Vertex credentials into the journal config."""

    try:
        response = _request(
            "POST",
            "/app/thinking/api/vertex-credentials/import",
            json_body={"path": file_path, "skip_validation": skip_validation},
        )
    except ConveyClientError as err:
        if err.reason_code == FILE_NOT_FOUND.code:
            _exit_with(f"Credential file not found: {err.detail}")
        if err.reason_code == INVALID_JSON_REQUEST.code:
            _exit_with(f"Invalid JSON in credential file: {err.detail}")
        if err.reason_code == MISSING_REQUIRED_FIELD.code:
            _exit_with(f"Missing required fields: {err.detail}")
        typer.echo(err.error, err=True)
        raise typer.Exit(1)
    _echo_json(response)


@vertex_app.command("clear")
@convey_cli
def vertex_credentials_clear() -> None:
    """Clear stored Vertex credentials."""

    _request(
        "POST",
        "/app/thinking/api/providers",
        json_body={"vertex_credentials": ""},
    )
    _echo_json({"configured": False})


@local_app.command("readiness")
@convey_cli
def local_readiness() -> None:
    """Show local provider readiness."""

    response = _get_providers()
    _echo_json(response.get("ai_readiness", {}).get("local", {}))


@local_app.command("status")
@convey_cli
def local_status() -> None:
    """Show local provider status."""

    _echo_json(_request("GET", "/app/thinking/api/providers/local/status"))


@local_app.command("availability")
@convey_cli
def local_availability(
    model: str | None = typer.Option(None, "--model", help="Local model id."),
) -> None:
    """Show local model availability."""

    _echo_json(
        _request(
            "GET",
            "/app/thinking/api/local/availability",
            params={"model": model},
        )
    )


@local_app.command("bootstrap")
@convey_cli
def local_bootstrap(
    model: str | None = typer.Option(None, "--model", help="Local model id."),
) -> None:
    """Start local model setup."""

    _echo_json(
        _request(
            "POST",
            "/app/thinking/api/local/bootstrap",
            params={"model": model},
        )
    )


@local_app.command("bootstrap-status")
@convey_cli
def local_bootstrap_status(
    model: str | None = typer.Option(None, "--model", help="Local model id."),
) -> None:
    """Show local setup status."""

    _echo_json(
        _request(
            "GET",
            "/app/thinking/api/local/bootstrap/status",
            params={"model": model},
        )
    )


@local_app.command("models")
@convey_cli
def local_models() -> None:
    """List local models."""

    _echo_json(_request("GET", "/app/thinking/api/local/models"))

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for Thinking configuration."""

from __future__ import annotations

import json
from typing import Any

import typer

from solstone.convey.reasons import (
    FILE_NOT_FOUND,
    INVALID_CONFIG_VALUE,
    INVALID_JSON_REQUEST,
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

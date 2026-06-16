# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for journal settings management.

Auto-discovered by ``think.call`` and mounted as ``sol call settings ...``.
"""

import json
from typing import Any

import typer

from solstone.convey.reasons import (
    INVALID_CONFIG_VALUE,
)
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

# Mirrors solstone.apps.settings.routes.API_KEY_ENV_VARS (the canonical order
# used in the "Invalid env var" message); reconstructed here rather than
# imported (HTTP-only gate) or read from the response (Flask sorts JSON keys).
_API_KEY_ENV_VARS = [
    "REVAI_ACCESS_TOKEN",
    "PLAUD_ACCESS_TOKEN",
]
_AI_KEY_ENV_VARS = {
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
}
_SERVICE_KEY_VALIDATION_NAME = {
    "REVAI_ACCESS_TOKEN": "revai",
    "PLAUD_ACCESS_TOKEN": "plaud",
}

app = typer.Typer(
    help="Journal settings — keys, providers, transcription, identity, and observer."
)

keys_app = typer.Typer(help="API key management.")
app.add_typer(keys_app, name="keys")
providers_app = typer.Typer(help="AI provider configuration.")
app.add_typer(providers_app, name="providers")
google_backend_app = typer.Typer(help="Google backend selection.")
app.add_typer(google_backend_app, name="google-backend")
vertex_app = typer.Typer(help="Vertex AI service account credentials.")
app.add_typer(vertex_app, name="vertex-credentials")
transcribe_app = typer.Typer(help="Transcription backend configuration.")
app.add_typer(transcribe_app, name="transcribe")
identity_app = typer.Typer(help="Journal owner identity.")
app.add_typer(identity_app, name="identity")
observer_app = typer.Typer(help="Observer capture settings.")
app.add_typer(observer_app, name="observer")
convey_app = typer.Typer(help="Convey access configuration.")
app.add_typer(convey_app, name="convey")


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, object] | None = None,
    json_body: dict[str, object] | None = None,
) -> Any:
    return get_client().request(method, path, params=params, json=json_body)


def _get_config() -> dict[str, Any]:
    return _request("GET", "/app/settings/api/config")


def _post_config(
    section: str,
    data: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    value: Any = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"section": section}
    if data is not None:
        body["data"] = data
    if key is not None:
        body["key"] = key
        body["value"] = value
    return _request("POST", "/app/settings/api/config", json_body=body)


def _echo_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2))


def _exit_with(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _validate_env_var_or_exit(env_var: str) -> None:
    if env_var in _AI_KEY_ENV_VARS:
        typer.echo(
            "Moved to `sol call thinking keys …` — run that instead.",
            err=True,
        )
        raise typer.Exit(2)
    if env_var not in _API_KEY_ENV_VARS:
        _exit_with(
            f"Invalid env var: {env_var}. Must be one of: {', '.join(_API_KEY_ENV_VARS)}"
        )


def _moved_stub(command: str) -> None:
    typer.echo(f"Moved to `sol call thinking {command}` — run that instead.", err=True)
    raise typer.Exit(2)


@convey_app.command("host-url")
@convey_cli
def convey_host_url(
    url: str | None = typer.Argument(
        None, help="Absolute URL to advertise to devices."
    ),
    auto: bool = typer.Option(
        False, "--auto", help="Clear the manual host URL override."
    ),
    show: bool = typer.Option(False, "--show", help="Show the effective host URL."),
) -> None:
    """Manage the host URL advertised to remote devices."""

    if sum(bool(flag) for flag in (url is not None, auto, show)) != 1:
        _exit_with("error: choose exactly one of <url>, --auto, or --show")
    if show:
        result = _request("GET", "/app/settings/api/convey/host-url")
        typer.echo(result["host_url"])
        return
    if auto:
        _request("POST", "/app/settings/api/convey/host-url", json_body={"auto": True})
        typer.echo("host url cleared. auto-detect is active.")
        return
    assert url is not None
    try:
        result = _request(
            "POST",
            "/app/settings/api/convey/host-url",
            json_body={"url": url},
        )
    except ConveyClientError as err:
        if err.reason_code == INVALID_CONFIG_VALUE.code and err.detail:
            _exit_with(err.detail)
        raise
    typer.echo(f"host url set: {result['host_url']}")


@convey_app.command("status")
@convey_cli
def convey_status() -> None:
    """Show Convey network and host-URL status."""

    result = _request("GET", "/app/settings/api/convey/status")
    typer.echo(result["status_text"])


@app.command("show")
@convey_cli
def show() -> None:
    """Show a summary of journal settings."""

    config = _get_config()
    env_config = config.get("env", {})
    keys = {key: bool(env_config.get(key)) for key in _API_KEY_ENV_VARS}
    summary = {
        "identity": config.get("identity", {}),
        "transcribe": config.get("transcribe", {}),
        "observe": config.get("observe", {}),
        "keys": keys,
    }
    _echo_json(summary)


@keys_app.command("show")
@convey_cli
def keys_show() -> None:
    """Show configured API key status."""

    config = _get_config()
    env_config = config.get("env", {})
    status = {key: bool(env_config.get(key)) for key in _API_KEY_ENV_VARS}
    _echo_json(status)


@keys_app.command("set")
@convey_cli
def keys_set(
    env_var: str = typer.Argument(..., help="Environment variable to set."),
    value: str = typer.Argument(..., help="API key value."),
) -> None:
    """Set an API key in journal config."""

    _validate_env_var_or_exit(env_var)
    response = _post_config("env", key=env_var, value=value)
    validation = response.get("key_validation", {}).get(
        _SERVICE_KEY_VALIDATION_NAME.get(env_var, ""),
    )
    _echo_json({"env_var": env_var, "set": True, "validation": validation})


@keys_app.command("clear")
@convey_cli
def keys_clear(
    env_var: str = typer.Argument(..., help="Environment variable to clear."),
) -> None:
    """Clear an API key from journal config."""

    _validate_env_var_or_exit(env_var)
    _post_config("env", key=env_var, value="")
    _echo_json({"env_var": env_var, "cleared": True})


@keys_app.command("validate")
@convey_cli
def keys_validate(
    cache_result: bool = typer.Option(
        False, "--cache-result", help="Persist results to providers.key_validation."
    ),
) -> None:
    """Validate all configured API keys without persisting by default."""

    method = "POST" if cache_result else "GET"
    response = _request(method, "/app/settings/api/validate-keys")
    _echo_json({"key_validation": response.get("key_validation", {})})


@providers_app.command("show")
def providers_show(
    human: bool = typer.Option(False, "--human", help="Print one-line statuses."),
) -> None:
    """Moved to ``sol call thinking providers show``."""

    _moved_stub("providers show")


@providers_app.command("install")
def providers_install(
    name: str = typer.Argument(None, help="Provider name."),
) -> None:
    """Moved to `journal install-provider`."""
    typer.echo("Moved to `journal install-provider` — run that instead.", err=True)
    raise typer.Exit(2)


@providers_app.command("set-local-endpoint")
def providers_set_local_endpoint(
    url: str = typer.Option(..., "--url", help="OpenAI-compatible endpoint URL."),
    model: str = typer.Option(..., "--model", help="Served model id."),
    credential: str | None = typer.Option(
        None,
        "--credential",
        help="Optional bearer credential for the endpoint.",
    ),
) -> None:
    """Moved to ``sol call thinking set-local-endpoint``."""

    _moved_stub("set-local-endpoint")


@providers_app.command("clear-local-endpoint")
def providers_clear_local_endpoint() -> None:
    """Moved to ``sol call thinking clear-local-endpoint``."""

    _moved_stub("clear-local-endpoint")


@providers_app.command("set-generate")
def providers_set_generate(
    provider: str | None = typer.Option(None, "--provider", help="Primary provider."),
    tier: int | None = typer.Option(None, "--tier", help="Tier (1, 2, or 3)."),
    backup: str | None = typer.Option(None, "--backup", help="Backup provider."),
) -> None:
    """Moved to ``sol call thinking providers set-generate``."""

    _moved_stub("providers set-generate")


@providers_app.command("set-cogitate")
def providers_set_cogitate(
    provider: str | None = typer.Option(None, "--provider", help="Primary provider."),
    tier: int | None = typer.Option(None, "--tier", help="Tier (1, 2, or 3)."),
    backup: str | None = typer.Option(None, "--backup", help="Backup provider."),
) -> None:
    """Moved to ``sol call thinking providers set-cogitate``."""

    _moved_stub("providers set-cogitate")


@google_backend_app.command("show")
def google_backend_show() -> None:
    """Moved to ``sol call thinking google-backend show``."""

    _moved_stub("google-backend show")


@google_backend_app.command("set")
def google_backend_set(
    backend: str = typer.Argument(..., help="Google backend to use."),
) -> None:
    """Moved to ``sol call thinking google-backend set``."""

    _moved_stub("google-backend set")


@vertex_app.command("show")
def vertex_credentials_show() -> None:
    """Moved to ``sol call thinking vertex-credentials show``."""

    _moved_stub("vertex-credentials show")


@vertex_app.command("import")
def vertex_credentials_import(
    file_path: str = typer.Argument(..., help="Path to service account JSON."),
    skip_validation: bool = typer.Option(
        False, "--skip-validation", help="Skip API validation of credentials."
    ),
) -> None:
    """Moved to ``sol call thinking vertex-credentials import``."""

    _moved_stub("vertex-credentials import")


@vertex_app.command("clear")
def vertex_credentials_clear() -> None:
    """Moved to ``sol call thinking vertex-credentials clear``."""

    _moved_stub("vertex-credentials clear")


@transcribe_app.command("show")
@convey_cli
def transcribe_show() -> None:
    """Show transcription backend configuration."""

    response = _request("GET", "/app/settings/api/transcribe")
    _echo_json(
        {
            "backends": response.get("backends", []),
            "api_keys": response.get("api_keys", {}),
            "config": response.get("config", {}),
        }
    )


@transcribe_app.command("set-backend")
@convey_cli
def transcribe_set_backend(
    backend: str = typer.Argument(..., help="Transcription backend."),
) -> None:
    """Set the transcription backend."""

    response = _request("GET", "/app/settings/api/transcribe")
    valid = sorted(item["name"] for item in response.get("backends", []))
    if backend not in valid:
        _exit_with(f"Invalid backend: {backend}. Must be one of: {', '.join(valid)}")
    update = _post_config("transcribe", {"backend": backend})
    _echo_json(update.get("config", {}).get("transcribe", {}))


@transcribe_app.command("set")
@convey_cli
def transcribe_set(
    enrich: bool | None = typer.Option(None, "--enrich/--no-enrich"),
    noise_upgrade: bool | None = typer.Option(
        None, "--noise-upgrade/--no-noise-upgrade"
    ),
) -> None:
    """Set transcription options."""

    data: dict[str, Any] = {}
    if enrich is not None:
        data["enrich"] = enrich
    if noise_upgrade is not None:
        data["noise_upgrade"] = noise_upgrade
    response = _post_config("transcribe", data)
    _echo_json(response.get("config", {}).get("transcribe", {}))


@identity_app.command("show")
@convey_cli
def identity_show() -> None:
    """Show journal identity config."""

    config = _get_config()
    _echo_json(config.get("identity", {}))


@identity_app.command("set")
@convey_cli
def identity_set(
    name: str | None = typer.Option(None, "--name"),
    preferred: str | None = typer.Option(None, "--preferred"),
    bio: str | None = typer.Option(None, "--bio"),
    timezone_name: str | None = typer.Option(None, "--timezone"),
    pronouns: str | None = typer.Option(None, "--pronouns"),
    add_email: str | None = typer.Option(None, "--add-email"),
    remove_email: str | None = typer.Option(None, "--remove-email"),
    add_alias: str | None = typer.Option(None, "--add-alias"),
    remove_alias: str | None = typer.Option(None, "--remove-alias"),
) -> None:
    """Update journal owner identity."""

    config = _get_config()
    identity = dict(config.get("identity", {}))
    data: dict[str, Any] = {}

    if name is not None:
        data["name"] = name
    if preferred is not None:
        data["preferred"] = preferred
    if bio is not None:
        data["bio"] = bio
    if timezone_name is not None:
        data["timezone"] = timezone_name

    if pronouns is not None:
        try:
            data["pronouns"] = json.loads(pronouns)
        except json.JSONDecodeError:
            typer.echo("Invalid JSON in pronouns", err=True)
            raise typer.Exit(1)

    if add_email is not None or remove_email is not None:
        emails = list(identity.get("email_addresses", []))
        if add_email is not None and add_email not in emails:
            emails.append(add_email)
        if remove_email is not None:
            emails = [email for email in emails if email != remove_email]
        data["email_addresses"] = emails

    if add_alias is not None or remove_alias is not None:
        aliases = list(identity.get("aliases", []))
        if add_alias is not None and add_alias not in aliases:
            aliases.append(add_alias)
        if remove_alias is not None:
            aliases = [alias for alias in aliases if alias != remove_alias]
        data["aliases"] = aliases

    response = _post_config("identity", data)
    _echo_json(response.get("config", {}).get("identity", {}))


@observer_app.command("show")
@convey_cli
def observer_show() -> None:
    """Show observer configuration with defaults."""

    response = _request("GET", "/app/settings/api/observe")
    _echo_json(response)


@observer_app.command("set")
@convey_cli
def observer_set(
    enabled: bool | None = typer.Option(None, "--enabled/--no-enabled"),
    capture_interval: int | None = typer.Option(None, "--capture-interval"),
) -> None:
    """Update observer capture settings."""

    current = _request("GET", "/app/settings/api/observe")
    defaults = current.get("defaults", {}).get("tmux", {})
    tmux: dict[str, Any] = {}

    if capture_interval is not None:
        min_val = defaults["capture_interval_min"]
        max_val = defaults["capture_interval_max"]
        if capture_interval < min_val or capture_interval > max_val:
            _exit_with(
                "tmux.capture_interval must be an integer between "
                f"{min_val} and {max_val}"
            )
        tmux["capture_interval"] = capture_interval

    if enabled is not None:
        tmux["enabled"] = enabled

    response = _request(
        "POST",
        "/app/settings/api/observe",
        json_body={"tmux": tmux},
    )
    _echo_json(response.get("tmux", {}))

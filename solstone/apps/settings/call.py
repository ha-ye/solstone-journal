# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for journal settings management.

Auto-discovered by ``think.call`` and mounted as ``sol call settings ...``.
"""

import json
from typing import Any

import typer

from solstone.convey.reasons import (
    FILE_NOT_FOUND,
    INVALID_CONFIG_VALUE,
    INVALID_JSON_REQUEST,
    MISSING_REQUIRED_FIELD,
    NETWORK_SECURITY_REQUIRES_PASSWORD,
)
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

# Mirrors solstone.apps.settings.routes.API_KEY_ENV_VARS (the canonical order
# used in the "Invalid env var" message); reconstructed here rather than
# imported (HTTP-only gate) or read from the response (Flask sorts JSON keys).
_API_KEY_ENV_VARS = [
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "REVAI_ACCESS_TOKEN",
    "PLAUD_ACCESS_TOKEN",
]
CONVEY_MOVED_NETWORK_ENABLE = (
    "moved to `journal settings convey network-access enable` — run that instead."
)
CONVEY_MOVED_NETWORK_DISABLE = (
    "moved to `journal settings convey network-access disable` — run that instead."
)

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
network_access_app = typer.Typer(help="Convey network exposure.")
convey_app.add_typer(network_access_app, name="network-access")
trust_localhost_app = typer.Typer(help="Localhost password-bypass behavior.")
convey_app.add_typer(trust_localhost_app, name="trust-localhost")


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


def _get_providers() -> dict[str, Any]:
    return _request("GET", "/app/settings/api/providers")


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
    if env_var not in _API_KEY_ENV_VARS:
        _exit_with(
            f"Invalid env var: {env_var}. Must be one of: {', '.join(_API_KEY_ENV_VARS)}"
        )


def _provider_for_env_var(providers: dict[str, Any], env_var: str) -> str | None:
    for provider in providers.get("providers", []):
        if provider.get("env_key") == env_var:
            return provider.get("name")
    return None


@network_access_app.command("enable")
def convey_network_access_enable() -> None:
    """Moved to ``journal settings convey network-access enable``."""

    typer.echo(CONVEY_MOVED_NETWORK_ENABLE, err=True)
    raise typer.Exit(2)


@network_access_app.command("disable")
def convey_network_access_disable() -> None:
    """Moved to ``journal settings convey network-access disable``."""

    typer.echo(CONVEY_MOVED_NETWORK_DISABLE, err=True)
    raise typer.Exit(2)


@trust_localhost_app.command("enable")
@convey_cli
def convey_trust_localhost_enable() -> None:
    """Enable localhost password bypass."""

    _post_config("convey", {"trust_localhost": True})
    typer.echo("localhost trust enabled. localhost requests skip the password.")


@trust_localhost_app.command("disable")
@convey_cli
def convey_trust_localhost_disable() -> None:
    """Disable localhost password bypass."""

    try:
        _post_config("convey", {"trust_localhost": False})
    except ConveyClientError as err:
        if err.reason_code == NETWORK_SECURITY_REQUIRES_PASSWORD.code:
            _exit_with(err.detail or err.error)
        raise
    typer.echo("localhost trust disabled. localhost requests now require the password.")


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
    providers = _get_providers()
    env_config = config.get("env", {})
    keys = {key: bool(env_config.get(key)) for key in _API_KEY_ENV_VARS}
    summary = {
        "identity": config.get("identity", {}),
        "providers": {
            "generate": providers.get("generate", {}),
            "cogitate": providers.get("cogitate", {}),
            "google_backend": providers.get("google_backend", "auto"),
            "key_validation": providers.get("key_validation", {}),
        },
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
    providers = _get_providers()
    provider = _provider_for_env_var(providers, env_var)
    response = _post_config("env", key=env_var, value=value)
    validation = None
    if provider:
        validation = response.get("key_validation", {}).get(provider)
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
@convey_cli
def providers_show(
    human: bool = typer.Option(False, "--human", help="Print one-line statuses."),
) -> None:
    """Show provider configuration."""

    response = _get_providers()
    provider_status = response.get("provider_status", {})
    if human:
        for name in sorted(provider_status):
            status = provider_status[name]
            issues = status.get("issues", [])
            if issues:
                status_text = issues[0]
            elif status.get("cogitate_ready") or (
                not status.get("cogitate_cli") and status.get("generate_ready")
            ):
                status_text = "ready"
            else:
                status_text = "not ready"
            typer.echo(f"{name}: {status_text}")
        return
    _echo_json(
        {
            "providers": response.get("providers", []),
            "provider_status": provider_status,
            "generate": response.get("generate", {}),
            "cogitate": response.get("cogitate", {}),
            "api_keys": response.get("api_keys", {}),
            "key_validation": response.get("key_validation", {}),
        }
    )


@providers_app.command("install")
def providers_install(
    name: str = typer.Argument(None, help="Provider name."),
) -> None:
    """Moved to `journal install-provider`."""
    typer.echo("Moved to `journal install-provider` — run that instead.", err=True)
    raise typer.Exit(2)


def _set_provider_type(
    agent_type: str,
    provider: str | None,
    tier: int | None,
    backup: str | None,
) -> dict[str, Any]:
    if tier is not None and tier not in {1, 2, 3}:
        _exit_with(f"Invalid tier: {tier}. Must be 1, 2, or 3.")
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
            "/app/settings/api/providers",
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


@google_backend_app.command("show")
@convey_cli
def google_backend_show() -> None:
    """Show Google backend status."""

    providers = _get_providers()
    _echo_json(
        {
            "google_backend": providers.get("google_backend", "auto"),
            "vertex_credentials_configured": providers.get(
                "vertex_credentials_configured", False
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
    if backend not in ("auto", "aistudio", "vertex"):
        _exit_with(
            f"Invalid google_backend: {backend}. Must be 'auto', 'aistudio', or 'vertex'."
        )

    _request(
        "POST",
        "/app/settings/api/providers",
        json_body={"google_backend": backend},
    )
    _echo_json({"google_backend": backend})


@vertex_app.command("show")
@convey_cli
def vertex_credentials_show() -> None:
    """Show Vertex credential status without secrets."""

    config = _get_config()
    providers = _get_providers()
    providers_config = config.get("providers", {})
    validation = providers.get("key_validation", {}).get("google_vertex", {})
    _echo_json(
        {
            "configured": providers.get("vertex_credentials_configured", False),
            "email": providers.get("vertex_credentials_email", ""),
            "path": providers_config.get("vertex_credentials") or "",
            "validation": validation,
        }
    )


@vertex_app.command("import")
@convey_cli
def vertex_credentials_import(
    file_path: str = typer.Argument(..., help="Path to service account JSON."),
    skip_validation: bool = typer.Option(
        False, "--skip-validation", help="Skip API validation of credentials."
    ),
) -> None:
    """Import Vertex service account credentials into the journal config."""

    try:
        response = _request(
            "POST",
            "/app/settings/api/vertex-credentials/import",
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
        "/app/settings/api/providers",
        json_body={"vertex_credentials": ""},
    )
    _echo_json({"configured": False})


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

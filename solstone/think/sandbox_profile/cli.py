# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI for the disposable sandbox profile lifecycle.

``describe`` and ``status`` are read-only verbs and must not reach any helper
that can materialize journal state. ``prepare``, ``apply``, and ``disable`` are
polarity ``other`` rather than write-verb commands; the disposable sandbox
marker is their safety gate, so L5's mechanical ``--commit`` default does not
bind this host-only lifecycle wrapper.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer

from solstone.think.sandbox_profile import (
    capabilities,
    envelope,
    intent,
    manifest,
    marker,
)
from solstone.think.utils import get_journal_info

app = typer.Typer(
    help="Manage disposable sandbox service profile.", no_args_is_help=True
)
log = logging.getLogger(__name__)

MAX_STDIN_BYTES = 64 * 1024
PRODUCTION_RECONCILER_ACTION = (
    "Relay instance retirement, portal token and binding revocation, and "
    "production storage purge are not performed here; the production-side "
    "reconciler owns them."
)


class PayloadReadError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _configure_logging(verbose: bool, debug: bool) -> None:
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level)


def _supported_contract_action() -> tuple[str, ...]:
    return (
        "Supported sandbox profile contract: "
        f"profile={manifest.PROFILE} "
        f"contract_version={manifest.CONTRACT_VERSION} "
        f"capabilities={','.join(manifest.CAPABILITY_ORDER)}.",
    )


def _supported_apply_action() -> tuple[str, ...]:
    return (f"Supported apply capabilities: {', '.join(manifest.APPLY_CAPABILITIES)}.",)


def _supported_disable_action() -> tuple[str, ...]:
    return ("Supported disable capabilities: all, scout, spl, spb, spp.",)


def _emit(result: envelope.Envelope, *, json_output: bool) -> NoReturn:
    body = (
        envelope.render_json(result)
        if json_output
        else envelope.summarize_human(result)
    )
    typer.echo(body, nl=False)
    raise typer.Exit(result.exit_code)


def _error(
    *,
    action: str,
    code: str,
    message: str,
    run_id: str | None = None,
    next_actions: tuple[str, ...] = (),
) -> envelope.Envelope:
    return envelope.error_envelope(
        action=action,
        code=code,
        message=message,
        run_id=run_id,
        next_actions=next_actions,
    )


def _resolved_journal_path() -> Path:
    path, _source = get_journal_info()
    return marker.canonical_path(path)


def _marker_context(action: str) -> marker.MarkerContext | envelope.Envelope:
    journal_path = _resolved_journal_path()
    try:
        return marker.validate_marker(journal_path)
    except marker.MarkerError as exc:
        return _error(
            action=action,
            code=exc.code,
            message=exc.message,
            run_id=None,
            next_actions=_supported_contract_action()
            if exc.code
            in {
                "sandbox_marker_wrong_contract_version",
                "sandbox_marker_wrong_profile",
            }
            else (),
        )


def _load_intent_for_context(
    ctx: marker.MarkerContext,
    *,
    require: bool,
) -> dict[str, Any] | None:
    if require:
        return intent.require_intent(ctx.journal_path, ctx.run_id)
    payload = intent.load_intent(ctx.journal_path)
    if payload is None:
        return None
    return intent.require_intent(ctx.journal_path, ctx.run_id)


def _capability_status(
    action: str,
    ctx: marker.MarkerContext,
    *,
    require_intent: bool,
) -> envelope.Envelope:
    try:
        intent_payload = _load_intent_for_context(ctx, require=require_intent)
    except FileNotFoundError:
        return _error(
            action=action, code="intent_missing", message="prepare is required first"
        )
    except intent.IntentRunMismatch:
        return _error(
            action=action,
            code="intent_run_mismatch",
            message="sandbox profile intent belongs to a different run",
            run_id=ctx.run_id,
        )
    except intent.IntentError:
        return _error(
            action=action, code="internal_error", message="intent is unreadable"
        )
    caps = capabilities.observe_capabilities(ctx.journal_path, intent_payload)
    return envelope.Envelope(
        action=action,
        profile=ctx.profile,
        run_id=ctx.run_id,
        state=capabilities.top_state(caps),
        capabilities=caps,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen.add(key)
        result[key] = value
    return result


def _read_payload() -> dict[str, Any]:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raw = sys.stdin.read(MAX_STDIN_BYTES + 1).encode("utf-8", "surrogateescape")
    else:
        raw = stream.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise PayloadReadError("payload exceeds 64 KiB")
    try:
        text = raw.decode("utf-8")
        stripped = text.lstrip()
        decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
        payload, end = decoder.raw_decode(stripped)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise PayloadReadError("payload must be one valid JSON object") from None
    if text[len(text) - len(stripped) + end :].strip():
        raise PayloadReadError("payload must not contain trailing content")
    if not isinstance(payload, dict):
        raise PayloadReadError("payload must be a JSON object")
    return payload


def _finalize_action(
    *,
    action: str,
    ctx: marker.MarkerContext,
    caps: tuple[envelope.CapabilityEnvelope, ...],
    next_actions: tuple[str, ...] = (),
) -> envelope.Envelope:
    return envelope.Envelope(
        action=action,
        profile=ctx.profile,
        run_id=ctx.run_id,
        state=capabilities.top_state(caps),
        capabilities=caps,
        next_actions=next_actions,
    )


def _cleanup_next_actions(
    caps: tuple[envelope.CapabilityEnvelope, ...],
) -> tuple[str, ...]:
    actions = [PRODUCTION_RECONCILER_ACTION]
    residuals = {residual for cap in caps for residual in cap.residuals}
    if "spp_credential_ownership_conflict" in residuals:
        actions.append(
            "Repair SPP credential ownership before treating the sandbox as clean."
        )
    if "spb_binding_missing" in residuals:
        actions.append(
            "Restore the hosted backup binding or create a fresh sandbox before retrying cleanup."
        )
    if "missing_expected_artifact" in residuals:
        actions.append(
            "Inspect the named local artifact and retry disable after restoring or removing it."
        )
    return tuple(actions)


@app.command("describe")
def describe(
    json_output: bool = typer.Option(
        True,
        "--json/--human",
        help="Emit machine JSON or redacted human text.",
    ),
    profile: str = typer.Option(manifest.PROFILE, "--profile", help="Profile name."),
    contract_version: int = typer.Option(
        manifest.CONTRACT_VERSION,
        "--contract-version",
        help="Sandbox profile contract version.",
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose logging."
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging."),
) -> None:
    _configure_logging(verbose, debug)
    action = "describe"
    if profile != manifest.PROFILE:
        _emit(
            _error(
                action=action,
                code="sandbox_marker_wrong_profile",
                message="profile is unsupported",
                next_actions=_supported_contract_action(),
            ),
            json_output=json_output,
        )
    if contract_version != manifest.CONTRACT_VERSION:
        _emit(
            _error(
                action=action,
                code="sandbox_marker_wrong_contract_version",
                message="contract_version is unsupported",
                next_actions=_supported_contract_action(),
            ),
            json_output=json_output,
        )
    ctx_or_error = _marker_context(action)
    if isinstance(ctx_or_error, envelope.Envelope):
        _emit(ctx_or_error, json_output=json_output)
    ctx = ctx_or_error
    result = envelope.Envelope(
        action=action,
        profile=ctx.profile,
        run_id=ctx.run_id,
        state=envelope.TOP_OK,
        capabilities=envelope.empty_capabilities(),
        next_actions=_supported_contract_action(),
    )
    _emit(result, json_output=json_output)


@app.command("prepare")
def prepare(
    json_output: bool = typer.Option(
        True,
        "--json/--human",
        help="Emit machine JSON or redacted human text.",
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose logging."
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging."),
) -> None:
    _configure_logging(verbose, debug)
    action = "prepare"
    ctx_or_error = _marker_context(action)
    if isinstance(ctx_or_error, envelope.Envelope):
        _emit(ctx_or_error, json_output=json_output)
    ctx = ctx_or_error
    try:
        caps = capabilities.prepare_runtime(ctx.journal_path, ctx.run_id)
    except intent.IntentRunMismatch:
        _emit(
            _error(
                action=action,
                code="intent_run_mismatch",
                message="sandbox profile intent belongs to a different run",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    except Exception:
        log.debug("sandbox profile prepare failed without payload details")
        _emit(
            _error(
                action=action,
                code="internal_error",
                message="prepare failed",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    _emit(_finalize_action(action=action, ctx=ctx, caps=caps), json_output=json_output)


@app.command("apply")
def apply(
    capability: str = typer.Argument(..., help="Capability to apply."),
    json_output: bool = typer.Option(
        True,
        "--json/--human",
        help="Emit machine JSON or redacted human text.",
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose logging."
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging."),
) -> None:
    _configure_logging(verbose, debug)
    action = "apply"
    ctx_or_error = _marker_context(action)
    if isinstance(ctx_or_error, envelope.Envelope):
        _emit(ctx_or_error, json_output=json_output)
    ctx = ctx_or_error
    if capability not in manifest.CAPABILITY_ORDER:
        _emit(
            _error(
                action=action,
                code="unknown_capability",
                message="capability is unknown",
                run_id=ctx.run_id,
                next_actions=_supported_apply_action(),
            ),
            json_output=json_output,
        )
    if capability == manifest.CAPABILITY_RUNTIME:
        _emit(
            _error(
                action=action,
                code="unsupported_capability_action",
                message="runtime is prepare-only",
                run_id=ctx.run_id,
                next_actions=_supported_apply_action(),
            ),
            json_output=json_output,
        )
    try:
        intent.require_intent(ctx.journal_path, ctx.run_id)
    except FileNotFoundError:
        _emit(
            _error(
                action=action,
                code="intent_missing",
                message="prepare is required first",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    except intent.IntentRunMismatch:
        _emit(
            _error(
                action=action,
                code="intent_run_mismatch",
                message="sandbox profile intent belongs to a different run",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    except intent.IntentError:
        _emit(
            _error(
                action=action,
                code="internal_error",
                message="intent is unreadable",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    try:
        payload = _read_payload()
        caps = capabilities.apply_capability(
            ctx.journal_path,
            ctx.run_id,
            capability,
            payload,
        )
    except (PayloadReadError, capabilities.PayloadValidationError):
        _emit(
            _error(
                action=action,
                code="payload_invalid",
                message="payload is invalid",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    except Exception:
        log.debug("sandbox profile apply failed without payload details")
        _emit(
            _error(
                action=action,
                code="internal_error",
                message="apply failed",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    _emit(_finalize_action(action=action, ctx=ctx, caps=caps), json_output=json_output)


@app.command("status")
def status(
    json_output: bool = typer.Option(
        True,
        "--json/--human",
        help="Emit machine JSON or redacted human text.",
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose logging."
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging."),
) -> None:
    _configure_logging(verbose, debug)
    action = "status"
    ctx_or_error = _marker_context(action)
    if isinstance(ctx_or_error, envelope.Envelope):
        _emit(ctx_or_error, json_output=json_output)
    _emit(
        _capability_status(action, ctx_or_error, require_intent=False),
        json_output=json_output,
    )


@app.command("disable")
def disable(
    capability: str | None = typer.Argument(
        None, help="Optional capability to disable."
    ),
    json_output: bool = typer.Option(
        True,
        "--json/--human",
        help="Emit machine JSON or redacted human text.",
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose logging."
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging."),
) -> None:
    _configure_logging(verbose, debug)
    action = "disable"
    ctx_or_error = _marker_context(action)
    if isinstance(ctx_or_error, envelope.Envelope):
        _emit(ctx_or_error, json_output=json_output)
    ctx = ctx_or_error
    if capability is not None:
        if capability not in manifest.CAPABILITY_ORDER:
            _emit(
                _error(
                    action=action,
                    code="unknown_capability",
                    message="capability is unknown",
                    run_id=ctx.run_id,
                    next_actions=_supported_disable_action(),
                ),
                json_output=json_output,
            )
        if capability == manifest.CAPABILITY_RUNTIME:
            _emit(
                _error(
                    action=action,
                    code="unsupported_capability_action",
                    message="runtime state is owned by the sandbox harness",
                    run_id=ctx.run_id,
                    next_actions=_supported_disable_action(),
                ),
                json_output=json_output,
            )
    try:
        caps = capabilities.disable_capabilities(
            ctx.journal_path, ctx.run_id, capability
        )
    except FileNotFoundError:
        _emit(
            _error(
                action=action,
                code="intent_missing",
                message="prepare is required first",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    except intent.IntentRunMismatch:
        _emit(
            _error(
                action=action,
                code="intent_run_mismatch",
                message="sandbox profile intent belongs to a different run",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    except Exception:
        log.debug("sandbox profile disable failed without payload details")
        _emit(
            _error(
                action=action,
                code="internal_error",
                message="disable failed",
                run_id=ctx.run_id,
            ),
            json_output=json_output,
        )
    _emit(
        _finalize_action(
            action=action,
            ctx=ctx,
            caps=caps,
            next_actions=_cleanup_next_actions(caps),
        ),
        json_output=json_output,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()

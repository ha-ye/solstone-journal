# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI entrypoint for provider connectivity checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from solstone.think.utils import get_journal, require_solstone, setup_cli


def _local_readiness_message(status: dict[str, object] | None = None) -> str:
    issues = (status or {}).get("issues") or []
    if issues:
        return "; ".join(str(issue) for issue in issues)
    return "Local provider not ready"


def _provider_status(provider_name: str) -> dict[str, object]:
    from solstone.think.providers import build_provider_status, get_provider_list

    provider = next(
        (item for item in get_provider_list() if item["name"] == provider_name),
        None,
    )
    if provider is None:
        return {}
    return build_provider_status([provider], vertex_creds_configured=False).get(
        provider_name,
        {},
    )


def _check_generate(
    provider_name: str,
    model: str,
    timeout: int,
) -> tuple[str, str, str | None]:
    """Check generate interface for a provider."""
    from solstone.think.providers import PROVIDER_METADATA, get_provider_module

    env_key = PROVIDER_METADATA[provider_name]["env_key"]
    if env_key and not os.getenv(env_key):
        label = PROVIDER_METADATA[provider_name]["label"]
        return "skip", f"{label} not configured (no {env_key})", "provider_key_missing"

    if not env_key:
        from solstone.think.providers import validate_key

        result = validate_key(provider_name, "")
        if not result.get("valid"):
            if provider_name == "local":
                from solstone.think.providers import state

                return (
                    "skip",
                    f"Local provider not ready ({result.get('error', 'not ready')})",
                    state.readiness_for_provider("local", "generate").reason_code
                    or "unknown",
                )
            return (
                "skip",
                f"{PROVIDER_METADATA[provider_name]['label']} not reachable "
                f"({result.get('error', 'unreachable')})",
                "unknown",
            )

    try:
        module = get_provider_module(provider_name)
        # Connectivity probe with canned content; deliberately outside the
        # confidential attestation gate so diagnostics can always run.
        result = module.run_generate(
            contents="Say OK",
            model=model,
            provider=provider_name,
            temperature=0,
            max_output_tokens=16,
            system_instruction=None,
            json_output=False,
            thinking_budget=None,
            timeout_s=timeout,
        )
        text = result.get("text", "") if isinstance(result, dict) else ""
        if text:
            usage = result.get("usage") if isinstance(result, dict) else None
            if usage:
                from solstone.think.models import log_token_usage

                log_token_usage(
                    model=model,
                    usage=usage,
                    context="health.check.generate",
                    type="generate",
                )
            return "ok", "OK", None
        return "fail", "FAIL: empty response text", "provider_response_invalid"
    except Exception as exc:
        from solstone.think.providers.shared import classify_provider_error

        return "fail", f"FAIL: {exc}", classify_provider_error(exc, provider_name)


async def _check_cogitate(
    provider_name: str, model: str, timeout: int
) -> tuple[str, str, str | None]:
    """Check cogitate interface for a provider by running a real prompt."""
    from solstone.think.providers import PROVIDER_METADATA, get_provider_module

    env_key = PROVIDER_METADATA[provider_name]["env_key"]
    label = PROVIDER_METADATA[provider_name]["label"]
    if provider_name == "local":
        status = _provider_status(provider_name)
        if not status.get("cogitate_ready"):
            from solstone.think.providers import state

            reason_code = (
                state.readiness_for_provider("local", "cogitate").reason_code
                or "unknown"
            )
            if not status.get("cogitate_cli_found"):
                from solstone.think.providers.local_endpoint import (
                    resolve_local_endpoint,
                )

                if not resolve_local_endpoint().is_bundled:
                    return "skip", _local_readiness_message(status), reason_code
                from solstone.think.providers import local_install

                return (
                    "skip",
                    f"not installed; run `{local_install.install_hint()}`",
                    reason_code,
                )
            return "skip", _local_readiness_message(status), reason_code
    elif provider_name in {"anthropic", "openai", "google"}:
        if env_key and not os.getenv(env_key):
            return (
                "skip",
                f"{label} not configured (no {env_key})",
                "provider_key_missing",
            )
    elif env_key and not os.getenv(env_key):
        return "skip", f"{label} not configured (no {env_key})", "provider_key_missing"

    if not env_key:
        from solstone.think.providers import validate_key

        result = validate_key(provider_name, "")
        if not result.get("valid"):
            if provider_name == "local":
                from solstone.think.providers import state

                return (
                    "skip",
                    f"Local provider not ready ({result.get('error', 'not ready')})",
                    state.readiness_for_provider("local", "cogitate").reason_code
                    or "unknown",
                )
            return (
                "skip",
                f"{PROVIDER_METADATA[provider_name]['label']} not reachable "
                f"({result.get('error', 'unreachable')})",
                "unknown",
            )

    try:
        module = get_provider_module(provider_name)
        config = {"prompt": "Say OK", "model": model, "provider": provider_name}
        # Connectivity probe with canned content; deliberately outside the
        # confidential attestation gate so diagnostics can always run.
        result = await asyncio.wait_for(
            module.run_cogitate(config=config, on_event=None),
            timeout=timeout,
        )
        if result:
            return "ok", "OK", None
        return "fail", "FAIL: empty response", "provider_response_invalid"
    except asyncio.TimeoutError:
        return "fail", f"FAIL: timed out after {timeout}s", "chat_timeout"
    except Exception as exc:
        from solstone.think.providers.shared import classify_provider_error

        return "fail", f"FAIL: {exc}", classify_provider_error(exc, provider_name)


async def _run_check(args: argparse.Namespace) -> None:
    """Run connectivity checks against AI providers."""
    from solstone.think.models import (
        NO_BRAIN_PROVIDER,
        default_model_for_provider,
        resolve_provider,
    )
    from solstone.think.providers import PROVIDER_REGISTRY

    lock_fd = None
    if args.targeted and not args.provider:
        import fcntl

        lock_dir = Path(get_journal()) / "health"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_dir / "recheck.lock", "w", encoding="utf-8")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            return

    if args.model and not args.provider:
        print("--model requires --provider", file=sys.stderr)
        sys.exit(1)

    if args.provider:
        providers = args.provider
        for name in providers:
            if name not in PROVIDER_REGISTRY:
                available = ", ".join(PROVIDER_REGISTRY.keys())
                print(
                    f"Unknown provider: {name}. Available providers: {available}",
                    file=sys.stderr,
                )
                sys.exit(1)
    else:
        providers = list(PROVIDER_REGISTRY.keys())

    interfaces = [args.interface] if args.interface else ["generate", "cogitate"]

    probes: list[tuple[str, str, str]] = []
    if args.targeted and not args.provider:
        for interface_name in interfaces:
            provider, model = resolve_provider(interface_name)
            if provider != NO_BRAIN_PROVIDER:
                probes.append((provider, model, interface_name))
    else:
        for provider_name in providers:
            model = args.model or default_model_for_provider(provider_name)
            for interface_name in interfaces:
                probes.append((provider_name, model, interface_name))

    provider_width = max((len(provider) for provider, _, _ in probes), default=0)
    model_width = max((len(model) for _, model, _ in probes), default=0)
    interface_width = max(len(n) for n in interfaces) if interfaces else 0

    total = 0
    passed = 0
    failed = 0
    skipped = 0
    results: list[dict[str, object]] = []

    for provider_name, model, interface_name in probes:
        start = time.perf_counter()
        if interface_name == "generate":
            status, message, reason_code = _check_generate(
                provider_name, model, args.timeout
            )
        else:
            status, message, reason_code = await _check_cogitate(
                provider_name, model, args.timeout
            )
        elapsed_s = time.perf_counter() - start
        elapsed_s_rounded = round(elapsed_s, 1)

        result: dict[str, object] = {
            "provider": provider_name,
            "model": model,
            "interface": interface_name,
            "ok": status != "fail",
            "status": status,
            "reason_code": reason_code,
            "message": str(message),
            "elapsed_s": elapsed_s_rounded,
        }
        results.append(result)

        if not args.json:
            if status == "ok":
                mark = "✓"
            elif status == "skip":
                mark = "-"
            else:
                mark = "✗"
            print(
                f"{mark} "
                f"{provider_name:<{provider_width}}  "
                f"{model:<{model_width}}  "
                f"{interface_name:<{interface_width}}  "
                f"{message} ({elapsed_s:.1f}s)"
            )

        total += 1
        if status == "ok":
            passed += 1
        elif status == "skip":
            skipped += 1
        else:
            failed += 1

    any_failed = any(r["status"] == "fail" for r in results)

    summary = {
        "total": total,
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
    }
    checked_at = datetime.now(timezone.utc).isoformat()

    from solstone.think.providers import state

    state.write_active_check(results, summary, checked_at)

    if args.json:
        print(
            json.dumps(
                {
                    "results": results,
                    "summary": summary,
                },
                indent=2,
            )
        )
    else:
        print(f"{total} checks: {passed} passed, {skipped} skipped, {failed} failed")
    if lock_fd is not None:
        lock_fd.close()
    sys.exit(1 if any_failed else 0)


async def main_async() -> None:
    """CLI entrypoint for provider connectivity checks."""
    from solstone.think.providers import PROVIDER_REGISTRY

    parser = argparse.ArgumentParser(description="solstone Provider CLI")
    subparsers = parser.add_subparsers(dest="subcommand")
    check_parser = subparsers.add_parser("check", help="Check AI provider connectivity")
    check_parser.add_argument(
        "--provider",
        action="append",
        help=f"Provider to check (repeatable). Available: {', '.join(PROVIDER_REGISTRY.keys())}",
    )
    check_parser.add_argument(
        "--interface",
        choices=["generate", "cogitate"],
        default=None,
        help="Interface to check (default: both)",
    )
    check_parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Timeout in seconds for generate checks (default: 30)",
    )
    check_parser.add_argument(
        "--model",
        default=None,
        help="Model to check with --provider (default: provider default model)",
    )
    check_parser.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )
    check_parser.add_argument(
        "--targeted",
        action="store_true",
        help="Only check configured active routes (used by automated rechecks)",
    )

    args = setup_cli(parser)
    require_solstone()
    if args.subcommand != "check":
        parser.print_help()
        sys.exit(1)
    await _run_check(args)


def main() -> None:
    """Entry point wrapper."""
    asyncio.run(main_async())

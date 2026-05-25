# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI for optional hosted solstone services."""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time
import webbrowser

from solstone.think.journal_config import get_journal_config_path
from solstone.think.services import portal_client
from solstone.think.services.scout import (
    JournalNotInitializedError,
    is_manual_key_present,
    is_scout_enabled,
    provision_scout_handoff,
)

MIN_WAIT_SECONDS = 60
MAX_WAIT_SECONDS = 3600

STDOUT_OPENING = "Opening services.solstone.app to enable scout..."
STDOUT_WAITING = "Waiting for you to finish in the browser (up to 15 minutes)..."
STDOUT_SUCCESS = "Scout enabled."

ERROR_MESSAGES: dict[str, str] = {
    "consent_link_expired": (
        "Browser approval expired. Rerun the command to start a fresh enable flow."
    ),
    "consent_timeout": (
        "The browser flow exceeded the wait budget. "
        "Rerun with a longer --wait if needed."
    ),
    "portal_unreachable": (
        "services.solstone.app could not be reached. Check network and try again."
    ),
    "tls_verification_failed": (
        "TLS verification failed while contacting services.solstone.app. "
        "Check system time, certificates, or network interception."
    ),
    "nonce_invalid": (
        "The enable request token was rejected. "
        "Rerun the command to create a fresh token."
    ),
    "unexpected_payload": (
        "The services response shape was unexpected. Update solstone and try again."
    ),
    "write_failed": (
        "Scout was approved, but journal config was not saved. "
        "Check <journal>/config permissions and retry."
    ),
    "already_enabled": "Scout is already enabled. No change needed.",
    "manual_key_present": (
        "A manual Gemini key is already present in journal config. "
        "Use --force to overwrite with a portal-provisioned key."
    ),
    "headless_no_browser": (
        "No browser is available from this shell. Rerun from a desktop session."
    ),
    "journal_not_initialized": (
        "Journal config file is missing. Run sol setup, then retry."
    ),
    "unknown_service": "Unknown service name. Use sol services enable scout.",
}

EXIT_CODES: dict[str, int] = {
    "already_enabled": 0,
    "manual_key_present": 0,
    "headless_no_browser": 2,
    "unknown_service": 2,
}


class _CliError(Exception):
    def __init__(self, token: str):
        super().__init__(token)
        self.token = token


class _ServicesArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "invalid choice" in message and "choose from scout" in message:
            _print_error("unknown_service")
            raise SystemExit(EXIT_CODES["unknown_service"])
        super().error(message)


def _print_error(token: str) -> None:
    print(f"{token}: {ERROR_MESSAGES[token]}", file=sys.stderr)


def _wait_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("wait must be an integer") from exc
    return max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, seconds))


def _build_parser() -> argparse.ArgumentParser:
    parser = _ServicesArgumentParser(description="Manage optional solstone services.")
    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
        parser_class=_ServicesArgumentParser,
    )

    enable_parser = subparsers.add_parser(
        "enable",
        help="enable an optional service",
    )
    service_parsers = enable_parser.add_subparsers(
        dest="service",
        metavar="{scout}",
        title="services",
        parser_class=_ServicesArgumentParser,
    )
    scout_parser = service_parsers.add_parser(
        "scout",
        help="enable scout",
    )
    scout_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing manual Gemini key with a portal-provisioned key.",
    )
    scout_parser.add_argument(
        "--wait",
        type=_wait_seconds,
        default=portal_client.DEFAULT_WAIT_SECONDS,
        metavar="SECONDS",
        help=(
            "Owner-patience budget for the browser flow, clamped to 60-3600 seconds."
        ),
    )
    scout_parser.set_defaults(handler=_enable_scout)
    return parser


def _is_headless() -> bool:
    if os.environ.get("SSH_TTY"):
        return True
    return (
        platform.system() == "Linux"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    )


def _open_browser(url: str) -> bool:
    return webbrowser.open(url, new=2)


def _poll_handoff(base_url: str, nonce: str, wait_seconds: int) -> dict:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        timeout = min(
            portal_client.POLL_TIMEOUT_SECONDS,
            max(0.1, deadline - time.monotonic()),
        )
        outcome = portal_client.poll_handoff_once(base_url, nonce, timeout=timeout)
        if outcome.kind == "success":
            return outcome.payload or {}
        if outcome.kind == "continue":
            continue
        if outcome.kind == "failed" and outcome.reason:
            raise _CliError(outcome.reason)
        raise _CliError("unexpected_payload")

    raise _CliError("consent_timeout")


def _enable_scout(args: argparse.Namespace) -> int:
    if not get_journal_config_path().exists():
        _print_error("journal_not_initialized")
        return 1

    if not args.force and is_scout_enabled():
        _print_error("already_enabled")
        return EXIT_CODES["already_enabled"]

    if not args.force and is_manual_key_present():
        _print_error("manual_key_present")
        return EXIT_CODES["manual_key_present"]

    base_url = portal_client.portal_base_url()
    if _is_headless():
        nonce = portal_client.mint_nonce()
        print(portal_client.browser_url(base_url, nonce))
        _print_error("headless_no_browser")
        return EXIT_CODES["headless_no_browser"]

    nonce = portal_client.mint_nonce()
    browser_url = portal_client.browser_url(base_url, nonce)
    print(STDOUT_OPENING)
    if not _open_browser(browser_url):
        print(browser_url)
        _print_error("headless_no_browser")
        return EXIT_CODES["headless_no_browser"]

    print(STDOUT_WAITING)
    try:
        payload = _poll_handoff(base_url, nonce, args.wait)
        provision_scout_handoff(payload)
    except _CliError as exc:
        _print_error(exc.token)
        return EXIT_CODES.get(exc.token, 1)
    except JournalNotInitializedError:
        _print_error("journal_not_initialized")
        return 1
    except ValueError:
        _print_error("unexpected_payload")
        return 1
    except Exception:
        _print_error("write_failed")
        return 1

    print(STDOUT_SUCCESS)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI for optional hosted solstone services."""

from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any

from solstone.think.journal_config import get_journal_config_path
from solstone.think.services import portal_client, scout, spl
from solstone.think.services.constants import SERVICE_SCOUT

logger = logging.getLogger(__name__)

MIN_WAIT_SECONDS = 60
MAX_WAIT_SECONDS = 3600

STDOUT_LINK_TEMPLATE = "To enable scout, open this link in any browser:\n\n    {url}\n"
STDOUT_OPENED_BROWSER = "Opening it in your browser now."
STDOUT_WAITING = "Waiting for you to finish in the browser (up to 15 minutes)..."
STDOUT_SUCCESS = "Scout enabled."
STDOUT_PENDING = "Scout request applied — pending review (submitted {since})."
STDOUT_REVOKED = "Scout access has ended."
STDOUT_REVOKED_PRESERVED_MANUAL_KEY = (
    "Scout access has ended — your manually-pasted key was preserved."
)
STDOUT_REFRESH = "Re-pulled scout status."
STDOUT_DISABLE_SUCCESS = "Scout disabled."
STDOUT_DISABLE_PRESERVED_MANUAL_KEY = (
    "Scout disabled — your manually-pasted key was preserved."
)
STDOUT_SPL_SUCCESS = "sol private link enabled."
STDOUT_SPL_DISABLE_SUCCESS = "sol private link disabled."

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
    "scout_server_bad_payload": (
        "services.solstone.app returned an incomplete scout approval. "
        "Retry shortly; if it persists, the portal is at fault."
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
    "already_disabled": "solstone scout is not enabled on this machine.",
    "spl_already_enabled": "sol private link is already enabled. No change needed.",
    "spl_already_disabled": "sol private link is not enabled on this machine.",
    "relay_unreachable": "The spl relay could not be reached. Check network and try again.",
    "journal_not_initialized": (
        "Journal config file is missing. Run journal setup, then retry."
    ),
    "unknown_service": "supported services are scout and spl.",
}

EXIT_CODES: dict[str, int] = {
    "already_enabled": 0,
    "manual_key_present": 0,
    "already_disabled": 0,
    "spl_already_enabled": 0,
    "spl_already_disabled": 0,
    "unknown_service": 2,
}


class _CliError(Exception):
    def __init__(self, token: str, detail: str | None = None):
        super().__init__(token)
        self.token = token
        self.detail = detail


class _ServicesArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "invalid choice" in message and "scout" in message:
            _print_error("unknown_service")
            raise SystemExit(EXIT_CODES["unknown_service"])
        super().error(message)


_verbose_parent = argparse.ArgumentParser(add_help=False)
_verbose_parent.add_argument(
    "-v",
    "--verbose",
    action="store_true",
    default=argparse.SUPPRESS,
    help="Enable DEBUG logging for this command.",
)


def _print_error(token: str, detail: str | None = None) -> None:
    message = f"{token}: {ERROR_MESSAGES[token]}"
    if detail:
        message += f" ({detail})"
    print(message, file=sys.stderr)


def _wait_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("wait must be an integer") from exc
    return max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, seconds))


def _format_since(since: Any) -> str:
    try:
        return datetime.fromtimestamp(int(since) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "recently"


def _build_parser() -> argparse.ArgumentParser:
    parser = _ServicesArgumentParser(
        description="Manage optional solstone services.",
        parents=[_verbose_parent],
    )
    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
        parser_class=_ServicesArgumentParser,
    )

    enable_parser = subparsers.add_parser(
        "enable",
        help="enable an optional service",
        parents=[_verbose_parent],
    )
    service_parsers = enable_parser.add_subparsers(
        dest="service",
        metavar="{scout,spl}",
        title="services",
        parser_class=_ServicesArgumentParser,
    )
    scout_parser = service_parsers.add_parser(
        "scout",
        help="enable scout",
        parents=[_verbose_parent],
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
    spl_parser = service_parsers.add_parser(
        "spl",
        help="enable sol private link",
        parents=[_verbose_parent],
    )
    spl_parser.set_defaults(handler=_enable_spl)

    disable_parser = subparsers.add_parser(
        "disable",
        help="disable an optional service",
        parents=[_verbose_parent],
    )
    disable_service_parsers = disable_parser.add_subparsers(
        dest="service",
        metavar="{scout,spl}",
        title="services",
        parser_class=_ServicesArgumentParser,
    )
    disable_scout_parser = disable_service_parsers.add_parser(
        "scout",
        help="disable scout",
        parents=[_verbose_parent],
    )
    disable_scout_parser.set_defaults(handler=_disable_scout)
    disable_spl_parser = disable_service_parsers.add_parser(
        "spl",
        help="disable sol private link",
        parents=[_verbose_parent],
    )
    disable_spl_parser.set_defaults(handler=_disable_spl)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="refresh optional service status",
        parents=[_verbose_parent],
    )
    refresh_service_parsers = refresh_parser.add_subparsers(
        dest="service",
        metavar="{scout}",
        title="services",
        parser_class=_ServicesArgumentParser,
    )
    refresh_scout_parser = refresh_service_parsers.add_parser(
        "scout",
        help="refresh scout",
        parents=[_verbose_parent],
    )
    refresh_scout_parser.add_argument(
        "--wait",
        type=_wait_seconds,
        default=portal_client.DEFAULT_WAIT_SECONDS,
        metavar="SECONDS",
        help=(
            "Owner-patience budget for the browser flow, clamped to 60-3600 seconds."
        ),
    )
    refresh_scout_parser.set_defaults(handler=_refresh_scout)
    return parser


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception as exc:
        logger.warning("scout browser open failed: %s", exc)
        return False


def _poll_handoff(
    base_url: str,
    nonce: str,
    wait_seconds: int,
    *,
    service: str = SERVICE_SCOUT,
) -> dict:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        timeout = min(
            portal_client.POLL_TIMEOUT_SECONDS,
            max(0.1, deadline - time.monotonic()),
        )
        outcome = portal_client.poll_handoff_once(
            base_url,
            nonce,
            timeout=timeout,
            service=service,
        )
        if outcome.kind == "success":
            return outcome.payload or {}
        if outcome.kind == "continue":
            continue
        if outcome.kind == "failed" and outcome.reason:
            raise _CliError(outcome.reason)
        raise _CliError("unexpected_payload")

    raise _CliError("consent_timeout")


def _apply_handoff(payload: dict) -> str:
    """Interpret a portal handoff payload by state and apply it.

    Returns the STDOUT message; raises _CliError for known bad-payload tokens.
    """

    try:
        result = scout.apply_scout_state(payload)
    except scout.ScoutPayloadError as exc:
        raise _CliError(exc.token, exc.detail) from exc
    if result.kind == "approved":
        return STDOUT_SUCCESS
    if result.kind == "pending":
        return STDOUT_PENDING.format(since=_format_since(result.since))
    if result.env_key_preserved:
        return STDOUT_REVOKED_PRESERVED_MANUAL_KEY
    return STDOUT_REVOKED


def _enable_scout(args: argparse.Namespace) -> int:
    if not get_journal_config_path().exists():
        _print_error("journal_not_initialized")
        return 1

    if not args.force and scout.is_scout_enabled():
        _print_error("already_enabled")
        return EXIT_CODES["already_enabled"]

    if not args.force and scout.is_manual_key_present():
        _print_error("manual_key_present")
        return EXIT_CODES["manual_key_present"]

    base_url = portal_client.portal_base_url()
    try:
        nonce = portal_client.mint_nonce()
        browser_url = portal_client.browser_url(
            base_url,
            nonce,
            service=SERVICE_SCOUT,
        )
        print(STDOUT_LINK_TEMPLATE.format(url=browser_url))
        if _open_browser(browser_url):
            print(STDOUT_OPENED_BROWSER)
        print(STDOUT_WAITING)
        payload = _poll_handoff(
            base_url,
            nonce,
            args.wait,
            service=SERVICE_SCOUT,
        )
        message = _apply_handoff(payload)
    except _CliError as exc:
        _print_error(exc.token, exc.detail)
        return EXIT_CODES.get(exc.token, 1)
    except scout.JournalNotInitializedError:
        _print_error("journal_not_initialized")
        return 1
    except Exception as exc:
        _print_error("write_failed", str(exc))
        return 1

    print(message)
    return 0


def _refresh_scout(args: argparse.Namespace) -> int:
    if not get_journal_config_path().exists():
        _print_error("journal_not_initialized")
        return 1

    base_url = portal_client.portal_base_url()
    try:
        nonce = portal_client.mint_nonce()
        browser_url = portal_client.browser_url(
            base_url,
            nonce,
            service=SERVICE_SCOUT,
        )
        print(STDOUT_LINK_TEMPLATE.format(url=browser_url))
        if _open_browser(browser_url):
            print(STDOUT_OPENED_BROWSER)
        print(STDOUT_WAITING)
        payload = _poll_handoff(
            base_url,
            nonce,
            args.wait,
            service=SERVICE_SCOUT,
        )
        message = _apply_handoff(payload)
    except _CliError as exc:
        _print_error(exc.token, exc.detail)
        return EXIT_CODES.get(exc.token, 1)
    except scout.JournalNotInitializedError:
        _print_error("journal_not_initialized")
        return 1
    except Exception as exc:
        _print_error("write_failed", str(exc))
        return 1

    print(STDOUT_REFRESH)
    print(message)
    return 0


def _disable_scout(_args: argparse.Namespace) -> int:
    try:
        outcome = scout.disable_scout()
    except scout.JournalNotInitializedError:
        _print_error("journal_not_initialized")
        return 1
    except Exception:
        _print_error("write_failed")
        return 1

    if not outcome.was_enabled:
        _print_error("already_disabled")
        return EXIT_CODES["already_disabled"]
    if outcome.env_key_preserved:
        print(STDOUT_DISABLE_PRESERVED_MANUAL_KEY)
        return 0
    print(STDOUT_DISABLE_SUCCESS)
    return 0


def _enable_spl(_args: argparse.Namespace) -> int:
    if not get_journal_config_path().exists():
        _print_error("journal_not_initialized")
        return 1

    if spl.is_spl_enabled():
        _print_error("spl_already_enabled")
        return EXIT_CODES["spl_already_enabled"]

    try:
        spl.enable_spl()
    except spl.JournalNotInitializedError:
        _print_error("journal_not_initialized")
        return 1
    except spl.RelayUnreachableError:
        _print_error("relay_unreachable")
        return 1
    except spl.RelayResponseError:
        _print_error("unexpected_payload")
        return 1
    except Exception:
        _print_error("write_failed")
        return 1

    print(STDOUT_SPL_SUCCESS)
    return 0


def _disable_spl(_args: argparse.Namespace) -> int:
    try:
        outcome = spl.disable_spl()
    except spl.JournalNotInitializedError:
        _print_error("journal_not_initialized")
        return 1
    except Exception:
        _print_error("write_failed")
        return 1

    if not outcome.was_enabled:
        _print_error("spl_already_disabled")
        return EXIT_CODES["spl_already_disabled"]

    print(STDOUT_SPL_DISABLE_SUCCESS)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "verbose", False):
        logging.basicConfig(level=logging.DEBUG)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

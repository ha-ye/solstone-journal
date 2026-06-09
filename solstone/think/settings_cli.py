# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Service CLI for local journal settings operations."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from solstone.apps.settings.copy import (
    CONVEY_NETWORK_DISABLE_DONE,
    CONVEY_NETWORK_DISABLE_PROGRESS,
    CONVEY_NETWORK_ENABLE_DONE,
    CONVEY_NETWORK_ENABLE_PROGRESS,
    CONVEY_REFUSE_NO_PASSWORD_NETWORK,
    CONVEY_RESTART_TIMEOUT,
)
from solstone.convey.cli import _resolve_bind_host
from solstone.convey.copy import format_convey_status
from solstone.convey.network_access import (
    NetworkAccessPasswordRequired,
    set_network_access,
)
from solstone.think.pairing.config import get_host_url
from solstone.think.service import DEFAULT_SERVICE_PORT
from solstone.think.utils import (
    get_config,
    read_service_port,
    require_solstone,
    setup_cli,
)


def _network_access_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("convey", {}).get("allow_network_access", False))


def _trust_localhost_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("convey", {}).get("trust_localhost", True))


def _convey_password_is_set(config: dict[str, Any]) -> bool:
    password_hash = config.get("convey", {}).get("password_hash", "")
    return bool(str(password_hash or "").strip())


def _host_url_status_value(config: dict[str, Any]) -> str:
    pairing_host_url = config.get("pairing", {}).get("host_url")
    if isinstance(pairing_host_url, str) and pairing_host_url.strip():
        return f"{get_host_url()} (manual override)"
    if _network_access_enabled(config):
        return f"{get_host_url()} (auto-detected)"
    return f"{get_host_url()} (localhost — network access off)"


def _convey_port() -> int:
    return read_service_port("convey") or DEFAULT_SERVICE_PORT


def _status_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "network_access_enabled": _network_access_enabled(config),
        "effective_host_url": get_host_url(),
        "password_configured": _convey_password_is_set(config),
    }


def _print_status(*, as_json: bool) -> None:
    config = get_config()
    if as_json:
        print(json.dumps(_status_payload(config), indent=2))
        return

    bind_host = _resolve_bind_host()
    port = _convey_port()
    print(
        format_convey_status(
            bind=f"{bind_host}:{port}",
            host_url=_host_url_status_value(config),
            network_access="on"
            if _network_access_enabled(config)
            else "localhost only",
            password="set" if _convey_password_is_set(config) else "not set",
            trust_localhost="yes" if _trust_localhost_enabled(config) else "no",
        )
    )


def _set_network_access(*, enable: bool) -> None:
    require_solstone()
    progress = (
        CONVEY_NETWORK_ENABLE_PROGRESS if enable else CONVEY_NETWORK_DISABLE_PROGRESS
    )
    try:
        result = set_network_access(
            enable=enable,
            on_restart=lambda: print(progress),
        )
    except NetworkAccessPasswordRequired:
        print(CONVEY_REFUSE_NO_PASSWORD_NETWORK, file=sys.stderr)
        sys.exit(1)

    if result["restart_timeout"]:
        print(CONVEY_RESTART_TIMEOUT, file=sys.stderr)
        sys.exit(1)

    if enable:
        print(
            CONVEY_NETWORK_ENABLE_DONE.format(
                host_url=result["effective_host_url"],
            )
        )
    else:
        print(CONVEY_NETWORK_DISABLE_DONE.format(port=_convey_port()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local journal settings")
    subparsers = parser.add_subparsers(dest="section")

    convey_parser = subparsers.add_parser("convey", help="Manage convey settings")
    convey_subparsers = convey_parser.add_subparsers(dest="convey_command")

    status_parser = convey_subparsers.add_parser(
        "status",
        help="Show convey network and host-URL status",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable status.",
    )

    network_parser = convey_subparsers.add_parser(
        "network-access",
        help="Manage convey network access",
    )
    network_subparsers = network_parser.add_subparsers(dest="network_command")
    network_subparsers.add_parser("enable", help="Enable network access")
    network_subparsers.add_parser("disable", help="Disable network access")

    args = setup_cli(parser)

    if args.section == "convey":
        if args.convey_command == "status":
            _print_status(as_json=bool(args.json))
            return
        if args.convey_command == "network-access":
            if args.network_command == "enable":
                _set_network_access(enable=True)
                return
            if args.network_command == "disable":
                _set_network_access(enable=False)
                return
            network_parser.print_help()
            sys.exit(1)
        convey_parser.print_help()
        sys.exit(1)

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()

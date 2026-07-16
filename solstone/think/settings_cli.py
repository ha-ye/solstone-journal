# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Service CLI for local journal settings operations."""

from __future__ import annotations

import argparse
import json
import sys

from solstone.convey.cli import _resolve_bind_host
from solstone.convey.copy import format_convey_status
from solstone.think.service import DEFAULT_SERVICE_PORT
from solstone.think.utils import (
    read_service_port,
    setup_cli,
)


def _convey_port() -> int:
    return read_service_port("convey") or DEFAULT_SERVICE_PORT


def _dashboard_url() -> str:
    return f"http://localhost:{_convey_port()}"


def _status_payload() -> dict[str, str]:
    return {
        "dashboard_url": _dashboard_url(),
    }


def _print_status(*, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_status_payload(), indent=2))
        return

    bind_host = _resolve_bind_host()
    port = _convey_port()
    print(
        format_convey_status(
            bind=f"{bind_host}:{port}",
            dashboard_url=f"http://localhost:{port}",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local journal settings")
    subparsers = parser.add_subparsers(dest="section")

    convey_parser = subparsers.add_parser("convey", help="Manage convey settings")
    convey_subparsers = convey_parser.add_subparsers(dest="convey_command")

    status_parser = convey_subparsers.add_parser(
        "status",
        help="Show convey bind and dashboard URL status",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable status.",
    )

    args = setup_cli(parser)

    if args.section == "convey":
        if args.convey_command == "status":
            _print_status(as_json=bool(args.json))
            return
        convey_parser.print_help()
        sys.exit(1)

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Caller-side `sol link` command namespace."""

from __future__ import annotations

import argparse
import sys

from solstone.think.link import join_cli, serve_cli


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="solstone link access commands")
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{join,serve}",
        title="commands",
    )
    join_parser = subparsers.add_parser(
        "join",
        help="join a solstone with a short code or pair link",
    )
    join_cli.add_arguments(join_parser)
    serve_parser = subparsers.add_parser(
        "serve",
        help="serve a loopback proxy over a link tunnel",
    )
    serve_cli.add_arguments(serve_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `sol link`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if sys.argv[0] == "journal link":
        from solstone.apps.link.call import app as link_management_app

        link_management_app(args=args, prog_name="journal link")
        return 0  # unreachable: the Typer app raises SystemExit

    parser = _build_parser()
    namespace = parser.parse_args(args)
    if namespace.command is None:
        parser.print_help()
        return 0
    if namespace.command == "join":
        return join_cli.main(namespace)
    if namespace.command == "serve":
        return serve_cli.main(namespace)
    return 0

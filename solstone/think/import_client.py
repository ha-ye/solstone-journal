# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Thin `sol import` client for the local Convey import API."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from solstone.think.convey_client import (
    ConveyClient,
    ConveyClientError,
    ConveyUnreachableError,
    get_client,
)

IMPORT_API = "/app/import/api"
MALFORMED_RESPONSE = "I couldn't read the journal response."
JOURNAL_HOST_HINT = "Run this on the journal host with `journal importer`."
MODE_DISPOSITIONS = {
    "positional_media": "http-client",
    "--timestamp": "http-client",
    "--facet": "http-client",
    "--setting": "http-client",
    "--source": "http-client",
    "--force": "http-client",
    "--auto": "http-client",
    "--dry-run": "reject-journal-host",
    "--json": "client-output",
    "-v/--verbose": "client-logging",
    "--backends": "reject-journal-host",
    "--sync": "reject-journal-host",
    "--save": "reject-journal-host",
    "--path": "reject-journal-host",
    "--list-importers": "reject-journal-host",
    "journal-source": "relocate-sol-call-import",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import media through the journal")
    parser.add_argument("media", nargs="?", help="Path to a file or journal-host path")
    parser.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument(
        "--timestamp", help="Timestamp YYYYMMDD_HHMMSS for journal entry"
    )
    parser.add_argument("--facet", help="Facet name for this import")
    parser.add_argument(
        "--setting",
        help="Contextual setting description to store with import metadata",
    )
    parser.add_argument(
        "--source",
        help="Import source type (apple, plaud, audio, text, or a file importer name)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-import by deleting existing import directory",
    )
    parser.add_argument(
        "--auto",
        nargs="?",
        const=True,
        default=None,
        help="Accept the server-detected timestamp",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to the journal",
    )
    parser.add_argument(
        "--backends",
        action="store_true",
        help="List syncable importer backends",
    )
    parser.add_argument(
        "--sync",
        metavar="BACKEND",
        help="Sync catalog from a backend",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="With --sync: download and import new files",
    )
    parser.add_argument(
        "--path",
        help="With --sync: override the default source directory path",
    )
    parser.add_argument(
        "--list-importers",
        action="store_true",
        help="List available file importers",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def _exit_rejected(parser: argparse.ArgumentParser, message: str) -> int:
    parser.exit(2, f"sol import: {message}\n")
    return 2


def _reject_unsupported_modes(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> int | None:
    if args.media == "journal-source":
        return _exit_rejected(
            parser,
            "journal-source management moved to `sol call import <verb>`.",
        )
    if args.dry_run:
        return _exit_rejected(
            parser, f"`--dry-run` requires the journal host. {JOURNAL_HOST_HINT}"
        )
    if args.backends:
        return _exit_rejected(
            parser, f"`--backends` requires the journal host. {JOURNAL_HOST_HINT}"
        )
    if args.list_importers:
        return _exit_rejected(
            parser,
            f"`--list-importers` requires the journal host. {JOURNAL_HOST_HINT}",
        )
    if args.sync:
        return _exit_rejected(
            parser, f"`--sync` requires the journal host. {JOURNAL_HOST_HINT}"
        )
    if args.save:
        return _exit_rejected(
            parser, f"`--save` requires the journal host. {JOURNAL_HOST_HINT}"
        )
    if args.path:
        return _exit_rejected(
            parser, f"`--path` requires the journal host. {JOURNAL_HOST_HINT}"
        )
    if args.auto not in (None, True):
        return _exit_rejected(
            parser,
            "`--auto <guidance>` requires the journal host. "
            "Use `--timestamp` here or run `journal importer`.",
        )
    return None


def _payload_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _save_media(client: ConveyClient, args: argparse.Namespace) -> dict[str, Any]:
    media_path = Path(args.media).expanduser()
    data = {
        key: value
        for key, value in {
            "facet": _payload_value(args.facet),
            "setting": _payload_value(args.setting),
        }.items()
        if value is not None
    }
    if media_path.exists() and media_path.is_file():
        return client.upload(
            f"{IMPORT_API}/save",
            files={
                "file": (
                    media_path.name,
                    media_path,
                    "application/octet-stream",
                )
            },
            data=data,
        )
    return client.request(
        "POST",
        f"{IMPORT_API}/save-path",
        json={
            **data,
            "path": str(media_path),
        },
    )


def _start_import(
    client: ConveyClient,
    args: argparse.Namespace,
    save_response: dict[str, Any],
) -> dict[str, Any]:
    path = save_response.get("path")
    timestamp = args.timestamp or save_response.get("timestamp")
    if not isinstance(path, str) or not path:
        raise ConveyClientError(MALFORMED_RESPONSE)
    if not isinstance(timestamp, str) or not timestamp:
        raise ConveyClientError(MALFORMED_RESPONSE)

    payload: dict[str, Any] = {
        "path": path,
        "timestamp": timestamp,
        "force": bool(args.force),
    }
    for key, value in {
        "facet": _payload_value(args.facet),
        "setting": _payload_value(args.setting),
        "source": _payload_value(args.source),
    }.items():
        if value is not None:
            payload[key] = value
    start_response = client.request("POST", f"{IMPORT_API}/start", json=payload)
    if not isinstance(start_response, dict):
        raise ConveyClientError(MALFORMED_RESPONSE)
    task_id = start_response.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ConveyClientError(MALFORMED_RESPONSE)
    return start_response


def _is_malformed(err: ConveyClientError) -> bool:
    return err.error == MALFORMED_RESPONSE


def _print_client_error(operation: str, err: ConveyClientError) -> None:
    if _is_malformed(err):
        print("sol import: couldn't read journal response", file=sys.stderr)
        return
    print(f"sol import: failed to {operation}: {err.error}", file=sys.stderr)
    if err.detail:
        print(f"sol import: {err.detail}", file=sys.stderr)


def _print_partial_error(staged_path: str, err: ConveyClientError) -> None:
    if _is_malformed(err):
        print(
            f"sol import: staged {staged_path} but processing was not queued: "
            "couldn't read journal response",
            file=sys.stderr,
        )
        return
    print(
        f"sol import: staged {staged_path} but processing was not queued: {err.error}",
        file=sys.stderr,
    )
    if err.detail:
        print(f"sol import: {err.detail}", file=sys.stderr)


def _print_success(
    save_response: dict[str, Any],
    start_response: dict[str, Any],
    *,
    json_out: bool,
) -> None:
    timestamp = save_response.get("timestamp")
    path = save_response.get("path")
    if json_out:
        print(
            json.dumps(
                {
                    "status": "queued",
                    "path": path,
                    "timestamp": timestamp,
                    "save": save_response,
                    "start": start_response,
                },
                sort_keys=True,
            )
        )
        return

    print(f"staged {path}")
    if timestamp:
        print(f"timestamp {timestamp}")
    task_id = start_response.get("task_id")
    if task_id:
        print(f"queued processing task {task_id}")
    else:
        print("queued processing")


def _run(args: argparse.Namespace, client: ConveyClient) -> int:
    try:
        save_response = _save_media(client, args)
        if not isinstance(save_response, dict):
            raise ConveyClientError(MALFORMED_RESPONSE)
    except ConveyUnreachableError:
        print(
            "sol import: couldn't reach the journal. Start it with 'journal up' and retry.",
            file=sys.stderr,
        )
        return 1
    except ConveyClientError as err:
        _print_client_error("stage import", err)
        return 1

    staged_path = str(save_response.get("path") or args.media)
    try:
        start_response = _start_import(client, args, save_response)
    except ConveyUnreachableError:
        print(
            f"sol import: staged {staged_path} but processing was not queued: "
            "couldn't reach the journal",
            file=sys.stderr,
        )
        return 1
    except ConveyClientError as err:
        _print_partial_error(staged_path, err)
        return 1

    _print_success(save_response, start_response, json_out=bool(args.json))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    client: ConveyClient | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    rejected = _reject_unsupported_modes(parser, args)
    if rejected is not None:
        return rejected
    if args.extra:
        parser.error(f"unexpected argument(s): {' '.join(args.extra)}")
    if not args.media:
        parser.error("the following arguments are required: media")

    return _run(args, client or get_client())


if __name__ == "__main__":
    raise SystemExit(main())

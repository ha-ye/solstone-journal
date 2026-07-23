#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import click
import typer.main

from solstone.think.call import call_app

SOURCE = "ce65d06ba67ca4fad85ba3b3f71a1eec359bc6e5"
SCHEMA = "sol-call-grammar-v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "core/fixtures/native-sol/sol-call-grammar-v1.json"


def param_object(param: click.Parameter) -> dict[str, Any]:
    return {
        "name": param.name,
        "kind": param.param_type_name,
        "type": param.type.name,
        "required": param.required,
        "nargs": param.nargs,
        "multiple": param.multiple,
        "default": param.default,
        "options": list(getattr(param, "opts", None) or []),
        "secondary": list(getattr(param, "secondary_opts", None) or []),
        "hidden": param.hidden,
        "is_flag": getattr(param, "is_flag", False) or False,
        "count": getattr(param, "count", False) or False,
        "flag_value": getattr(param, "flag_value", None) or None,
    }


def entry_object(
    command: click.Command, path: tuple[str, ...], kind: str
) -> dict[str, Any]:
    return {
        "path": list(path),
        "kind": kind,
        "help": command.help or "",
        "params": [param_object(param) for param in command.params],
    }


def collect_entries(
    command: click.Command, path: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(command, click.Group):
        if command.callback is not None:
            entries.append(entry_object(command, path, "callback"))
        for name in sorted(command.commands):
            entries.extend(collect_entries(command.commands[name], path + (name,)))
        return entries
    entries.append(entry_object(command, path, "command"))
    return entries


def build_oracle() -> bytes:
    root = typer.main.get_command(call_app)
    entries = collect_entries(root)
    payload = {"schema": SCHEMA, "source": SOURCE, "entries": entries}
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def describe(data: bytes) -> str:
    payload = json.loads(data)
    return (
        f"entries={len(payload['entries'])} bytes={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the frozen native sol grammar oracle."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to write or check.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the output path is stale instead of rewriting it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    data = build_oracle()
    if args.check:
        existing = output.read_bytes()
        if existing != data:
            print(f"{output} is stale")
            print(f"expected {describe(data)}")
            print(f"actual   {describe(existing)}")
            return 1
        print(f"{output} is current: {describe(existing)}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    print(f"wrote {output}: {describe(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

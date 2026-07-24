#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "solstone/think/sol_cli.py"
OUTPUT = (
    REPO_ROOT / "core/crates/solstone-core-sol/src/generated/journal_host_commands.rs"
)
EXPECTED_COUNT = 44
SENTINELS = {"think", "setup", "up"}


def call_surface(node: ast.AST, position: int) -> str | None:
    if not isinstance(node, ast.Call) or len(node.args) <= position:
        return None
    surface = node.args[position]
    if isinstance(surface, ast.Constant) and isinstance(surface.value, str):
        return surface.value
    return None


def extract() -> list[str]:
    tree = ast.parse(SOURCE.read_text())
    commands: list[str] = []
    aliases: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        else:
            continue
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Dict):
            continue
        if target.id == "COMMANDS":
            commands.extend(dict_names_with_surface(value, "service", 1))
        elif target.id == "ALIASES":
            aliases.extend(dict_names_with_surface(value, "service", 2))
    result = sorted(set(commands + aliases))
    if not result:
        raise RuntimeError("journal-host command extraction is empty")
    missing = sorted(SENTINELS - set(result))
    if missing:
        raise RuntimeError(f"missing journal-host sentinels: {missing}")
    if len(result) != EXPECTED_COUNT:
        raise RuntimeError(
            f"journal-host command count {len(result)} != {EXPECTED_COUNT}: {result!r}"
        )
    return result


def dict_names_with_surface(node: ast.Dict, surface: str, position: int) -> list[str]:
    names: list[str] = []
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if call_surface(value, position) == surface:
            names.append(key.value)
    return names


def rust_string(value: str) -> str:
    return repr(value).replace("'", '"')


def render(commands: list[str]) -> str:
    lines = [
        "// SPDX-License-Identifier: AGPL-3.0-only",
        "// Copyright (c) 2026 sol pbc",
        "",
        f"pub const JOURNAL_HOST_COMMAND_COUNT: usize = {len(commands)};",
        "pub const JOURNAL_HOST_COMMANDS: &[&str] = &[",
    ]
    lines.extend(f"    {rust_string(command)}," for command in commands)
    lines.extend(["];", ""])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build native sol journal-host command list."
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    rendered = render(extract())
    if args.check:
        if not output.is_file():
            print(f"{output} is missing")
            return 1
        if output.read_text() != rendered:
            print(f"{output} is stale; run make build-native-sol-journal-host-commands")
            return 1
        print(f"{output} is current")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

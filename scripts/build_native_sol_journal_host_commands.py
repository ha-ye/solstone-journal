#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "solstone/think/sol_cli.py"
OUTPUT = (
    REPO_ROOT / "core/crates/solstone-core-sol/src/generated/journal_host_commands.rs"
)
EXPECTED_SERVICE_COMMANDS_COUNT = 42
EXPECTED_UNIVERSAL_COMMANDS = frozenset({"doctor", "check", "contract", "link"})
EXPECTED_SERVICE_ALIASES = frozenset({"up", "down"})
EXPECTED_UNIVERSAL_ALIASES = frozenset()
SERVICE_SENTINELS = frozenset({"think", "setup"})


@dataclass(frozen=True)
class JournalHostCommandPartitions:
    service_commands: tuple[str, ...]
    universal_commands: tuple[str, ...]
    service_aliases: tuple[str, ...]
    universal_aliases: tuple[str, ...]


def call_surface(node: ast.AST, position: int) -> str | None:
    if not isinstance(node, ast.Call) or len(node.args) <= position:
        return None
    surface = node.args[position]
    if isinstance(surface, ast.Constant) and isinstance(surface.value, str):
        return surface.value
    return None


def extract_partitions(source_text: str | None = None) -> JournalHostCommandPartitions:
    tree = ast.parse(source_text if source_text is not None else SOURCE.read_text())
    commands: dict[str, list[str]] = {"service": [], "universal": []}
    aliases: dict[str, list[str]] = {"service": [], "universal": []}
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
            extend_names_by_surface(commands, value, 1)
        elif target.id == "ALIASES":
            extend_names_by_surface(aliases, value, 2)
    partitions = JournalHostCommandPartitions(
        service_commands=tuple(sorted(commands["service"])),
        universal_commands=tuple(sorted(commands["universal"])),
        service_aliases=tuple(sorted(aliases["service"])),
        universal_aliases=tuple(sorted(aliases["universal"])),
    )
    validate_partitions(partitions)
    return partitions


def extract(source_text: str | None = None) -> list[str]:
    partitions = extract_partitions(source_text)
    return sorted(set(partitions.service_commands + partitions.service_aliases))


def validate_partitions(partitions: JournalHostCommandPartitions) -> None:
    result = sorted(
        set(
            partitions.service_commands
            + partitions.universal_commands
            + partitions.service_aliases
            + partitions.universal_aliases
        )
    )
    if not result:
        raise RuntimeError("journal-host command extraction is empty")

    command_alias_overlap = sorted(
        set(partitions.service_commands + partitions.universal_commands)
        & set(partitions.service_aliases + partitions.universal_aliases)
    )
    if command_alias_overlap:
        raise RuntimeError(
            f"journal-host COMMANDS and ALIASES overlap: {command_alias_overlap!r}"
        )

    missing = sorted(SERVICE_SENTINELS - set(partitions.service_commands))
    if missing:
        raise RuntimeError(
            f"journal-host service COMMANDS missing sentinels: {missing!r}"
        )

    if len(partitions.service_commands) != EXPECTED_SERVICE_COMMANDS_COUNT:
        raise RuntimeError(
            "journal-host service COMMANDS count "
            f"{len(partitions.service_commands)} != {EXPECTED_SERVICE_COMMANDS_COUNT}: "
            f"{list(partitions.service_commands)!r}"
        )

    assert_exact_set(
        "journal-host universal COMMANDS",
        set(partitions.universal_commands),
        EXPECTED_UNIVERSAL_COMMANDS,
        changed_surface=set(partitions.service_commands),
    )
    assert_exact_set(
        "journal-host service ALIASES",
        set(partitions.service_aliases),
        EXPECTED_SERVICE_ALIASES,
        changed_surface=set(partitions.universal_aliases),
    )
    if set(partitions.universal_aliases) != EXPECTED_UNIVERSAL_ALIASES:
        raise RuntimeError(
            "journal-host universal ALIASES must be empty; "
            f"found={list(partitions.universal_aliases)!r}"
        )


def assert_exact_set(
    label: str,
    actual: set[str],
    expected: frozenset[str],
    *,
    changed_surface: set[str],
) -> None:
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    changed = sorted(set(missing) & changed_surface)
    raise RuntimeError(
        f"{label} drifted; missing={missing!r}; extra={extra!r}; "
        f"changed_surface={changed!r}"
    )


def extend_names_by_surface(
    names_by_surface: dict[str, list[str]], node: ast.Dict, position: int
) -> None:
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        surface = call_surface(value, position)
        if surface in names_by_surface:
            names_by_surface[surface].append(key.value)


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

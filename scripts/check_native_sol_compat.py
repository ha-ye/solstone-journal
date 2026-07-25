#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Static checks for the private native-sol Python compatibility entry."""

from __future__ import annotations

from pathlib import Path

from typer.main import get_command

try:
    from scripts.build_native_sol_inventory import (
        FINAL_JOURNAL_PYTHON_COMPAT_TOTAL,
        ORACLE_PATH,
        REPO_ROOT,
        check_complete_partition,
        collect_oracle_paths,
        discover,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution path.
    from build_native_sol_inventory import (  # type: ignore[no-redef]
        FINAL_JOURNAL_PYTHON_COMPAT_TOTAL,
        ORACLE_PATH,
        REPO_ROOT,
        check_complete_partition,
        collect_oracle_paths,
        discover,
    )

from solstone.think.sol_compat_inventory import COMPAT_MODULE
from solstone.think.tools.call import app as journal_app

COMPAT_EXEC_OWNER = REPO_ROOT / "core/crates/solstone-core-sol/src/lib.rs"
CLIENT_CRATES = (
    REPO_ROOT / "core/crates/solstone-core-sol-client",
    REPO_ROOT / "core/crates/solstone-core-sol-client-cli",
)


def derive_journal_typer_paths() -> set[tuple[str, ...]]:
    command = get_command(journal_app)
    paths = _walk_click_command(command, ("journal",))
    return paths


def _walk_click_command(
    command: object, prefix: tuple[str, ...]
) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    callback = getattr(command, "callback", None)
    if callback is not None:
        paths.add(prefix)
    commands = getattr(command, "commands", None)
    if isinstance(commands, dict):
        for name, child in sorted(commands.items()):
            paths.update(_walk_click_command(child, (*prefix, name)))
    return paths


def frozen_journal_remainder_paths() -> tuple[list[str], set[tuple[str, ...]]]:
    entries = discover(REPO_ROOT)
    errors = check_complete_partition(entries, ORACLE_PATH)
    oracle_errors, oracle_paths = collect_oracle_paths(ORACLE_PATH)
    errors.extend(oracle_errors)
    authority_paths = {entry.path for entry in entries if entry.surface == "sol-call"}
    remainder = {
        path for path in oracle_paths - authority_paths if path and path[0] == "journal"
    }
    if not remainder:
        errors.append("frozen journal compatibility remainder is empty")
    if len(remainder) != FINAL_JOURNAL_PYTHON_COMPAT_TOTAL:
        errors.append(
            "frozen journal compatibility remainder count "
            f"{len(remainder)} != {FINAL_JOURNAL_PYTHON_COMPAT_TOTAL}"
        )
    return errors, remainder


def check_journal_subtree() -> list[str]:
    errors, expected = frozen_journal_remainder_paths()
    actual = derive_journal_typer_paths()
    if not actual:
        errors.append("derived journal Typer path set is empty")
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"journal compat Typer paths missing: {format_paths(missing)}")
    if extra:
        errors.append(
            f"journal compat Typer paths outside oracle: {format_paths(extra)}"
        )
    return errors


def check_compat_module_boundary() -> list[str]:
    errors: list[str] = []
    owner_text = COMPAT_EXEC_OWNER.read_text(encoding="utf-8")
    if COMPAT_MODULE not in owner_text:
        errors.append(
            f"{rel(COMPAT_EXEC_OWNER)} does not invoke {COMPAT_MODULE}; "
            "compatibility exec belongs in solstone-core-sol"
        )
    for path in native_boundary_files():
        if path == COMPAT_EXEC_OWNER:
            continue
        text = path.read_text(encoding="utf-8")
        if COMPAT_MODULE in text:
            errors.append(
                f"{rel(path)} mentions {COMPAT_MODULE}; "
                "compatibility module exec belongs only in solstone-core-sol"
            )
    return errors


def native_boundary_files() -> list[Path]:
    files = {entry.source for entry in discover(REPO_ROOT)}
    for crate in CLIENT_CRATES:
        files.update(crate.rglob("*.rs"))
    return sorted(path for path in files if path.is_file())


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def format_paths(paths: list[tuple[str, ...]]) -> list[list[str]]:
    return [list(path) for path in paths]


def main() -> int:
    errors = check_journal_subtree()
    errors.extend(check_compat_module_boundary())
    if errors:
        print("native sol compatibility violations:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("native sol compatibility ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

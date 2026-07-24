# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Private native-sol compatibility dispatcher."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from types import ModuleType

import typer

from solstone.think.sol_compat_inventory import (
    EXIT_SOFTWARE,
    EXIT_USAGE,
    RECURSION_ERROR,
    SENTINEL,
    SENTINEL_ACTIVE,
    SENTINEL_ARMED,
    UNSUPPORTED_ERROR,
    CompatTarget,
    parse_marker,
    resolve_target,
)

Runner = Callable[[CompatTarget], int]


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
) -> int:
    if os.environ.get(SENTINEL) != SENTINEL_ARMED:
        sys.stderr.write(f"{RECURSION_ERROR}\n")
        return EXIT_SOFTWARE
    os.environ[SENTINEL] = SENTINEL_ACTIVE

    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        sys.stderr.write(f"{UNSUPPORTED_ERROR}\n")
        return EXIT_USAGE
    public_argv0 = parse_marker(raw_args[0])
    if public_argv0 is None:
        sys.stderr.write(f"{UNSUPPORTED_ERROR}\n")
        return EXIT_USAGE

    target = resolve_target(public_argv0, raw_args[1:])
    if target is None:
        sys.stderr.write(f"{UNSUPPORTED_ERROR}\n")
        return EXIT_USAGE

    with _patched_argv(target.argv):
        return (runner or run_target)(target)


def run_target(target: CompatTarget) -> int:
    try:
        module = importlib.import_module(target.module)
    except ImportError as exc:
        print(
            f"Error: Could not import module '{target.module}': {exc}",
            file=sys.stderr,
        )
        return 1
    if target.kind == "main":
        return _run_main(module)
    return _run_typer_app(module, target)


def _run_main(module: ModuleType) -> int:
    entry = getattr(module, "main", None)
    if entry is None:
        print(
            f"Error: Module '{module.__name__}' has no main() function",
            file=sys.stderr,
        )
        return 1
    return _call_entry(entry)


def _run_typer_app(module: ModuleType, target: CompatTarget) -> int:
    app = getattr(module, "app", None)
    if not isinstance(app, typer.Typer):
        print(f"Error: Module '{target.module}' has no Typer app", file=sys.stderr)
        return 1
    return _call_entry(app)


def _call_entry(entry: Callable[[], object]) -> int:
    try:
        result = entry()
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            return 1
        return 0 if not code else 1
    return 0 if result is None else int(result)


@contextmanager
def _patched_argv(argv: list[str]):
    previous = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = previous

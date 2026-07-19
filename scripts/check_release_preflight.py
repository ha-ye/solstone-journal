#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Release environment checks for the Rust toolchain and wheel rail."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

EXPECTED_ZIG_VERSION = "0.16.0"
TOOLCHAIN_FILE = "rust-toolchain.toml"


@dataclass(frozen=True)
class ToolchainSpec:
    channel: str
    components: tuple[str, ...]
    targets: tuple[str, ...]
    profile: str | None


@dataclass(frozen=True)
class Failure:
    error: str
    expected: str
    actual: str
    repair: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _format_failures(failures: Sequence[Failure]) -> None:
    for failure in failures:
        print(f"ERROR: {failure.error}", file=sys.stderr)
        print(f"  expected: {failure.expected}", file=sys.stderr)
        print(f"  actual: {failure.actual}", file=sys.stderr)
        print(f"  repair command: {failure.repair}", file=sys.stderr)


def load_toolchain_spec(root: Path) -> ToolchainSpec:
    path = root / TOOLCHAIN_FILE
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    toolchain = data.get("toolchain", {})
    channel = toolchain.get("channel")
    components = toolchain.get("components", [])
    targets = toolchain.get("targets", [])
    profile = toolchain.get("profile")
    if not isinstance(channel, str) or not channel:
        raise ValueError(f"{TOOLCHAIN_FILE} must set [toolchain].channel")
    if not isinstance(components, list) or not all(
        isinstance(item, str) for item in components
    ):
        raise ValueError(f"{TOOLCHAIN_FILE} [toolchain].components must be strings")
    if not isinstance(targets, list) or not all(
        isinstance(item, str) for item in targets
    ):
        raise ValueError(f"{TOOLCHAIN_FILE} [toolchain].targets must be strings")
    if profile is not None and not isinstance(profile, str):
        raise ValueError(f"{TOOLCHAIN_FILE} [toolchain].profile must be a string")
    return ToolchainSpec(
        channel=channel,
        components=tuple(components),
        targets=tuple(targets),
        profile=profile,
    )


def check_rustup_override(
    expected: str,
    env: Mapping[str, str],
) -> list[Failure]:
    actual = env.get("RUSTUP_TOOLCHAIN")
    if actual is None or actual == expected:
        return []
    return [
        Failure(
            error="RUSTUP_TOOLCHAIN overrides the pinned release toolchain",
            expected=f"RUSTUP_TOOLCHAIN unset or {expected}",
            actual=actual,
            repair=f"unset RUSTUP_TOOLCHAIN || export RUSTUP_TOOLCHAIN={expected}",
        )
    ]


def rustup_home(env: Mapping[str, str]) -> Path:
    value = env.get("RUSTUP_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".rustup"


def find_installed_toolchain(expected: str, rustup_home_path: Path) -> Path | None:
    toolchains = rustup_home_path / "toolchains"
    if not toolchains.is_dir():
        return None
    candidates = sorted(
        path
        for path in toolchains.iterdir()
        if path.is_dir()
        and (path.name == expected or path.name.startswith(f"{expected}-"))
        and (path / "bin" / "rustc").is_file()
    )
    return candidates[0] if candidates else None


def check_toolchain_installed(
    expected: str,
    rustup_home_path: Path,
) -> tuple[Path | None, list[Failure]]:
    toolchain_dir = find_installed_toolchain(expected, rustup_home_path)
    if toolchain_dir is not None:
        return toolchain_dir, []
    return (
        None,
        [
            Failure(
                error="required Rust toolchain is not installed",
                expected=f"installed rustup toolchain {expected}",
                actual=f"no {expected}-* directory with bin/rustc under {rustup_home_path / 'toolchains'}",
                repair=f"rustup toolchain install {expected}",
            )
        ],
    )


def check_rustc_version(
    toolchain_dir: Path,
    expected: str,
    *,
    runner: Runner = subprocess.run,
) -> list[Failure]:
    rustc = toolchain_dir / "bin" / "rustc"
    result = runner(
        [str(rustc), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    actual = (
        result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}"
    )
    if result.returncode != 0 or not actual.startswith(f"rustc {expected} "):
        return [
            Failure(
                error="rustc version does not match the pinned release toolchain",
                expected=f"rustc {expected}",
                actual=actual,
                repair=f"rustup toolchain install {expected}",
            )
        ]
    return []


def check_zig(
    expected: str = EXPECTED_ZIG_VERSION,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = subprocess.run,
) -> list[Failure]:
    zig = which("zig")
    if zig is None:
        return [
            Failure(
                error="zig is not on PATH",
                expected=f"zig {expected}",
                actual="not found",
                repair=f"python3 -m pip install --user ziglang=={expected}",
            )
        ]
    result = runner(
        [zig, "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    actual = (
        result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}"
    )
    if result.returncode != 0 or actual != expected:
        return [
            Failure(
                error="zig version does not match the supported wheel rail",
                expected=expected,
                actual=actual,
                repair=f"python3 -m pip install --user ziglang=={expected}",
            )
        ]
    return []


def check_local_clean_status(status_output: str) -> list[Failure]:
    paths = [line for line in status_output.splitlines() if line.strip()]
    if not paths:
        return []
    return [
        Failure(
            error="working tree is dirty",
            expected="no tracked or untracked changes",
            actual="; ".join(paths),
            repair="git status --short --untracked-files=normal",
        )
    ]


def local_status(root: Path, *, runner: Runner = subprocess.run) -> str:
    result = runner(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    return result.stdout


def check_remote_state(
    expected_ref: str,
    actual_ref: str,
    status_output: str,
    *,
    label: str,
) -> list[Failure]:
    failures: list[Failure] = []
    actual_ref = actual_ref.strip()
    if actual_ref != expected_ref:
        failures.append(
            Failure(
                error=f"{label} is not on the release ref",
                expected=expected_ref,
                actual=actual_ref or "<empty>",
                repair=f"git fetch origin && git checkout {expected_ref}",
            )
        )
    for failure in check_local_clean_status(status_output):
        failures.append(
            Failure(
                error=f"{label} working tree is dirty",
                expected=failure.expected,
                actual=failure.actual,
                repair="git status --short --untracked-files=normal",
            )
        )
    return failures


def check_pinned_toolchain(root: Path, env: Mapping[str, str]) -> list[Failure]:
    try:
        spec = load_toolchain_spec(root)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [
            Failure(
                error="release toolchain file is invalid",
                expected=f"valid {TOOLCHAIN_FILE} with [toolchain].channel",
                actual=str(exc),
                repair="$EDITOR rust-toolchain.toml",
            )
        ]
    failures = check_rustup_override(spec.channel, env)
    toolchain_dir, install_failures = check_toolchain_installed(
        spec.channel,
        rustup_home(env),
    )
    failures.extend(install_failures)
    if toolchain_dir is not None:
        failures.extend(check_rustc_version(toolchain_dir, spec.channel))
    return failures


def check_named_toolchain(toolchain: str, env: Mapping[str, str]) -> list[Failure]:
    toolchain_dir, failures = check_toolchain_installed(toolchain, rustup_home(env))
    if toolchain_dir is not None:
        failures.extend(check_rustc_version(toolchain_dir, toolchain))
    return failures


def _cmd_local(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    failures = check_pinned_toolchain(root, os.environ)
    failures.extend(check_zig())
    if args.require_clean:
        try:
            status = local_status(root)
        except RuntimeError as exc:
            failures.append(
                Failure(
                    error="could not inspect local working tree",
                    expected="git status succeeds",
                    actual=str(exc),
                    repair="git status --short --untracked-files=normal",
                )
            )
        else:
            failures.extend(check_local_clean_status(status))
    if failures:
        _format_failures(failures)
        return 1
    print("release local preflight ok")
    return 0


def _cmd_msrv(args: argparse.Namespace) -> int:
    failures = check_named_toolchain(args.toolchain, os.environ)
    if failures:
        _format_failures(failures)
        return 1
    print(f"rust MSRV toolchain {args.toolchain} ok")
    return 0


def _cmd_remote_state(args: argparse.Namespace) -> int:
    status = args.status_file.read_text(encoding="utf-8")
    failures = check_remote_state(
        args.expected_ref,
        args.actual_ref,
        status,
        label=args.label,
    )
    if failures:
        _format_failures(failures)
        return 1
    print(f"{args.label} release state ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    local = subparsers.add_parser("local")
    local.add_argument("--root", type=Path, default=Path("."))
    local.add_argument("--require-clean", action="store_true")
    local.set_defaults(func=_cmd_local)

    msrv = subparsers.add_parser("msrv")
    msrv.add_argument("--toolchain", required=True)
    msrv.set_defaults(func=_cmd_msrv)

    remote = subparsers.add_parser("remote-state")
    remote.add_argument("--label", required=True)
    remote.add_argument("--expected-ref", required=True)
    remote.add_argument("--actual-ref", required=True)
    remote.add_argument("--status-file", type=Path, required=True)
    remote.set_defaults(func=_cmd_remote_state)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

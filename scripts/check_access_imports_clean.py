#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Smoke guard for import-clean `sol` access commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKED_FAMILIES = (
    "flask",
    "werkzeug",
    "jinja2",
    "anthropic",
    "openai",
    "google.genai",
    "google.generativeai",
    "httpx",
    "numpy",
    "PIL",
    "soundfile",
    "av",
    "pypdf",
    "frontmatter",
)
ACCESS_CASES: tuple[tuple[str, list[str]], ...] = (
    ("sol", ["sol"]),
    ("sol --help", ["sol", "--help"]),
    ("sol --version", ["sol", "--version"]),
    ("sol --path", ["sol", "--path"]),
    ("sol root", ["sol", "root"]),
    ("sol chat --help", ["sol", "chat", "--help"]),
    ("sol call --help", ["sol", "call", "--help"]),
    ("sol import --help", ["sol", "import", "--help"]),
    ("sol notify --help", ["sol", "notify", "--help"]),
    ("sol skills --help", ["sol", "skills", "--help"]),
    ("sol link --help", ["sol", "link", "--help"]),
    ("sol doctor --help", ["sol", "doctor", "--help"]),
)
HINT_CASES: tuple[tuple[str, list[str]], ...] = (
    ("journal convey --help", ["journal", "convey", "--help"]),
    ("journal transcribe --help", ["journal", "transcribe", "--help"]),
)
ROUTING_CASES: tuple[tuple[str, list[str], str], ...] = (
    (
        "service-routing help case",
        ["sol", "think", "--help"],
        "moved to 'journal think'",
    ),
    (
        "journal import --help",
        ["journal", "import", "--help"],
        "is a journal-access command",
    ),
)

CHILD = r"""
import importlib
import json
import os
import sys

payload = json.loads(sys.argv[1])
root = payload["root"]
if root not in sys.path:
    sys.path.insert(0, root)

blocked = tuple(payload["blocked"])

def blocked_family(fullname):
    return any(fullname == family or fullname.startswith(family + ".") for family in blocked)

class BlockHeavyFinder:
    def find_spec(self, fullname, path=None, target=None):
        if blocked_family(fullname):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None

sys.meta_path.insert(0, BlockHeavyFinder())

real_import_module = importlib.import_module
inject_heavy_module = os.environ.get("SOLSTONE_ACCESS_GUARD_INJECT_HEAVY_MODULE")
inject_mounted_app = os.environ.get("SOLSTONE_ACCESS_GUARD_INJECT_MOUNTED_APP")

def guarded_import_module(name, package=None):
    if inject_heavy_module and name == inject_heavy_module:
        __import__("numpy")
    if (
        inject_mounted_app
        and os.environ.get("SOLSTONE_STRICT_CALL_DISCOVERY") == "1"
        and name == f"solstone.apps.{inject_mounted_app}.call"
    ):
        raise RuntimeError(f"injected mounted app failure: {inject_mounted_app}")
    return real_import_module(name, package)

importlib.import_module = guarded_import_module

from solstone.think import sol_cli

sys.argv = payload["argv"]
if payload["argv"][0] == "journal":
    sol_cli.journal_main()
else:
    sol_cli.main()
"""


def _call_app_names(root: Path) -> list[str]:
    apps_dir = root / "solstone" / "apps"
    if not apps_dir.is_dir():
        return []
    return sorted(
        app_dir.name
        for app_dir in apps_dir.iterdir()
        if app_dir.is_dir()
        and not app_dir.name.startswith("_")
        and (app_dir / "call.py").is_file()
    )


def _run_case(
    root: Path,
    label: str,
    argv: list[str],
    *,
    strict_call_discovery: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("SOLSTONE_JOURNAL", str(root / "tests" / "fixtures" / "journal"))
    env["PYTHONPATH"] = (
        str(root)
        if not env.get("PYTHONPATH")
        else str(root) + os.pathsep + env["PYTHONPATH"]
    )
    if strict_call_discovery:
        env["SOLSTONE_STRICT_CALL_DISCOVERY"] = "1"
    if extra_env:
        env.update(extra_env)
    payload = {
        "root": str(root),
        "argv": argv,
        "blocked": BLOCKED_FAMILIES,
        "label": label,
    }
    return subprocess.run(
        [sys.executable, "-c", CHILD, json.dumps(payload)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _format_failure(label: str, result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"access-imports-clean: FAIL {label} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _has_traceback(result: subprocess.CompletedProcess[str]) -> bool:
    return "Traceback (most recent call last)" in result.stdout + result.stderr


def _check_access_case(
    root: Path,
    label: str,
    argv: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    strict = label == "sol call --help"
    result = _run_case(
        root,
        label,
        argv,
        strict_call_discovery=strict,
        extra_env=extra_env,
    )
    failures: list[str] = []
    if result.returncode != 0:
        failures.append(_format_failure(label, result))
        return failures
    if _has_traceback(result):
        failures.append(f"access-imports-clean: FAIL {label} printed a traceback")
    if strict:
        missing = [
            app_name
            for app_name in _call_app_names(root)
            if app_name not in result.stdout
        ]
        if missing:
            failures.append(
                f"access-imports-clean: FAIL sol call --help omitted apps: {missing}"
            )
    return failures


def _check_hint_case(root: Path, label: str, argv: list[str]) -> list[str]:
    result = _run_case(root, label, argv)
    output = result.stdout + result.stderr
    failures: list[str] = []
    if result.returncode == 0:
        failures.append(_format_failure(label, result))
    for expected in (
        "this command needs the journal host dependencies",
        "pip install 'solstone[journal]'",
        "uv tool install 'solstone[journal]'",
    ):
        if expected not in output:
            failures.append(
                f"access-imports-clean: FAIL {label} missing hint: {expected}"
            )
    if _has_traceback(result):
        failures.append(f"access-imports-clean: FAIL {label} printed a traceback")
    return failures


def _check_routing_case(
    root: Path,
    label: str,
    argv: list[str],
    expected: str,
) -> list[str]:
    result = _run_case(root, label, argv)
    output = result.stdout + result.stderr
    failures: list[str] = []
    if result.returncode == 0:
        failures.append(_format_failure(label, result))
    if expected not in output:
        failures.append(
            f"access-imports-clean: FAIL {label} missing routing text: {expected}"
        )
    if _has_traceback(result):
        failures.append(f"access-imports-clean: FAIL {label} printed a traceback")
    return failures


def run_checks(root: Path, *, extra_env: dict[str, str] | None = None) -> list[str]:
    failures: list[str] = []
    for label, argv in ACCESS_CASES:
        failures.extend(_check_access_case(root, label, argv, extra_env=extra_env))
    for label, argv in HINT_CASES:
        failures.extend(_check_hint_case(root, label, argv))
    for label, argv, expected in ROUTING_CASES:
        failures.extend(_check_routing_case(root, label, argv, expected))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--inject-heavy-module")
    parser.add_argument("--inject-mounted-app")
    args = parser.parse_args(argv)

    extra_env = {}
    if args.inject_heavy_module:
        extra_env["SOLSTONE_ACCESS_GUARD_INJECT_HEAVY_MODULE"] = (
            args.inject_heavy_module
        )
    if args.inject_mounted_app:
        extra_env["SOLSTONE_ACCESS_GUARD_INJECT_MOUNTED_APP"] = args.inject_mounted_app

    failures = run_checks(args.root.resolve(), extra_env=extra_env or None)
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("access-imports-clean: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

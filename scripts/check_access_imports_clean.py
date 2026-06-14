#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Smoke guard for import-clean `sol` access commands.

Two modes:

* Default (fast, wired into `make ci`): run each access command in a child that
  installs a `BlockHeavyFinder` on `sys.meta_path` to simulate the heavy host
  families (`BLOCKED_FAMILIES`) being absent. Fast and offline — the inner-loop
  gate.

* `--real-install` (faithful, opt-in via `make check-thin-base-install`): build
  a fresh venv with the REAL thin base partition (`pip install .`, no extras),
  assert the heavy families are genuinely absent, then run the same battery
  against that venv's interpreter. This catches what the simulation can't — an
  access command that imports a *non-blocked* light dep which is not in the thin
  base. The real install is the authority; `BLOCKED_FAMILIES` is the set we
  assert absent and the real-install mode verifies it against the partition,
  rather than standing in for it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    python: str | None = None,
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
        [python or sys.executable, "-c", CHILD, json.dumps(payload)],
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
    python: str | None = None,
) -> list[str]:
    strict = label == "sol call --help"
    result = _run_case(
        root,
        label,
        argv,
        strict_call_discovery=strict,
        extra_env=extra_env,
        python=python,
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


def _check_hint_case(
    root: Path, label: str, argv: list[str], *, python: str | None = None
) -> list[str]:
    result = _run_case(root, label, argv, python=python)
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
    *,
    python: str | None = None,
) -> list[str]:
    result = _run_case(root, label, argv, python=python)
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


def run_checks(
    root: Path,
    *,
    extra_env: dict[str, str] | None = None,
    python: str | None = None,
) -> list[str]:
    failures: list[str] = []
    for label, argv in ACCESS_CASES:
        failures.extend(
            _check_access_case(root, label, argv, extra_env=extra_env, python=python)
        )
    for label, argv in HINT_CASES:
        failures.extend(_check_hint_case(root, label, argv, python=python))
    for label, argv, expected in ROUTING_CASES:
        failures.extend(_check_routing_case(root, label, argv, expected, python=python))
    return failures


def _real_base_python(root: Path, tmpdir: str) -> str:
    """Build a fresh venv with the REAL thin base partition (no extras) and
    return its interpreter. `pip install .` resolves exactly what
    [project.dependencies] declares — the faithful counterpart to the in-CI
    BlockHeavyFinder simulation."""
    venv = Path(tmpdir) / "thin-base-venv"
    uv = shutil.which("uv")
    if uv:
        subprocess.run(
            [uv, "venv", str(venv)], check=True, capture_output=True, text=True
        )
        python = str(venv / "bin" / "python")
        subprocess.run(
            [uv, "pip", "install", "--python", python, str(root)],
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            text=True,
        )
        python = str(venv / "bin" / "python")
        subprocess.run(
            [python, "-m", "pip", "install", str(root)],
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
        )
    return python


def _check_heavy_absent(python: str) -> list[str]:
    """Assert no blocked heavy family is importable in the real thin base."""
    families = sorted({family.split(".")[0] for family in BLOCKED_FAMILIES})
    probe = (
        "import importlib.util as u, json, sys\n"
        "present = []\n"
        "for m in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        if u.find_spec(m) is not None: present.append(m)\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(present))\n"
    )
    result = subprocess.run(
        [python, "-c", probe, json.dumps(families)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        return [
            "access-imports-clean: FAIL heavy-absence probe errored\n"
            f"{result.stdout}\n{result.stderr}"
        ]
    present = json.loads(result.stdout or "[]")
    if present:
        return [
            "access-imports-clean: FAIL real base partition contains heavy "
            f"families: {present}"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--inject-heavy-module")
    parser.add_argument("--inject-mounted-app")
    parser.add_argument(
        "--real-install",
        action="store_true",
        help=(
            "build a fresh venv with the real thin base partition (no extras) "
            "and assert against it, instead of the meta_path simulation"
        ),
    )
    args = parser.parse_args(argv)

    extra_env = {}
    if args.inject_heavy_module:
        extra_env["SOLSTONE_ACCESS_GUARD_INJECT_HEAVY_MODULE"] = (
            args.inject_heavy_module
        )
    if args.inject_mounted_app:
        extra_env["SOLSTONE_ACCESS_GUARD_INJECT_MOUNTED_APP"] = args.inject_mounted_app

    root = args.root.resolve()
    if args.real_install:
        with tempfile.TemporaryDirectory(prefix="solstone-thin-base-") as tmpdir:
            print("access-imports-clean: building real thin-base venv (no extras)...")
            python = _real_base_python(root, tmpdir)
            failures = _check_heavy_absent(python)
            failures.extend(
                run_checks(root, extra_env=extra_env or None, python=python)
            )
    else:
        failures = run_checks(root, extra_env=extra_env or None)

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    mode = "real-install" if args.real_install else "simulated"
    print(f"access-imports-clean: pass ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

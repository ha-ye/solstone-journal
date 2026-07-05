#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Synchronize hand-maintained journal leaf package versions."""

from __future__ import annotations

import argparse
import logging
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_PYPROJECT = ROOT / "pyproject.toml"
CPU_PYPROJECT = ROOT / "packages" / "solstone-journal" / "pyproject.toml"
CUDA_PYPROJECT = ROOT / "packages" / "solstone-journal-cuda" / "pyproject.toml"
TOMBSTONE_PIN = "solstone-journal-host==0.7.0"
HOST_PIN_RE = re.compile(r'(?P<quote>")solstone\[journal-host\]==[^"]+(?P=quote)')
VERSION_RE = re.compile(r'(?m)^version = "[^"]+"')

LOGGER = logging.getLogger(__name__)


class PackagingRenderError(RuntimeError):
    """Raised when packaging metadata cannot be rendered safely."""


def _read_version(pyproject_text: str) -> str:
    data = tomllib.loads(pyproject_text)
    try:
        version = data["project"]["version"]
    except KeyError as exc:
        raise PackagingRenderError(
            "root pyproject.toml is missing [project] version"
        ) from exc
    if not isinstance(version, str) or not version:
        raise PackagingRenderError("root [project] version must be a non-empty string")
    return version


def _leaf_paths(root: Path) -> tuple[Path, Path]:
    return (
        root / "packages" / "solstone-journal" / "pyproject.toml",
        root / "packages" / "solstone-journal-cuda" / "pyproject.toml",
    )


def _rewrite_leaf(text: str, version: str) -> str:
    text, version_count = VERSION_RE.subn(f'version = "{version}"', text)
    if version_count != 1:
        raise PackagingRenderError(
            f"leaf pyproject must contain exactly one [project].version line; found {version_count}"
        )

    text, pin_count = HOST_PIN_RE.subn(f'"solstone[journal-host]=={version}"', text)
    if pin_count != 1:
        raise PackagingRenderError(
            f"leaf pyproject must contain exactly one solstone[journal-host]== pin; found {pin_count}"
        )
    return text


def _write_if_changed(path: Path, content: str) -> None:
    old_content = path.read_text(encoding="utf-8") if path.exists() else None
    if old_content == content:
        LOGGER.info("%s already up to date", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    LOGGER.info("wrote %s", path)


def _drifted(path: Path, expected: str) -> bool:
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return current != expected


def _check_root_tombstones(root: Path) -> list[str]:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data.get("project", {}).get("optional-dependencies", {})
    errors = []
    for name in ("journal", "journal-cuda"):
        if extras.get(name) != [TOMBSTONE_PIN]:
            errors.append(
                f"[project.optional-dependencies].{name} must be exactly [{TOMBSTONE_PIN!r}]"
            )
    return errors


def render(root: Path = ROOT) -> dict[Path, str]:
    root = Path(root)
    root_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = _read_version(root_text)
    return {
        path: _rewrite_leaf(path.read_text(encoding="utf-8"), version)
        for path in _leaf_paths(root)
    }


def check(root: Path = ROOT) -> int:
    root = Path(root)
    try:
        expected = render(root)
        tombstone_errors = _check_root_tombstones(root)
    except (OSError, tomllib.TOMLDecodeError, PackagingRenderError) as exc:
        print(f"packaging metadata check failed: {exc}")
        return 1

    drifted = [
        str(path.relative_to(root))
        for path, content in expected.items()
        if _drifted(path, content)
    ]
    if drifted or tombstone_errors:
        print("packaging metadata is stale; run python3 scripts/render_packaging.py")
        for path in drifted:
            print(f"  drifted: {path}")
        for error in tombstone_errors:
            print(f"  error: {error}")
        return 1
    print("packaging metadata is up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if generated packaging metadata is not up to date",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.check:
        return check()

    try:
        for path, content in render().items():
            _write_if_changed(path, content)
    except (OSError, tomllib.TOMLDecodeError, PackagingRenderError) as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

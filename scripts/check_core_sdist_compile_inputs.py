#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Check that shipping Rust compile-time inputs are covered by the core sdist."""

from __future__ import annotations

import argparse
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts.core_compile_inputs import (
        CoreCompileInputAsset,
        CoreCompileInputError,
        discover_core_compile_inputs,
    )
    from scripts.normalize_maturin_sdist import core_sdist_injected_files
except ModuleNotFoundError:  # pragma: no cover - direct script execution path.
    from core_compile_inputs import (  # type: ignore[no-redef]
        CoreCompileInputAsset,
        CoreCompileInputError,
        discover_core_compile_inputs,
    )
    from normalize_maturin_sdist import (  # type: ignore[no-redef]
        core_sdist_injected_files,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Violation:
    file: str
    kind: str
    detail: str


def collect_violations(root: Path, *, sdist: Path | None = None) -> list[Violation]:
    root = root.resolve()
    violations: list[Violation] = []
    try:
        assets = discover_core_compile_inputs(root)
    except CoreCompileInputError as exc:
        return [Violation("core", "compile-input-discovery-failed", str(exc))]
    if not assets:
        violations.append(
            Violation(
                "core",
                "compile-input-discovery-empty",
                "shipping solstone-core compile-input discovery returned no assets",
            )
        )
        return violations
    violations.extend(_injection_mapping_violations(root, assets))
    if sdist is not None:
        violations.extend(_archive_violations(root, assets, sdist))
    return violations


def _injection_mapping_violations(
    root: Path, assets: tuple[CoreCompileInputAsset, ...]
) -> list[Violation]:
    try:
        injected = core_sdist_injected_files(root)
    except Exception as exc:  # noqa: BLE001 - gate must report script-boundary failures.
        return [Violation("core", "sdist-injection-mapping-failed", str(exc))]
    violations: list[Violation] = []
    for asset in assets:
        actual = injected.get(asset.sdist_path)
        if actual is None:
            violations.append(
                Violation(
                    _rel(root, asset.source_file),
                    "compile-input-not-injected",
                    f"{asset.sdist_path} is absent from the normalizer injection mapping",
                )
            )
            continue
        expected = asset.resolved_path.read_bytes()
        if actual != expected:
            violations.append(
                Violation(
                    _rel(root, asset.source_file),
                    "compile-input-injected-bytes-mismatch",
                    f"{asset.sdist_path} bytes differ from source",
                )
            )
    return violations


def _archive_violations(
    root: Path, assets: tuple[CoreCompileInputAsset, ...], sdist: Path
) -> list[Violation]:
    try:
        with tarfile.open(sdist, "r:gz") as archive:
            members = archive.getmembers()
            roots = {Path(member.name).parts[0] for member in members if member.name}
            if len(roots) != 1:
                return [
                    Violation(
                        str(sdist),
                        "sdist-root-invalid",
                        f"expected one archive root, found {sorted(roots)}",
                    )
                ]
            archive_root = next(iter(roots))
            by_name = {member.name: member for member in members}
            violations: list[Violation] = []
            for asset in assets:
                member_name = f"{archive_root}/{asset.sdist_path}"
                member = by_name.get(member_name)
                if member is None:
                    violations.append(
                        Violation(
                            _rel(root, asset.source_file),
                            "compile-input-archive-member-missing",
                            f"{member_name} is absent from {sdist}",
                        )
                    )
                    continue
                if not member.isfile():
                    violations.append(
                        Violation(
                            _rel(root, asset.source_file),
                            "compile-input-archive-member-not-regular",
                            f"{member_name} is not a regular file",
                        )
                    )
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    violations.append(
                        Violation(
                            _rel(root, asset.source_file),
                            "compile-input-archive-member-unreadable",
                            f"{member_name} could not be read",
                        )
                    )
                    continue
                if extracted.read() != asset.resolved_path.read_bytes():
                    violations.append(
                        Violation(
                            _rel(root, asset.source_file),
                            "compile-input-archive-bytes-mismatch",
                            f"{member_name} bytes differ from source",
                        )
                    )
            return violations
    except (OSError, tarfile.TarError) as exc:
        return [Violation(str(sdist), "sdist-unreadable", str(exc))]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdist", type=Path)
    args = parser.parse_args()
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    violations = collect_violations(REPO_ROOT, sdist=args.sdist)
    if violations:
        LOGGER.error("core sdist compile-input violations:")
        for violation in violations:
            LOGGER.error(
                "- %s: %s: %s", violation.file, violation.kind, violation.detail
            )
        return 1
    LOGGER.info("core sdist compile inputs ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release_digest import file_sha256_size
from scripts.release_nvattest_proof import SUPPORT_DISTRIBUTION_NAMES
from scripts.release_nvattest_support import (
    SupportLockError,
    read_support_lock_entries,
    support_declarations_from_lock,
    verify_support_wheels_against_lock,
)
from tests.helpers.release_wheel_fixtures import write_support_wheel


def _support_version(index: int) -> str:
    return f"0.0.{index}"


def _write_support_wheels(path: Path) -> tuple[Path, ...]:
    return tuple(
        write_support_wheel(path, name=name, version=_support_version(index))
        for index, name in enumerate(sorted(SUPPORT_DISTRIBUTION_NAMES), start=1)
    )


def _write_lock(
    path: Path, wheels: tuple[Path, ...], *, omit: str | None = None
) -> None:
    blocks = ["version = 1\n"]
    by_name = {wheel.name.split("-", 1)[0].replace("_", "-"): wheel for wheel in wheels}
    for index, name in enumerate(sorted(SUPPORT_DISTRIBUTION_NAMES), start=1):
        if name == omit:
            continue
        wheel = by_name[name]
        sha256, size = file_sha256_size(wheel)
        blocks.extend(
            [
                "[[package]]\n",
                f'name = "{name}"\n',
                f'version = "{_support_version(index)}"\n',
                "wheels = [\n",
                "  { "
                f'url = "wheels/{wheel.name}", '
                f'hash = "sha256:{sha256}", '
                f"size = {size} "
                "},\n",
                "]\n\n",
            ]
        )
    path.write_text("".join(blocks), encoding="utf-8")


def test_support_lock_entries_verify_materialized_wheels(tmp_path: Path) -> None:
    wheels = _write_support_wheels(tmp_path / "wheels")
    lock = tmp_path / "uv.lock"
    _write_lock(lock, wheels)

    entries = read_support_lock_entries(lock)
    declarations = support_declarations_from_lock(entries)

    assert {entry.name for entry in entries} == SUPPORT_DISTRIBUTION_NAMES
    assert verify_support_wheels_against_lock(wheels, entries) == declarations


def test_support_lock_requires_exact_support_packages(tmp_path: Path) -> None:
    wheels = _write_support_wheels(tmp_path / "wheels")
    lock = tmp_path / "uv.lock"
    missing = sorted(SUPPORT_DISTRIBUTION_NAMES)[0]
    _write_lock(lock, wheels, omit=missing)

    with pytest.raises(SupportLockError) as exc:
        read_support_lock_entries(lock)

    assert any(
        failure.error == "nvattest support lock package is not exact"
        for failure in exc.value.failures
    )


def test_support_wheel_verification_rejects_lock_byte_mismatch(
    tmp_path: Path,
) -> None:
    wheels = _write_support_wheels(tmp_path / "wheels")
    lock = tmp_path / "uv.lock"
    _write_lock(lock, wheels)
    entries = read_support_lock_entries(lock)
    wheels[0].write_bytes(wheels[0].read_bytes() + b"changed")

    with pytest.raises(SupportLockError) as exc:
        verify_support_wheels_against_lock(wheels, entries)

    assert any(
        failure.error == "nvattest support wheel does not match uv.lock"
        for failure in exc.value.failures
    )

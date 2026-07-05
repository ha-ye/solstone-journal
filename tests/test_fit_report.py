# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.providers import fit_report, local_install
from solstone.think.providers.local import LocalProviderError
from solstone.think.providers.memory import MemoryVerdict


def test_overall_collapses_unknown_to_warning() -> None:
    report = fit_report.FitReport(
        artifact="artifact",
        checks=(
            fit_report.FitCheck("platform", "ok", "ok"),
            fit_report.FitCheck("probe", "unknown", "unknown"),
        ),
    )

    assert report.overall == "warning"
    assert "[unknown] probe: unknown" in fit_report.render_fit_report(report)


def test_overall_blocked_wins() -> None:
    report = fit_report.FitReport(
        artifact="artifact",
        checks=(
            fit_report.FitCheck("disk", "warning", "warning"),
            fit_report.FitCheck("platform", "blocked", "blocked"),
        ),
    )

    assert report.overall == "blocked"


def test_disk_unknown_size_warns_when_known_size_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fit_report, "free_bytes", lambda _path: 10)

    check = fit_report._disk_check(
        "disk",
        tmp_path,
        (("known", 5),),
        ("server tarball",),
    )

    assert check.severity == "warning"
    assert check.required_bytes == 5
    assert check.available_bytes == 10
    assert "unknown download size for server tarball" in check.detail


def test_disk_read_error_reports_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_free(_path: Path) -> int:
        raise OSError("disk unavailable")

    monkeypatch.setattr(fit_report, "free_bytes", fail_free)

    check = fit_report._disk_check("disk", tmp_path, (("known", 5),), ())

    assert check.severity == "unknown"
    assert check.required_bytes == 5
    assert check.available_bytes is None
    assert "could not be verified" in check.detail


def test_ram_unavailable_reports_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fit_report,
        "assess_memory",
        lambda required, *, block_below_floor: MemoryVerdict(
            available_bytes=None,
            required_bytes=required,
            severity="warning",
        ),
    )

    check = fit_report._ram_check(
        "ram",
        1024,
        block_below_floor=True,
        artifact_label="model",
    )

    assert check.severity == "warning"
    assert check.available_bytes is None
    assert "available memory could not be verified" in check.detail


def test_local_platform_unsupported_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_install, "llama_server_artifact_key", lambda: "bad")

    def fail_pin() -> None:
        raise LocalProviderError("unsupported_platform", "unsupported test platform")

    monkeypatch.setattr(local_install, "pin_for_current_platform", fail_pin)

    check = fit_report._local_platform_check()

    assert check.severity == "blocked"
    assert check.detail == "unsupported test platform"

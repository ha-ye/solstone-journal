# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest


@pytest.fixture
def doctor():
    from solstone.think import doctor as doctor_module

    return doctor_module


def args(doctor):
    return doctor.Args(verbose=False, json=False, jsonl=False, port=5015)


def test_task_pace_skips_when_supervisor_status_unavailable(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "fetch_supervisor_status", lambda: None)

    result = doctor.task_pace_check(args(doctor))

    assert result.status == "skip"


def test_task_pace_ok_when_idle(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "fetch_supervisor_status", lambda: {"tasks": []})

    result = doctor.task_pace_check(args(doctor))

    assert result.status == "ok"


def test_task_pace_warns_on_slow_task(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "fetch_supervisor_status",
        lambda: {
            "tasks": [
                {
                    "name": "providers",
                    "duration_seconds": 80,
                    "max_runtime_seconds": 100,
                    "slow": True,
                    "stuck": False,
                }
            ]
        },
    )

    result = doctor.task_pace_check(args(doctor))

    assert result.status == "warn"
    assert "providers" in result.detail


def test_task_pace_ok_under_soft_fraction(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "fetch_supervisor_status",
        lambda: {
            "tasks": [
                {
                    "name": "providers",
                    "duration_seconds": 50,
                    "max_runtime_seconds": 100,
                    "slow": False,
                    "stuck": False,
                }
            ]
        },
    )

    result = doctor.task_pace_check(args(doctor))

    assert result.status == "ok"


def test_task_pace_warns_on_stuck_task(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "fetch_supervisor_status",
        lambda: {
            "tasks": [
                {
                    "name": "providers",
                    "duration_seconds": 120,
                    "max_runtime_seconds": 100,
                    "slow": True,
                    "stuck": True,
                }
            ]
        },
    )

    result = doctor.task_pace_check(args(doctor))

    assert result.status == "warn"
    assert "providers" in result.detail


def test_task_pace_registered_in_journal_checks(doctor):
    assert "task_pace" in {check.name for check, _ in doctor.JOURNAL_CHECKS}
    assert doctor.CHECK_MAP["task_pace"].severity == "advisory"

# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.think import features


@pytest.fixture
def doctor():
    from solstone.think import doctor as doctor_module

    return doctor_module


def args(doctor, *, port: int = 5015, feature: str | None = None):
    return doctor.Args(
        verbose=False, json=False, jsonl=False, port=port, feature=feature
    )


def run_check(doctor, name: str):
    _check, runner = doctor.FEATURE_CHECKS[name.removeprefix("feature:")]
    return runner(args(doctor))


def test_feature_checks_registered(doctor):
    assert set(doctor.FEATURE_CHECKS) == set(features.FEATURES)


def test_feature_checks_in_check_map(doctor):
    assert "feature:pdf-import" in doctor.CHECK_MAP
    assert "feature:pdf-export" in doctor.CHECK_MAP
    assert f"feature:{'pdf'}" not in doctor.CHECK_MAP


def test_pdf_import_feature_check_ok_when_available(doctor):
    result = run_check(doctor, "feature:pdf-import")

    assert result.status == "ok"


def test_pdf_import_feature_check_warns_when_missing(doctor, monkeypatch):
    monkeypatch.setattr(features, "is_available", lambda name: name != "pdf-import")

    result = run_check(doctor, "feature:pdf-import")

    assert result.status == "warn"
    assert result.fix == features.install_hint("pdf-import", doctor.platform_tag())


def test_parse_args_feature(doctor):
    parsed = doctor.parse_args(["--feature", "pdf-import"])

    assert parsed.feature == "pdf-import"


def test_parse_args_rejects_unknown_feature(doctor, capsys):
    with pytest.raises(SystemExit) as error:
        doctor.parse_args(["--feature", "bogus"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "known features" in stderr
    assert "pdf-import" in stderr


def test_run_checks_filters_to_feature(doctor):
    results = doctor.run_checks(args(doctor, feature="pdf-import"))

    assert len(results) == 1
    assert results[0].name == "feature:pdf-import"


def test_emit_json_filtered_summary(doctor, capsys):
    results = doctor.run_checks(args(doctor, feature="pdf-import"))

    doctor.emit_json(results)
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["total"] == 1
    assert payload["summary"]["errors"] == 0
    assert len(payload["checks"]) == 1
    assert payload["checks"][0]["execution_error"] is None

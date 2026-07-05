# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from scripts.check_extras_consistency import _check_models_pin


def test_check_models_pin_accepts_exact_pin():
    extras = {
        "journal-host": [
            "solstone-journal-models==1.0.0",
            "solstone[pdf]",
        ]
    }

    assert _check_models_pin(extras, "1.0.0") == []


def test_check_models_pin_reports_missing_pin():
    extras = {"journal-host": ["solstone[pdf]"]}

    errors = _check_models_pin(extras, "1.0.0")

    assert errors
    assert "exactly one solstone-journal-models== pin; found 0" in errors[0]


def test_check_models_pin_reports_wrong_version():
    extras = {"journal-host": ["solstone-journal-models==0.9.0"]}

    errors = _check_models_pin(extras, "1.0.0")

    assert errors
    assert "solstone-journal-models==1.0.0" in errors[0]
    assert "solstone-journal-models==0.9.0" in errors[0]

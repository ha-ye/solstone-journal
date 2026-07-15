# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import sys


def test_generate_plan(monkeypatch):
    sys.modules.pop("solstone.think.planner", None)
    mod = importlib.import_module("solstone.think.planner")

    # Mock generate to return "plan"
    def mock_generate(**kwargs):
        return "plan"

    monkeypatch.setattr("solstone.think.models.generate", mock_generate)
    result = mod.generate_plan("do something")
    assert result == "plan"

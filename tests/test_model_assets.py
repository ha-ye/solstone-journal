# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import sys

import pytest

from solstone.think.model_assets import (
    ModelsDistributionUnavailable,
    resolve_wespeaker_model,
)

MISSING_MODELS_MESSAGE = (
    "solstone-journal-models is not installed; it ships solstone's bundled "
    "speaker/VAD model weights and is included with a journal-host install "
    "(for example: pip install solstone-journal)."
)


class _BlockModelsDistribution:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "solstone_journal_models" or fullname.startswith(
            "solstone_journal_models."
        ):
            raise ImportError("blocked solstone_journal_models")
        return None


def test_transcribe_imports_survive_missing_models_distribution(monkeypatch):
    for name in list(sys.modules):
        if name == "solstone_journal_models" or name.startswith(
            "solstone_journal_models."
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockModelsDistribution(), *sys.meta_path])

    importlib.import_module("solstone.observe.transcribe.main")
    importlib.import_module("solstone.observe._silero_vad")

    with pytest.raises(ModelsDistributionUnavailable) as exc_info:
        resolve_wespeaker_model()

    assert str(exc_info.value) == MISSING_MODELS_MESSAGE

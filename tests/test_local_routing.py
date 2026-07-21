# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for local-only provider routing promises."""

from __future__ import annotations

import json
from pathlib import Path

from solstone.think.models import LOCAL_MODEL
from solstone.think.talents import prepare_config


def _read_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text(encoding="utf-8"))


def _write_config(journal: Path, config: dict) -> None:
    (journal / "config" / "journal.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_prepare_config_uses_active_local_and_ignores_legacy_context_pin(
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    providers = config.setdefault("providers", {})
    providers["active"] = {"provider": "local", "model": LOCAL_MODEL}
    contexts = providers.setdefault("contexts", {})
    contexts["talent.timeline.segment_summary"] = {
        "provider": "google",
        "model": "gemini-3.1-flash-lite",
    }
    _write_config(journal_copy, config)

    prepared = prepare_config({"name": "timeline:segment_summary"})

    assert prepared["provider"] == "local"
    assert prepared["model"] == LOCAL_MODEL


def test_prepare_config_rejects_explicit_request_model_pin(
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    providers = config.setdefault("providers", {})
    providers["active"] = {"provider": "local", "model": LOCAL_MODEL}
    _write_config(journal_copy, config)

    import pytest

    with pytest.raises(ValueError, match="request overrides for 'provider'"):
        prepare_config(
            {
                "name": "timeline:segment_summary",
                "provider": "local",
                "model": "local/custom-7b",
            }
        )

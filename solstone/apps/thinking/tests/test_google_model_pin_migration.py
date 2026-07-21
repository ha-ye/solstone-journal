# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.thinking.google_model_pins import (
    GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD,
    GOOGLE_PRO_ALIAS,
    THINKING_BYO_MODEL_HREF,
    read_google_exact_model_advisory,
)
from solstone.think.journal_io import LockTimeout

migration = importlib.import_module(
    "solstone.apps.thinking.maint.002_pin_google_model_aliases"
)


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_config(journal: Path, config: dict[str, Any]) -> None:
    path = _config_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads(_config_path(journal).read_text(encoding="utf-8"))


def _run_main(
    journal: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    monkeypatch.setattr(migration, "get_journal", lambda: str(journal))
    migration.main()
    return capsys.readouterr().out.strip().splitlines()


def test_migration_pins_all_slots_and_reports_secret_free_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = {
        "env": {"GOOGLE_API_KEY": "secret-key"},
        "providers": {
            "active": {"provider": "google", "model": "gemini-flash-latest"},
            "byo_models": {"google": "gemini-flash-lite-latest"},
        },
        "services": {
            "confidential": {
                "endpoint_url": "https://spp.example.test/v1",
                "served_model_id": "served",
                "prior_active": {
                    "provider": "google",
                    "model": "gemini-flash-latest",
                },
                "prior_local_endpoint": {
                    "endpoint_url": "https://prior.example.test/v1"
                },
                "prompt": "do-not-print",
                "response": "do-not-print",
            }
        },
    }
    _write_config(tmp_path, config)

    lines = _run_main(tmp_path, monkeypatch, capsys)

    assert lines == [
        "providers.active.model: gemini-flash-latest -> gemini-3.5-flash",
        "providers.byo_models.google: gemini-flash-lite-latest -> gemini-3.1-flash-lite",
        "services.confidential.prior_active.model: gemini-flash-latest -> gemini-3.5-flash",
    ]
    stored = _read_config(tmp_path)
    assert stored["providers"]["active"]["model"] == "gemini-3.5-flash"
    assert stored["providers"]["byo_models"]["google"] == "gemini-3.1-flash-lite"
    assert (
        stored["services"]["confidential"]["prior_active"]["model"]
        == "gemini-3.5-flash"
    )
    output = "\n".join(lines)
    for secret in (
        "secret-key",
        "https://spp.example.test/v1",
        "https://prior.example.test/v1",
        "do-not-print",
    ):
        assert secret not in output

    before = _config_path(tmp_path).read_bytes()
    lines = _run_main(tmp_path, monkeypatch, capsys)
    assert lines == ["Google model aliases already pinned."]
    assert _config_path(tmp_path).read_bytes() == before


def test_migration_keeps_pro_aliases_and_reports_choose_model_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = {
        "providers": {
            "active": {"provider": "google", "model": GOOGLE_PRO_ALIAS},
            "byo_models": {"google": GOOGLE_PRO_ALIAS},
        },
        "services": {
            "confidential": {
                "prior_active": {"provider": "google", "model": GOOGLE_PRO_ALIAS}
            }
        },
    }
    _write_config(tmp_path, config)

    lines = _run_main(tmp_path, monkeypatch, capsys)

    assert lines == [
        "providers.active.model: gemini-pro-latest -> choose exact Gemini model",
        "providers.byo_models.google: gemini-pro-latest -> choose exact Gemini model",
        "services.confidential.prior_active.model: gemini-pro-latest -> choose exact Gemini model",
    ]
    assert _read_config(tmp_path) == config


def test_migration_ignores_custom_provider_mismatched_and_non_exact_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = {
        "providers": {
            "active": {"provider": "anthropic", "model": "gemini-flash-latest"},
            "byo_models": {"google": " gemini-flash-lite-latest "},
        },
        "services": {
            "confidential": {
                "prior_active": {
                    "provider": "google",
                    "model": "Gemini-Flash-Latest",
                }
            }
        },
    }
    _write_config(tmp_path, config)
    before = _config_path(tmp_path).read_bytes()

    lines = _run_main(tmp_path, monkeypatch, capsys)

    assert lines == ["Google model aliases already pinned."]
    assert _config_path(tmp_path).read_bytes() == before
    assert _read_config(tmp_path) == config


def test_migration_ignores_custom_google_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = {
        "providers": {
            "active": {"provider": "google", "model": "gemini-custom-flash"},
            "byo_models": {"google": "gemini-custom-remembered"},
        },
        "services": {
            "confidential": {
                "prior_active": {
                    "provider": "google",
                    "model": "gemini-custom-prior",
                }
            }
        },
    }
    _write_config(tmp_path, config)
    before = _config_path(tmp_path).read_bytes()

    lines = _run_main(tmp_path, monkeypatch, capsys)

    assert lines == ["Google model aliases already pinned."]
    assert _config_path(tmp_path).read_bytes() == before
    assert _read_config(tmp_path) == config


@pytest.mark.parametrize(
    "error",
    [
        LockTimeout(path=Path("config/journal.json"), timeout=0.01),
        OSError("commit failed"),
    ],
)
def test_migration_propagates_transaction_failures_without_partial_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    config = {
        "providers": {"active": {"provider": "google", "model": "gemini-flash-latest"}}
    }
    _write_config(tmp_path, config)
    before = _config_path(tmp_path).read_bytes()
    monkeypatch.setattr(migration, "get_journal", lambda: str(tmp_path))

    def fail_mutation(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(migration, "mutate_journal_config", fail_mutation)

    with pytest.raises(type(error)):
        migration.main()

    assert migration.MAINT_RETRY_ON_NEXT_START is True
    assert migration.MAINT_BLOCKS_SUPERVISOR_START is True
    assert _config_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize(
    ("config", "expected_targets"),
    [
        (
            {
                "providers": {
                    "active": {"provider": "google", "model": GOOGLE_PRO_ALIAS}
                }
            },
            ["active"],
        ),
        (
            {"providers": {"byo_models": {"google": GOOGLE_PRO_ALIAS}}},
            ["remembered"],
        ),
        (
            {
                "services": {
                    "confidential": {
                        "prior_active": {
                            "provider": "google",
                            "model": GOOGLE_PRO_ALIAS,
                        }
                    }
                }
            },
            ["confidential_prior"],
        ),
    ],
)
def test_read_google_exact_model_advisory_is_content_free(
    config: dict[str, Any],
    expected_targets: list[str],
) -> None:
    advisory = read_google_exact_model_advisory(config)

    assert advisory == {
        "id": "choose_exact_gemini_model",
        "heading": "choose an exact Gemini model",
        GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: expected_targets,
        "action": {"label": "choose model", "href": THINKING_BYO_MODEL_HREF},
    }
    assert GOOGLE_PRO_ALIAS not in json.dumps(advisory)


def test_read_google_exact_model_advisory_clears_for_exact_models() -> None:
    config = {
        "providers": {
            "active": {"provider": "google", "model": "gemini-3.5-flash"},
            "byo_models": {"google": "gemini-3.5-flash"},
        },
        "services": {
            "confidential": {
                "prior_active": {"provider": "google", "model": "gemini-3.5-flash"}
            }
        },
    }

    assert read_google_exact_model_advisory(config) is None

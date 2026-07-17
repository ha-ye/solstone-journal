# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from solstone.think import user_config
from solstone.think import utils as think_utils

VECTOR_PATH = Path("tests/fixtures/journal_path_resolution_vectors.json")
TMP_TOKEN = "$TMP"


def _load_vectors() -> dict[str, Any]:
    vectors = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    cases = vectors.get("cases")
    assert isinstance(cases, list), "journal path vectors must contain a cases list"
    assert cases, "journal path vectors must not be empty"
    for case in cases:
        _validate_case(case)
    return vectors


def _validate_state(
    mapping: dict[str, Any],
    *,
    field: str,
    case_id: str,
    allowed: set[str],
) -> None:
    state = mapping.get("state")
    assert state in allowed, f"unknown {field} state for {case_id}: {state}"


def _validate_case(case: dict[str, Any]) -> None:
    case_id = case["id"]
    _validate_state(
        case["solstone_journal"],
        field="solstone_journal",
        case_id=case_id,
        allowed={"absent", "set"},
    )
    _validate_state(
        case["home"],
        field="home",
        case_id=case_id,
        allowed={"absent", "set"},
    )
    _validate_state(
        case["config"],
        field="config",
        case_id=case_id,
        allowed={"absent", "text", "hex"},
    )
    _validate_state(
        case["checkout_root"],
        field="checkout_root",
        case_id=case_id,
        allowed={"absent", "present"},
    )
    outcome_type = case["outcome"].get("type")
    assert outcome_type in {"ok", "error"}, (
        f"unknown outcome type for {case_id}: {outcome_type}"
    )


def _expand(value: str, tmp_path: Path) -> str:
    return value.replace(TMP_TOKEN, str(tmp_path))


def _expand_mapping(mapping: dict[str, str], tmp_path: Path) -> dict[str, str]:
    return {
        key: _expand(value, tmp_path) if isinstance(value, str) else value
        for key, value in mapping.items()
    }


VECTORS = _load_vectors()
CASES = VECTORS["cases"]


def _write_config(config: dict[str, str]) -> None:
    if config["state"] == "absent":
        return
    cfg = user_config.config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if config["state"] == "text":
        cfg.write_text(config["value"], encoding="utf-8")
    elif config["state"] == "hex":
        cfg.write_bytes(bytes.fromhex(config["value"]))
    else:
        raise AssertionError(f"unknown config state: {config['state']}")


def _write_checkout_root(checkout_root: dict[str, str]) -> None:
    root = Path(checkout_root["path"])
    root.mkdir(parents=True, exist_ok=True)
    if checkout_root["state"] == "present":
        (root / "pyproject.toml").write_text(
            '[project]\nname = "probe"\n',
            encoding="utf-8",
        )
        (root / ".git").mkdir()
    elif checkout_root["state"] != "absent":
        raise AssertionError(f"unknown checkout_root state: {checkout_root['state']}")


def _assert_python_get_journal_info_matches_vector(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    solstone_journal = _expand_mapping(case["solstone_journal"], tmp_path)
    home = _expand_mapping(case["home"], tmp_path)
    config = _expand_mapping(case["config"], tmp_path)
    checkout_root = _expand_mapping(case["checkout_root"], tmp_path)
    cwd = Path(_expand(case["cwd"], tmp_path))
    cwd.mkdir(parents=True)

    if solstone_journal["state"] == "absent":
        monkeypatch.delenv("SOLSTONE_JOURNAL", raising=False)
    else:
        monkeypatch.setenv("SOLSTONE_JOURNAL", solstone_journal["value"])
    if home["state"] == "absent":
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", home["value"])
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(think_utils, "get_project_root", lambda: checkout_root["path"])

    _write_config(config)
    _write_checkout_root(checkout_root)

    outcome = case["outcome"]
    if outcome["type"] == "error":
        with pytest.raises(Exception) as excinfo:
            think_utils.get_journal_info()
        assert excinfo.value.__class__.__name__ == outcome["python_class"], case["id"]
        return

    path, source = think_utils.get_journal_info()
    assert source == outcome["source"], case["id"]
    assert path == _expand(outcome["path"], tmp_path), case["id"]


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[case["id"] for case in CASES],
)
def test_python_get_journal_info_matches_vector(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_python_get_journal_info_matches_vector(case, monkeypatch, tmp_path)


def test_vector_cases_are_stably_ordered() -> None:
    ids = [case["id"] for case in CASES]
    assert ids == sorted(ids)

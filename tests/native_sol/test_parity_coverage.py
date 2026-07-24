# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import scripts.build_native_sol_inventory as inventory

PARITY_DIR = inventory.REPO_ROOT / "core/fixtures/native-sol/parity"


def load_vectors(paths: list[Path]) -> list[dict[str, object]]:
    vectors: list[dict[str, object]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                vectors.append(json.loads(line))
    return vectors


VECTORS = (
    load_vectors(sorted(PARITY_DIR.glob("*.jsonl"))) if PARITY_DIR.exists() else []
)
AUTHORITIES = inventory.discover(inventory.REPO_ROOT)


def test_every_declared_option_token_has_parity_coverage() -> None:
    missing: list[str] = []
    for entry in AUTHORITIES:
        tokens = set(iter_entry_argv_tokens(entry, VECTORS))
        for param in entry.params:
            if param["kind"] != "option":
                continue
            for option in list(param["options"]) + list(param["secondary"]):
                if option not in tokens:
                    missing.append(
                        f"{' '.join(entry.path)} {param['name']} token {option}"
                    )

    assert missing == []


def test_every_declared_positional_has_parity_coverage() -> None:
    missing: list[str] = []
    for entry in AUTHORITIES:
        if not any(param["kind"] == "argument" for param in entry.params):
            continue
        if not any(vector_has_positional(entry, vector) for vector in VECTORS):
            missing.append(" ".join(entry.path))

    assert missing == []


def test_activity_env_default_seams_have_parity_coverage() -> None:
    activity_vectors = [
        vector
        for vector in VECTORS
        if vector.get("surface") == "sol-call"
        and list(vector.get("argv", []))[:1] == ["activities"]
    ]

    assert any(
        vector.get("env", {}).get("SOL_DAY") == "20260723"
        and vector.get("env", {}).get("SOL_FACET") == "work"
        and "--day" not in vector["argv"]
        and "-d" not in vector["argv"]
        and "--facet" not in vector["argv"]
        and "-f" not in vector["argv"]
        for vector in activity_vectors
    )
    assert any(
        vector.get("env", {}).get("SOL_FACET") == "work"
        and "day is required" in vector.get("expected", {}).get("stderr", "")
        for vector in activity_vectors
    )
    assert any(
        vector.get("env", {}).get("SOL_DAY") == "20260723"
        and "facet is required" in vector.get("expected", {}).get("stderr", "")
        for vector in activity_vectors
    )


def iter_entry_argv_tokens(
    entry: inventory.AuthorityEntry,
    vectors: Iterable[dict[str, object]],
) -> Iterable[str]:
    for vector in vectors:
        if not vector_matches_entry(entry, vector):
            continue
        yield from argv_tail(entry, vector)


def vector_has_positional(
    entry: inventory.AuthorityEntry,
    vector: dict[str, object],
) -> bool:
    if not vector_matches_entry(entry, vector):
        return False

    option_params = {
        option: param
        for param in entry.params
        if param["kind"] == "option"
        for option in list(param["options"]) + list(param["secondary"])
    }
    tail = argv_tail(entry, vector)
    index = 0
    while index < len(tail):
        token = tail[index]
        param = option_params.get(token)
        if param is not None:
            index += 1
            if not param["is_flag"] and index < len(tail):
                index += 1
            continue
        return True
    return False


def vector_matches_entry(
    entry: inventory.AuthorityEntry,
    vector: dict[str, object],
) -> bool:
    if entry.surface != vector.get("surface"):
        return False
    argv = list(vector.get("argv", []))
    if entry.surface == "sol-chat":
        return tuple(entry.path) == ("chat",)
    if entry.surface == "sol-import":
        return tuple(argv[: len(entry.path)]) == entry.path
    return tuple(argv[: len(entry.path)]) == entry.path


def argv_tail(
    entry: inventory.AuthorityEntry,
    vector: dict[str, object],
) -> list[str]:
    argv = [str(arg) for arg in vector.get("argv", [])]
    if entry.surface == "sol-chat":
        return argv
    if entry.surface == "sol-import":
        return argv[1:]
    return argv[len(entry.path) :]

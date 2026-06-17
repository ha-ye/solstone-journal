# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for entities CLI verbs referenced by read talent prose."""

from __future__ import annotations

import re
from pathlib import Path

import typer

from solstone.apps.entities.call import app as entities_app

ROOT = Path(__file__).resolve().parents[1]


def _live_entities_verbs() -> set[str]:
    return set(typer.main.get_command(entities_app).commands.keys())


def _extract_entities_verbs_from_read_talent(text: str) -> set[str]:
    verbs: set[str] = set()
    for code_span in re.findall(r"`([^`]+)`", text):
        verbs.update(re.findall(r"\bentities\s+([a-z][a-z-]*)", code_span))

    slash_run_pattern = re.compile(
        r"`sol call entities\s+[a-z][a-z-]*`(?:\s*/\s*`[a-z][a-z-]*`)+"
    )
    for match in slash_run_pattern.finditer(text):
        for code_span in re.findall(r"`([^`]+)`", match.group(0)):
            entity_match = re.search(r"\bentities\s+([a-z][a-z-]*)", code_span)
            if entity_match:
                verbs.add(entity_match.group(1))
            elif re.fullmatch(r"[a-z][a-z-]*", code_span):
                verbs.add(code_span)
    return verbs


def _extract_entities_verbs_from_cogitate_doc(text: str) -> set[str]:
    return set(re.findall(r"`sol call entities\s+([a-z][a-z-]*)", text))


def test_read_talent_entities_cli_references_are_live() -> None:
    live = _live_entities_verbs()
    read_verbs = _extract_entities_verbs_from_read_talent(
        (ROOT / "solstone" / "talent" / "read.md").read_text(encoding="utf-8")
    )
    cogitate_verbs = _extract_entities_verbs_from_cogitate_doc(
        (ROOT / "docs" / "COGITATE.md").read_text(encoding="utf-8")
    )
    extracted = read_verbs | cogitate_verbs

    assert extracted
    assert extracted <= live
